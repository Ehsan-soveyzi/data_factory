"""Section 7: frozen-policy evaluation, development stability and cached holdout."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from time import perf_counter
import warnings

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                             precision_score, recall_score, roc_auc_score, roc_curve)
from threadpoolctl import threadpool_limits

from . import credit_missingness as missingness
from . import credit_preprocessing as preprocessing
from . import credit_threshold_selection as thresholding


MODEL_FILE = "section_07_fitted_classifier.joblib"
PREDICTION_FILE = "section_07_holdout_predictions.npz"
RESULT_FILE = "section_07_evaluation.json"
BOOTSTRAP_REPETITIONS = 1000
BOOTSTRAP_SEED = 42
METRIC_NOTES = (
    {"metric": "precision", "explanation": "سهم نکول واقعی بین افراد پرخطر اعلام‌شده؛ نکول‌های ازدست‌رفته را نشان نمی‌دهد."},
    {"metric": "recall", "explanation": "سهم نکول‌هایی که شناسایی شدند؛ رد اشتباهی افراد سالم را نشان نمی‌دهد."},
    {"metric": "f1", "explanation": "توازن Precision و Recall؛ نسبت هزینه ۱۲ به ۱ را لحاظ نمی‌کند."},
    {"metric": "roc_auc", "explanation": "توان رتبه‌بندی سالم و پرخطر در همه آستانه‌ها؛ هزینه تصمیم در آستانه منتخب را نشان نمی‌دهد."},
    {"metric": "accuracy", "explanation": "سهم کل تصمیم‌های درست؛ فراوانی افراد سالم می‌تواند ضعف تشخیص نکول را پنهان کند."},
    {"metric": "cost_per_1000", "explanation": "زیان موزون به ازای هزار متقاضی؛ به ماتریس هزینه و ترکیب متقاضی‌ها وابسته است."},
)
METRICS = tuple(row["metric"] for row in METRIC_NOTES)


def _recipe_hash(selection):
    raw = json.dumps(selection, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def binary_metrics(target, probabilities, selection):
    y, p = thresholding._validated_vectors(target, probabilities)
    predicted = (p >= selection["threshold"]).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel().tolist()
    cost = selection["fn_cost"] * fn + selection["fp_cost"] * fp
    return {"records": len(y), "positives": int(np.sum(y)), "positive_rate": float(np.mean(y)),
            "true_negative": tn, "false_positive": fp, "false_negative": fn, "true_positive": tp,
            "confusion_matrix": [[tn, fp], [fn, tp]], "positive_label": 1,
            "precision": float(precision_score(y, predicted, pos_label=1, zero_division=0)),
            "recall": float(recall_score(y, predicted, pos_label=1, zero_division=0)),
            "f1": float(f1_score(y, predicted, pos_label=1, zero_division=0)),
            "roc_auc": float(roc_auc_score(y, p)), "accuracy": float(accuracy_score(y, predicted)),
            "false_positive_rate": fp / (tn + fp), "false_negative_rate": fn / (tp + fn),
            "cost": int(cost), "cost_per_1000": 1000 * cost / len(y)}


def development_evaluation(evidence_dir):
    """Only the previously selected primary policy and saved development OOF."""
    evidence_dir = Path(evidence_dir)
    choices = json.loads((evidence_dir / "section_06_selected_decision.json").read_text(encoding="utf-8"))
    selection = choices["primary"]
    if selection["fn_cost"] != 12 or selection["fp_cost"] != 1:
        raise ValueError("Final evaluation uses the original 12:1 primary cost matrix.")
    thresholding.make_decision_estimator(selection)  # validate precision/recipe, without fit
    oof, source, partition, integrity = thresholding.load_predictions(evidence_dir)
    if (selection["source_fingerprint"] != partition["source_fingerprint"] or
            selection["oof_sha256"] != integrity["oof_sha256"]):
        raise ValueError("The selected policy and OOF snapshot differ.")
    p = oof[selection["model"]].to_numpy()
    pooled = binary_metrics(oof.target, p, selection)
    if not np.isclose(pooled["cost_per_1000"], selection["development_cost_per_1000"]):
        raise ValueError("The selected threshold does not reproduce its development cost.")
    folds = []
    for fold in sorted(oof.fold.unique()):
        mask = oof.fold.eq(fold).to_numpy()
        folds.append({"fold": int(fold), **binary_metrics(oof.target.to_numpy()[mask], p[mask], selection)})
    fold_table = pd.DataFrame(folds)
    stability = [{"metric": name, "mean": float(fold_table[name].mean()),
                  "std": float(fold_table[name].std(ddof=1)), "min": float(fold_table[name].min()),
                  "max": float(fold_table[name].max())} for name in METRICS]
    development = {"pooled": pooled, "folds": folds, "stability": stability,
        "scope": "Descriptive variation on selection development data: features/model/threshold were chosen using this population. Not an independent nested-CV estimate."}
    return selection, oof, source, partition, development


def _train(raw, target, selection, database):
    """This function receives development data only, with no holdout input."""
    estimator = thresholding.make_decision_estimator(selection)
    with warnings.catch_warnings(record=True) as caught, threadpool_limits(limits=1):
        warnings.simplefilter("always", ConvergenceWarning)
        started = perf_counter()
        estimator.fit(raw, target)
        seconds = perf_counter() - started
        prep = estimator.estimator_.named_steps["preprocessing"]
        before = preprocessing._state_hash(prep)
        probe = raw.head(32).loc[:, list(missingness.FEATURES)]
        probabilities = estimator.predict_proba(probe)[:, np.flatnonzero(estimator.classes_ == 1)[0]]
        prediction = estimator.predict(probe)
    if any(issubclass(w.category, ConvergenceWarning) for w in caught):
        raise RuntimeError("The frozen final candidate did not converge.")
    state = preprocessing.learned_state(prep)
    clean = preprocessing.semantic_inputs(raw)
    strategy = missingness.POLICIES[selection["policy"]]["strategy"]
    expected = clean.median() if strategy == "median" else clean.mode().iloc[0]
    fill_error = float(np.max(np.abs(np.asarray(list(state["imputation_statistics"].values())) - expected.to_numpy())))
    mean_error = variance_error = None
    if state["scaler"]:
        values = prep[:-1].transform(raw)
        mean_error = float(np.max(np.abs(prep.named_steps["scaler"].mean_ - values.mean(axis=0))))
        variance_error = float(np.max(np.abs(prep.named_steps["scaler"].var_ - values.var(axis=0))))
    if (fill_error > 1e-12 or state["schema_fit_rows"] != len(raw) or
            prep.named_steps["business_features"].fit_rows_ != len(raw) or
            any(e is not None and e > 1e-12 for e in [mean_error, variance_error]) or
            before != preprocessing._state_hash(prep) or
            not np.array_equal(prediction, probabilities >= selection["threshold"])):
        raise RuntimeError("Final training-only preprocessing or native threshold audit failed.")
    metadata = {"selection": selection, "recipe_sha256": _recipe_hash(selection),
        "source_fingerprint": selection["source_fingerprint"], "database": database,
        "fit_records": len(raw), "fit_partition": "development_only", "model_fit_count": 1,
        "training_ids_sha256": hashlib.sha256(raw.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
        "fit_seconds": seconds, "fitted_preprocessing": state,
        "training_fill_max_error": fill_error, "scaler_mean_max_error": mean_error,
        "scaler_variance_max_error": variance_error,
        "development_probe_threshold_rule_verified": True,
        "threshold_hex": float(estimator.threshold).hex(),
        "bootstrap_repetitions": BOOTSTRAP_REPETITIONS, "bootstrap_seed": BOOTSTRAP_SEED,
        "versions": {"numpy": np.__version__, "pandas": pd.__version__,
                     "scikit-learn": sklearn.__version__, "joblib": joblib.__version__}}
    return {"classifier": estimator, "metadata": metadata}, probe, probabilities


def _load_predictions(path, selection, partition, model_sha):
    with np.load(path, allow_pickle=False) as saved:
        arrays = {key: saved[key].copy() for key in saved.files}
    if (str(arrays.pop("recipe_sha256")) != _recipe_hash(selection) or
            str(arrays.pop("source_fingerprint")) != selection["source_fingerprint"] or
            str(arrays.pop("model_sha256")) != model_sha):
        raise ValueError("Final predictions belong to a different frozen policy/model.")
    expected = sorted(partition["holdout_row_ids"])
    if arrays["row_id"].tolist() != expected or any(v.shape != (len(expected),) for v in arrays.values()):
        raise ValueError("Final prediction cache does not match the exact reserved holdout.")
    y, p = thresholding._validated_vectors(arrays["target"], arrays["probability"])
    if not np.array_equal(arrays["prediction"], p >= selection["threshold"]):
        raise ValueError("Cached decisions do not match the exact frozen threshold.")
    return pd.DataFrame(arrays)


def _bootstrap_cost(predictions, selection):
    """Cluster bootstrap conditional on this fixed model; do not fit/tune anything."""
    _, inverse = np.unique(predictions.group_id.to_numpy(dtype=np.uint64), return_inverse=True)
    y, pred = predictions.target.to_numpy(), predictions.prediction.to_numpy()
    loss = selection["fn_cost"] * ((y == 1) & (pred == 0)) + selection["fp_cost"] * ((y == 0) & (pred == 1))
    baseline_loss = selection["fn_cost"] * (y == 1)
    sizes = np.bincount(inverse)
    costs = np.bincount(inverse, weights=loss)
    baseline_costs = np.bincount(inverse, weights=baseline_loss)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    model_samples, baseline_samples, reductions = [], [], []
    for offset in range(0, BOOTSTRAP_REPETITIONS, 32):
        count = min(32, BOOTSTRAP_REPETITIONS - offset)
        positions = rng.integers(0, len(sizes), size=(count, len(sizes)))
        total_records = sizes[positions].sum(axis=1)
        model_cost = costs[positions].sum(axis=1)
        baseline_cost = baseline_costs[positions].sum(axis=1)
        model_samples.extend((1000 * model_cost / total_records).tolist())
        baseline_samples.extend((1000 * baseline_cost / total_records).tolist())
        reductions.extend((100 * (baseline_cost - model_cost) / baseline_cost).tolist())
    def interval(values):
        return [float(v) for v in np.percentile(values, [2.5, 97.5])]
    return {"method": "Percentile bootstrap of identical-semantic-input groups, resampled with replacement",
        "groups": len(sizes), "repetitions": BOOTSTRAP_REPETITIONS, "seed": BOOTSTRAP_SEED,
        "confidence_level": .95, "cost_per_1000_interval": interval(model_samples),
        "baseline_cost_per_1000_interval": interval(baseline_samples),
        "reduction_percent_interval": interval(reductions),
        "scope": "Conditional on the single frozen fitted model and observed holdout groups; training/selection uncertainty is not included."}


def _gap(development, final, uncertainty):
    p_dev, p_final = development["positive_rate"], final["positive_rate"]
    prevalence = 1000 * (p_final - p_dev) * (12 * development["false_negative_rate"] - development["false_positive_rate"])
    missed = 1000 * 12 * p_final * (final["false_negative_rate"] - development["false_negative_rate"])
    false_alarms = 1000 * (1 - p_final) * (final["false_positive_rate"] - development["false_positive_rate"])
    difference = final["cost_per_1000"] - development["cost_per_1000"]
    if not np.isclose(prevalence + missed + false_alarms, difference, atol=1e-10):
        raise RuntimeError("The final/development cost-gap decomposition does not reconcile.")
    lo, hi = uncertainty["cost_per_1000_interval"]
    return {"cost_change_per_1000": difference,
            "relative_cost_change_percent": 100 * difference / development["cost_per_1000"],
            "auc_change": final["roc_auc"] - development["roc_auc"],
            "prevalence_contribution": prevalence, "missed_default_contribution": missed,
            "false_alarm_contribution": false_alarms,
            "development_cost_inside_final_bootstrap_interval": lo <= development["cost_per_1000"] <= hi,
            "scope": "Descriptive decomposition, not a causal explanation or an independent significance test. OOF used for selection; CV models trained on about 96k rows versus 120k in the final model."}


def evaluate_once(engine, evidence_dir):
    """One frozen final evaluation; subsequent notebook runs reuse cached results."""
    evidence_dir = Path(evidence_dir)
    selection, oof, source, partition, development = development_evaluation(evidence_dir)
    recipe_sha = _recipe_hash(selection)
    result_path, model_path, prediction_path = (evidence_dir / RESULT_FILE, evidence_dir / MODEL_FILE,
                                               evidence_dir / PREDICTION_FILE)
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result["recipe_sha256"] != recipe_sha:
            raise ValueError("Final evaluation is already frozen for another policy; it must not be overwritten.")
        if (_file_hash(model_path) != result["model_artifact"]["sha256"] or
                _file_hash(prediction_path) != result["prediction_artifact"]["sha256"]):
            raise ValueError("Final artifacts changed; restore them instead of reevaluating the holdout.")
        bundle = joblib.load(model_path)
        predictions = _load_predictions(prediction_path, selection, partition, _file_hash(model_path))
        return result, bundle["classifier"], predictions, True

    lock = evidence_dir / ".section_07_evaluation.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise RuntimeError("A final evaluation is already active; preserve the existing artifacts.") from None
    try:
        with handle:
            handle.write(recipe_sha)
        if prediction_path.exists() and not model_path.exists():
            raise ValueError("The final prediction cache exists without its frozen model; restore that model.")
        if model_path.exists():
            bundle = joblib.load(model_path)
            if bundle["metadata"]["recipe_sha256"] != recipe_sha:
                raise ValueError("The persisted final model belongs to a different frozen decision.")
        else:
            bundle = None

        if not prediction_path.exists():
            # One database snapshot; only its development subset is passed to fit.
            frame, database = missingness.read_credit_data(engine)
            checked = missingness.reserve_partition(frame, evidence_dir / "section_02_partition.json",
                seed=partition["seed"], n_splits=partition["n_splits"])
            if checked["source_fingerprint"] != selection["source_fingerprint"]:
                raise ValueError("Database snapshot changed after model/threshold selection.")
            indexed = frame.set_index("row_id", drop=False)
            raw = indexed.loc[oof.row_id.to_numpy()].copy()
            if not np.array_equal(raw[missingness.TARGET].to_numpy(dtype=int), oof.target.to_numpy(dtype=int)):
                raise ValueError("Development labels differ from the frozen OOF snapshot.")
            if bundle is None:
                target = pd.Series(oof.target.to_numpy(dtype=int), index=raw.index, name=missingness.TARGET)
                bundle, probe, before = _train(raw, target, selection, database)
                temporary = model_path.with_suffix(model_path.suffix + ".tmp")
                joblib.dump(bundle, temporary, compress=3)
                temporary.replace(model_path)
                bundle = joblib.load(model_path)
                with threadpool_limits(limits=1):
                    after = bundle["classifier"].predict_proba(probe)[:, 1]
                if not np.array_equal(before, after):
                    raise RuntimeError("The fitted classifier changed after serialization.")
            classifier = bundle["classifier"]
            prep = classifier.estimator_.named_steps["preprocessing"]
            state_before = preprocessing._state_hash(prep)
            holdout = indexed.loc[sorted(partition["holdout_row_ids"])].copy()
            if set(raw.index).intersection(holdout.index):
                raise RuntimeError("Development and final holdout overlap.")
            with threadpool_limits(limits=1):
                probabilities = classifier.predict_proba(holdout.loc[:, list(missingness.FEATURES)])
            positive = int(np.flatnonzero(classifier.classes_ == 1)[0])
            p = probabilities[:, positive]
            if (not np.isfinite(probabilities).all() or np.any((probabilities < 0) | (probabilities > 1)) or
                    not np.allclose(probabilities.sum(axis=1), 1) or
                    state_before != preprocessing._state_hash(prep)):
                raise RuntimeError("Final inference changed preprocessing or produced invalid probabilities.")
            groups = pd.util.hash_pandas_object(missingness.prepare_features(holdout), index=False).to_numpy(dtype=np.uint64)
            arrays = {"row_id": holdout.index.to_numpy(dtype=np.int64),
                "target": holdout[missingness.TARGET].to_numpy(dtype=np.uint8), "probability": p,
                "prediction": (p >= selection["threshold"]).astype(np.uint8), "group_id": groups,
                "recipe_sha256": np.asarray(recipe_sha),
                "source_fingerprint": np.asarray(selection["source_fingerprint"]),
                "model_sha256": np.asarray(_file_hash(model_path))}
            temporary = prediction_path.with_suffix(prediction_path.suffix + ".tmp")
            with temporary.open("wb") as stream:
                np.savez_compressed(stream, **arrays)
            temporary.replace(prediction_path)

        model_sha = _file_hash(model_path)
        predictions = _load_predictions(prediction_path, selection, partition, model_sha)
        final = binary_metrics(predictions.target, predictions.probability, selection)
        positives = final["positives"]
        baseline = {"rule": "always healthy (class 0)", "false_negative": positives, "false_positive": 0,
                    "accuracy": 1 - positives / final["records"], "recall": 0.0,
                    "cost": 12 * positives, "cost_per_1000": 1000 * 12 * positives / final["records"]}
        uncertainty = _bootstrap_cost(predictions, selection)
        result = {"section": "Challenge 3 Section 7: full evaluation and stability",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "recipe_sha256": recipe_sha, "selection": selection,
            "source": source["source"], "development_records": len(oof),
            "holdout_records": final["records"], "holdout_evaluations": 1, "model_fits": 1,
            "model_artifact": {"file": MODEL_FILE, "sha256": model_sha},
            "prediction_artifact": {"file": PREDICTION_FILE, "sha256": _file_hash(prediction_path)},
            "training_audit": bundle["metadata"], "development": development, "final": final,
            "baseline_final": baseline,
            "reduction_vs_final_baseline_percent": 100 * (baseline["cost"] - final["cost"]) / baseline["cost"],
            "meets_25_percent_on_final": bool(4 * final["cost"] <= 3 * baseline["cost"]),
            "uncertainty": uncertainty, "gap_analysis": _gap(development["pooled"], final, uncertainty),
            "metric_notes": list(METRIC_NOTES),
            "policy": "Primary 12:1 policy frozen before holdout scoring; no alternative model/threshold was evaluated on holdout. Subsequent runs reload the same cached result without DB access, training or inference.",
            "scope": "Development OOF metrics are descriptive selection metrics; final holdout metrics evaluate the one frozen fitted model. No post-holdout model/threshold adjustment is performed.",
            "data_changes": "No database/CSV or earlier evidence changes; a fitted decision model and its one-time final predictions were saved."}
        missingness.write_json(result_path, result)
        return result, bundle["classifier"], predictions, False
    finally:
        lock.unlink(missing_ok=True)


def metric_table(result):
    return pd.DataFrame([{"metric": name, "development_oof": result["development"]["pooled"][name],
                          "final_holdout": result["final"][name]} for name in METRICS])


def plot_evaluation(result, predictions, path):
    import matplotlib.pyplot as plt
    from sklearn.metrics import ConfusionMatrixDisplay

    with plt.style.context("dark_background"):
        figure, axes = plt.subplots(1, 3, figsize=(13, 4), constrained_layout=True)
        ConfusionMatrixDisplay(np.asarray(result["final"]["confusion_matrix"]),
            display_labels=["Healthy (0)", "Default (1)"]).plot(
                ax=axes[0], cmap="Blues", colorbar=False, values_format="d")
        axes[0].set_title("Final holdout: absolute counts")
        fpr, tpr, _ = roc_curve(predictions.target, predictions.probability, pos_label=1)
        axes[1].plot(fpr, tpr, label=f"AUC = {result['final']['roc_auc']:.4f}", color="#66b3ff")
        axes[1].plot([0, 1], [0, 1], "--", color="#888888", linewidth=1)
        axes[1].scatter(result["final"]["false_positive_rate"], result["final"]["recall"],
                        color="#ffb454", label="Frozen threshold", zorder=3)
        axes[1].set(xlabel="False positive rate", ylabel="True positive rate", title="Final holdout ROC")
        axes[1].legend(fontsize=8)
        folds = result["development"]["folds"]
        axes[2].bar([r["fold"] for r in folds], [r["cost_per_1000"] for r in folds], color="#67cf9d")
        axes[2].axhline(result["final"]["cost_per_1000"], color="#ffb454", linestyle="--", label="Final holdout")
        axes[2].set(xlabel="Development fold", ylabel="Cost per 1,000 applicants",
                    title="Selection-data variation", xticks=[1, 2, 3, 4, 5])
        axes[2].legend(fontsize=8)
        for axis in axes[1:]:
            axis.grid(alpha=.15)
        figure.savefig(path, dpi=160, facecolor=figure.get_facecolor())
        plt.close(figure)
