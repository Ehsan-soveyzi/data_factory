"""Section 5: a fixed no-learning baseline and development model comparison."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from time import perf_counter
import warnings

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import roc_auc_score
from threadpoolctl import threadpool_limits

from . import credit_feature_engineering as engineering
from . import credit_missingness as missingness
from . import credit_preprocessing as preprocessing


FAMILIES = ("logistic", "random_forest", "hist_gradient_boosting")
BASELINE = "always_healthy"
FALSE_NEGATIVE_COST = 12
FALSE_POSITIVE_COST = 1
THRESHOLD = missingness.DECISION_THRESHOLD
RF_PARAMETERS = {
    "n_estimators": 200, "max_depth": 12, "min_samples_leaf": 20,
    "max_features": "sqrt", "class_weight": None, "n_jobs": 2, "random_state": 42,
}


def load_development(engine, evidence_dir):
    """Load only development data and freeze the already selected feature recipes."""
    data = preprocessing.load_development(engine, evidence_dir)
    raw, target, groups, folds, partition, policy_choice, database = data
    choice = json.loads((Path(evidence_dir) / "section_04_selected_features.json").read_text(encoding="utf-8"))
    policy = policy_choice["selected_policy"]
    if choice["source_fingerprint"] != partition["source_fingerprint"] or choice["policy"] != policy:
        raise ValueError("Section 4 features do not match this source snapshot/imputation policy.")
    feature_map = {family: tuple(choice["selected_features"][family])
                   for family in preprocessing.FAMILIES}
    # New reference family: transfer the frozen tree representation, without
    # another feature search or a claim that it is optimal for Random Forest.
    feature_map["random_forest"] = feature_map["hist_gradient_boosting"]
    return raw, target, groups, folds, partition, policy, feature_map, database


def make_model_pipeline(*, family, features, policy="median_indicator"):
    """Create an unfitted full sklearn Pipeline for the requested model family."""
    if family not in FAMILIES:
        raise ValueError(f"Unknown model family: {family}")
    preparation_family = "hist_gradient_boosting" if family == "random_forest" else family
    pipeline = engineering.make_feature_pipeline(
        family=preparation_family, features=features, policy=policy)
    if family == "random_forest":
        pipeline.set_params(classifier=RandomForestClassifier(**RF_PARAMETERS))
    return pipeline


def decision_metrics(target, prediction, probabilities=None):
    actual = np.asarray(target, dtype=int)
    prediction = np.asarray(prediction, dtype=int)
    if actual.ndim != 1 or prediction.shape != actual.shape or not len(actual):
        raise ValueError("Nonempty aligned target/prediction vectors are required.")
    if not np.isin(actual, [0, 1]).all() or not np.isin(prediction, [0, 1]).all():
        raise ValueError("Only binary class labels are supported.")
    fn = int(np.sum((actual == 1) & (prediction == 0)))
    fp = int(np.sum((actual == 0) & (prediction == 1)))
    tp = int(np.sum((actual == 1) & (prediction == 1)))
    tn = int(np.sum((actual == 0) & (prediction == 0)))
    cost = FALSE_NEGATIVE_COST * fn + FALSE_POSITIVE_COST * fp
    return {"records": len(actual), "false_negative": fn, "false_positive": fp,
            "true_positive": tp, "true_negative": tn, "cost": cost,
            "cost_per_1000": 1000 * cost / len(actual),
            "roc_auc": float(roc_auc_score(actual, probabilities)) if probabilities is not None else None}


def _validate_partition(raw, target, groups, folds, partition):
    if not raw.index.is_unique or not np.array_equal(raw.index.to_numpy(), target.index.to_numpy()):
        raise ValueError("Inputs/target must have the same unique ordered source IDs.")
    expected = sorted(i for ids in partition["development_validation_row_ids"].values() for i in ids)
    if raw.index.tolist() != expected or len(raw) != len(groups):
        raise ValueError("The saved development membership is required.")
    if set(raw.index).intersection(partition["holdout_row_ids"]) or len(folds) != 5:
        raise ValueError("The final holdout must remain outside the five development folds.")
    coverage = np.zeros(len(raw), dtype=int)
    seen_folds = set()
    for fold, training, validation in folds:
        if fold in seen_folds or fold not in range(1, 6):
            raise ValueError("Expected five distinct saved fold identifiers.")
        seen_folds.add(fold)
        if raw.index[validation].tolist() != partition["development_validation_row_ids"][str(fold)]:
            raise ValueError("Validation membership/order differs from the saved fold.")
        if (np.intersect1d(training, validation).size or
                not np.array_equal(np.sort(np.concatenate([training, validation])), np.arange(len(raw)))):
            raise ValueError("Training/validation positions must partition development exactly.")
        if np.intersect1d(groups[training], groups[validation]).size:
            raise ValueError("Identical-feature groups cross a training/validation boundary.")
        coverage[validation] += 1
    if not np.all(coverage == 1):
        raise ValueError("Each development row must be evaluated exactly once.")


def evaluate_baseline(raw, target, groups, folds, partition):
    """Always predict class 0: no fitted model, probability or threshold is needed."""
    _validate_partition(raw, target, groups, folds, partition)
    rows = []
    for fold, _, validation in folds:
        metrics = decision_metrics(target.iloc[validation], np.zeros(len(validation), dtype=int))
        rows.append({"model": BASELINE, "fold": fold, **metrics})
    metrics = decision_metrics(target, np.zeros(len(target), dtype=int))
    summary = {"model": BASELINE, "learned": False, "input_columns": 0,
               **metrics, "fold_cost_std": float(np.std([r["cost_per_1000"] for r in rows], ddof=1)),
               "reduction_vs_baseline_percent": 0.0, "meets_25_percent": False,
               "fit_seconds": 0.0, "predict_seconds": 0.0}
    if summary["cost"] <= 0:
        raise ValueError("A positive baseline cost is required for percentage comparison.")
    return summary, pd.DataFrame(rows)


def compare_models(raw, target, groups, folds, partition, *, policy, feature_map,
                   baseline, progress=None):
    """Train three frozen recipes with complete, training-only OOF predictions."""
    _validate_partition(raw, target, groups, folds, partition)
    if set(feature_map) != set(FAMILIES):
        raise ValueError("A frozen feature recipe is required for each candidate family.")
    baseline_check = decision_metrics(target, np.zeros(len(target), dtype=int))
    if baseline_check["cost"] != baseline["cost"] or baseline_check["records"] != baseline["records"]:
        raise ValueError("Baseline and candidates must use the same evaluation population.")
    fill_reference = {}
    for fold, training, _ in folds:
        clean = preprocessing.semantic_inputs(raw.iloc[training])
        strategy = missingness.POLICIES[policy]["strategy"]
        reference = clean.median() if strategy == "median" else clean.mode().iloc[0]
        fill_reference[fold] = reference.to_numpy(dtype=float)

    fold_ids = np.zeros(len(raw), dtype=np.uint8)
    for fold, _, validation in folds:
        fold_ids[validation] = fold
    oof = pd.DataFrame({"row_id": raw.index.to_numpy(dtype="int64"),
                        "fold": fold_ids, "target": target.to_numpy(dtype=np.uint8)})
    summaries, results, audits = [], [], []
    for family in FAMILIES:
        probabilities = np.full(len(raw), np.nan)
        coverage = np.zeros(len(raw), dtype=int)
        fit_seconds = predict_seconds = 0.0
        family_costs = []
        for fold, training, validation in folds:
            pipeline = make_model_pipeline(family=family, features=feature_map[family], policy=policy)
            train, valid = raw.iloc[training], raw.iloc[validation]
            with warnings.catch_warnings(record=True) as caught, threadpool_limits(limits=1):
                warnings.simplefilter("always", ConvergenceWarning)
                started = perf_counter()
                pipeline.fit(train, target.iloc[training])
                elapsed_fit = perf_counter() - started
                prep = pipeline.named_steps["preprocessing"]
                before = preprocessing._state_hash(prep)
                started = perf_counter()
                probability_matrix = pipeline.predict_proba(valid)
                elapsed_predict = perf_counter() - started
                classifier = pipeline.named_steps["classifier"]
                positive_column = np.flatnonzero(classifier.classes_ == 1)
                if len(positive_column) != 1:
                    raise RuntimeError("The fitted classifier has no unique positive class.")
                probability = probability_matrix[:, positive_column[0]]
                prepared = prep.transform(valid)
                # Named features-only inference must be independent of ID/target.
                probe = valid.head(8)
                named_probe = preprocessing.transform_frame(prep, probe)
                features_only = preprocessing.transform_frame(prep, probe.loc[:, list(reversed(missingness.FEATURES))])
            if any(issubclass(item.category, ConvergenceWarning) for item in caught):
                raise RuntimeError(f"Model did not converge: {family}, fold {fold}.")
            if (not np.isfinite(probability_matrix).all() or
                    np.any((probability_matrix < 0) | (probability_matrix > 1)) or
                    not np.allclose(probability_matrix.sum(axis=1), 1) or not np.isfinite(prepared).all()):
                raise RuntimeError("Invalid probability/preprocessing output.")
            state = preprocessing.learned_state(prep)
            fill_error = float(np.max(np.abs(
                np.asarray(list(state["imputation_statistics"].values())) - fill_reference[fold])))
            mean_error = variance_error = None
            if state["scaler"]:
                training_values = prep[:-1].transform(train)
                scaler = prep.named_steps["scaler"]
                mean_error = float(np.max(np.abs(scaler.mean_ - training_values.mean(axis=0))))
                variance_error = float(np.max(np.abs(scaler.var_ - training_values.var(axis=0))))
            unchanged = before == preprocessing._state_hash(prep)
            metadata_error = float(np.max(np.abs(named_probe.to_numpy() - features_only.to_numpy())))
            width = len(missingness.FEATURES) + len(feature_map[family])
            if missingness.POLICIES[policy]["add_indicator"]:
                width += len(preprocessing.MISSING_FEATURES)
            active_errors = [fill_error, metadata_error] + ([mean_error, variance_error] if state["scaler"] else [])
            if (max(active_errors) > 1e-12 or not unchanged or prepared.shape[1] != width or
                    state["schema_fit_rows"] != len(train) or
                    prep.named_steps["business_features"].fit_rows_ != len(train) or
                    (state["scaler"] and state["scaler"]["fit_rows"] != len(train))):
                raise RuntimeError("A pipeline fit/schema boundary check failed.")
            probabilities[validation] = probability
            coverage[validation] += 1
            fit_seconds += elapsed_fit
            predict_seconds += elapsed_predict
            metrics = decision_metrics(target.iloc[validation], probability >= THRESHOLD, probability)
            family_costs.append(metrics["cost_per_1000"])
            results.append({"model": family, "fold": fold, **metrics,
                            "fit_seconds": elapsed_fit, "predict_seconds": elapsed_predict})
            audits.append({"model": family, "fold": fold, "training_records": len(train),
                "validation_records": len(valid), "row_overlap": 0, "group_overlap": 0,
                "output_columns": width, "scaled": bool(state["scaler"]),
                "training_fill_max_error": fill_error,
                "scaler_training_mean_max_error": mean_error,
                "scaler_training_variance_max_error": variance_error,
                "features_only_reordered_max_error": metadata_error,
                "parameters_unchanged_after_validation": unchanged,
                "training_ids_sha256": hashlib.sha256(train.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
                "validation_ids_sha256": hashlib.sha256(valid.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
                "fitted_preprocessing": state})
            if progress:
                progress(f"{family}, fold {fold}: cost/1000={metrics['cost_per_1000']:.3f}")
        if not np.all(coverage == 1) or not np.isfinite(probabilities).all():
            raise RuntimeError("Every candidate must score all development rows exactly once.")
        metrics = decision_metrics(target, probabilities >= THRESHOLD, probabilities)
        summaries.append({"model": family, "learned": True, "input_columns": width, **metrics,
            "fold_cost_std": float(np.std(family_costs, ddof=1)),
            "reduction_vs_baseline_percent": 100 * (baseline["cost"] - metrics["cost"]) / baseline["cost"],
            "meets_25_percent": bool(4 * metrics["cost"] <= 3 * baseline["cost"]),
            "fit_seconds": fit_seconds, "predict_seconds": predict_seconds})
        oof[family] = probabilities
    summary = pd.DataFrame(summaries)
    summary["_priority"] = summary.model.map({name: i for i, name in enumerate(FAMILIES)})
    summary = summary.sort_values(["cost", "_priority"]).drop(columns="_priority").reset_index(drop=True)
    return summary, pd.DataFrame(results), pd.DataFrame(audits), oof


def save_comparison(evidence_dir, *, database, partition, policy, feature_map,
                    baseline, baseline_folds, summary, fold_results, fit_audit, oof):
    """Publish comparison evidence and reusable OOF probabilities, not final scores."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    winner = summary.iloc[0].to_dict()
    family = winner["model"]
    candidate_parameters = {
        name: make_model_pipeline(family=name, features=feature_map[name], policy=policy)
              .named_steps["classifier"].get_params(deep=False)
        for name in FAMILIES
    }
    artifact = evidence_dir / "section_05_oof_predictions.npz"
    temporary = artifact.with_suffix(artifact.suffix + ".tmp")
    arrays = {
        "row_id": oof.row_id.to_numpy(dtype=np.int64), "fold": oof.fold.to_numpy(dtype=np.uint8),
        "target": oof.target.to_numpy(dtype=np.uint8),
        "source_fingerprint": np.asarray(partition["source_fingerprint"]),
        **{name: oof[name].to_numpy(dtype=float) for name in FAMILIES},
    }
    with temporary.open("wb") as stream:
        np.savez_compressed(stream, **arrays)
    temporary.replace(artifact)
    with np.load(artifact, allow_pickle=False) as loaded:
        if set(loaded.files) != set(arrays) or any(not np.array_equal(loaded[k], v) for k, v in arrays.items()):
            raise RuntimeError("Saved OOF predictions did not round-trip exactly.")
    receipt = {"file": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
               "records": len(oof), "probability_columns": list(FAMILIES),
               "row_order": "ascending source row_id; same for all models",
               "reload_max_error": 0.0, "final_holdout_included": False}
    selected = {
        "model": family, "features": list(feature_map[family]), "policy": policy,
        "model_parameters": candidate_parameters[family],
        "source_fingerprint": partition["source_fingerprint"],
        "reference_threshold": THRESHOLD,
        "baseline_cost_per_1000": baseline["cost_per_1000"],
        "development_cost_per_1000": winner["cost_per_1000"],
        "reduction_vs_baseline_percent": winner["reduction_vs_baseline_percent"],
        "meets_25_percent": bool(winner["meets_25_percent"]),
        "selection_rule": "Lowest pooled development OOF integer cost; exact ties use the declared model order.",
        "model_order": list(FAMILIES), "holdout_evaluations": 0,
        "usage": "Build a fresh make_model_pipeline with this model/features/policy for later fitting; no full-development fitted classifier is published here.",
        "scope": "Development candidate selection at a fixed reference threshold. Feature recipes were selected on this development set, so this is not unbiased final performance. Empirical threshold selection and final holdout evaluation remain pending.",
    }
    result = {
        "section": "Challenge 3 Section 5: no-learning baseline and model comparison",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"database": database, "table": "credit.accepted",
                   "source_fingerprint": partition["source_fingerprint"]},
        "development_records": len(oof), "holdout_records": len(partition["holdout_row_ids"]),
        "holdout_evaluations": 0,
        "cost_matrix": {"false_negative": FALSE_NEGATIVE_COST, "false_positive": FALSE_POSITIVE_COST,
                        "true_positive": 0, "true_negative": 0},
        "reference_threshold": THRESHOLD,
        "threshold_scope": "Fixed 1/13 for every learned model; no threshold search or calibration in Section 5. The deterministic baseline uses no probability threshold.",
        "baseline_rule": "Always predict class 0 (healthy), without fitting or reading target values to make predictions.",
        "baseline": baseline, "baseline_folds": missingness.records(baseline_folds),
        "candidate_features": {name: list(features) for name, features in feature_map.items()},
        "feature_policy": "Frozen Section 4 features for logistic/HGB; Random Forest uses the same frozen tree features as HGB, without a new feature search.",
        "imputation_policy": policy, "candidate_parameters": candidate_parameters,
        "scaling": {"logistic": "training-only log1p and StandardScaler for L2 regularization/optimization",
                    "random_forest": "no scaling/log transform; tree splits depend on feature ordering and thresholds",
                    "hist_gradient_boosting": "no scaling/log transform; histogram tree splits do not require standardization"},
        "summary": missingness.records(summary), "fold_results": missingness.records(fold_results),
        "fit_audit": missingness.records(fit_audit), "selected_candidate": selected,
        "acceptance_cost_per_1000": .75 * baseline["cost_per_1000"],
        "acceptance_explanation": ("The selected development candidate reduces the baseline cost by at least 25%."
            if selected["meets_25_percent"] else
            "The frozen model recipes at the fixed reference threshold did not reach a 25% development cost reduction; final acceptance is not claimed."),
        "oof_artifact": receipt,
        "versions": {"numpy": np.__version__, "pandas": pd.__version__, "scikit-learn": sklearn.__version__},
        "scope": selected["scope"],
        "data_changes": "No database, accepted CSV, reserved partition, earlier selected policies/features or prior evidence were modified.",
    }
    missingness.write_json(evidence_dir / "section_05_model_comparison.json", result)
    missingness.write_json(evidence_dir / "section_05_selected_model.json", selected)
    return result, selected
