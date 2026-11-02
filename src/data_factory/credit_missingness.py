"""Stage 03 Section 2: missingness diagnostics and fold-local policy comparison.

All analytical records come from PostgreSQL. The final holdout is reserved once
and is never scored here. This is a controlled policy study, not a final model.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
import scipy
from scipy.stats import fisher_exact, mannwhitneyu, norm
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler
from sqlalchemy import text
from threadpoolctl import threadpool_limits


TARGET = "SeriousDlqin2yrs"
FEATURES = (
    "RevolvingUtilizationOfUnsecuredLines", "age",
    "NumberOfTime30-59DaysPastDueNotWorse", "DebtRatio", "MonthlyIncome",
    "NumberOfOpenCreditLinesAndLoans", "NumberOfTimes90DaysLate",
    "NumberRealEstateLoansOrLines", "NumberOfTime60-89DaysPastDueNotWorse",
    "NumberOfDependents",
)
LATE_COLUMNS = (
    "NumberOfTime30-59DaysPastDueNotWorse",
    "NumberOfTime60-89DaysPastDueNotWorse", "NumberOfTimes90DaysLate",
)
POLICIES = {
    "median": {"strategy": "median", "add_indicator": False},
    "median_indicator": {"strategy": "median", "add_indicator": True},
    "most_frequent_indicator": {"strategy": "most_frequent", "add_indicator": True},
}
DECISION_THRESHOLD = 1 / 13


def write_json(path, value, *, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         indent=None if compact else 2,
                         separators=(",", ":") if compact else None) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def records(table):
    return json.loads(table.to_json(orient="records", double_precision=15, force_ascii=False))


def read_credit_data(engine):
    with engine.connect() as connection:
        database = connection.scalar(text("SELECT current_database()"))
        frame = pd.read_sql_query(text("SELECT * FROM credit.accepted ORDER BY row_id"),
                                  connection, dtype_backend="numpy_nullable")
    return frame, database


def prepare_features(frame):
    """Apply only the fixed Section 1 semantic rule, without learning statistics."""
    missing = set(FEATURES) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing source columns: {sorted(missing)}")
    features = frame.loc[:, list(FEATURES)].astype("float64").copy()
    features.loc[:, list(LATE_COLUMNS)] = features.loc[:, list(LATE_COLUMNS)].mask(
        features.loc[:, list(LATE_COLUMNS)].isin([96, 98])
    )
    if np.isinf(features.to_numpy()).any() or features.lt(0).any().any():
        raise ValueError("Accepted inputs must be finite/nonnegative, apart from missing values.")
    if "row_id" in frame.columns:
        features.index = pd.Index(frame.row_id.to_numpy(dtype="int64"), name="row_id")
    return features


def _source_fingerprint(frame):
    stable = frame.sort_values("row_id").loc[:, ["row_id", TARGET, *FEATURES]]
    return hashlib.sha256(pd.util.hash_pandas_object(stable, index=False).to_numpy().tobytes()).hexdigest()


def reserve_partition(frame, path, *, seed=42, n_splits=5):
    """Persist one approximately 20% grouped/stratified holdout and five dev folds.

    Identical semantic input vectors form a group, regardless of target or ID.
    Group labels are not person identifiers. Existing memberships are reused.
    """
    frame = frame.sort_values("row_id")
    if frame.row_id.isna().any() or not frame.row_id.is_unique:
        raise ValueError("The database must contain unique, nonmissing row identifiers.")
    if not frame[TARGET].isin([0, 1]).all():
        raise ValueError("A nonmissing binary target is required for stratification.")
    features = prepare_features(frame)
    target = frame[TARGET].to_numpy(dtype=int)
    groups = pd.util.hash_pandas_object(features, index=False).to_numpy()
    fingerprint = _source_fingerprint(frame)
    path = Path(path)
    if path.exists():
        partition = json.loads(path.read_text(encoding="utf-8"))
        if partition["source_fingerprint"] != fingerprint:
            raise ValueError("Database snapshot changed; the reserved holdout was not overwritten.")
        if partition["seed"] != seed or partition["n_splits"] != n_splits:
            raise ValueError("Split settings differ from the reserved partition; it was not overwritten.")
    else:
        outer = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed)
        dev_positions, holdout_positions = next(outer.split(features, target, groups))
        inner = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed + 1)
        development_folds = {}
        for fold, (_, valid_positions) in enumerate(inner.split(
                features.iloc[dev_positions], target[dev_positions], groups[dev_positions]), 1):
            development_folds[str(fold)] = features.index[dev_positions[valid_positions]].astype(int).tolist()
        partition = {
            "version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": fingerprint, "fingerprint_pandas_version": pd.__version__,
            "seed": seed, "n_splits": n_splits,
            "algorithm": "StratifiedGroupKFold: one of five outer folds reserved; five inner development folds",
            "grouping": "Identical semantic input vectors; excludes row_id and target",
            "holdout_row_ids": features.index[holdout_positions].astype(int).tolist(),
            "development_validation_row_ids": development_folds,
            "holdout_evaluations_in_section2": 0,
        }
        write_json(path, partition, compact=True)

    holdout_ids = set(partition["holdout_row_ids"])
    dev_lists = list(partition["development_validation_row_ids"].values())
    dev_flat = [row_id for items in dev_lists for row_id in items]
    if len(dev_flat) != len(set(dev_flat)) or holdout_ids.intersection(dev_flat):
        raise ValueError("Overlapping partition memberships.")
    if set(dev_flat) | holdout_ids != set(features.index):
        raise ValueError("Partition does not cover exactly this database snapshot.")
    group_by_id = pd.Series(groups, index=features.index)
    overlap = set(group_by_id.loc[dev_flat]).intersection(group_by_id.loc[list(holdout_ids)])
    if overlap:
        raise ValueError("Identical-feature groups cross the holdout boundary.")
    return partition


def development_data(frame, partition):
    """Return only development inputs/labels and predefined fold positions."""
    ids = sorted(row_id for values in partition["development_validation_row_ids"].values() for row_id in values)
    # Select development records before calculating analytical/learned statistics.
    selected = frame.set_index("row_id", drop=False).loc[ids]
    features = prepare_features(selected)
    target = pd.Series(selected[TARGET].to_numpy(dtype=int), index=features.index, name=TARGET)
    groups = pd.util.hash_pandas_object(features, index=False).to_numpy()
    positions = pd.Series(np.arange(len(features)), index=features.index)
    folds = []
    for fold, validation_ids in sorted(partition["development_validation_row_ids"].items(), key=lambda item: int(item[0])):
        valid_positions = positions.loc[validation_ids].to_numpy()
        train_positions = np.setdiff1d(np.arange(len(features)), valid_positions)
        if np.intersect1d(groups[train_positions], groups[valid_positions]).size:
            raise ValueError("Identical-feature groups cross a development fold boundary.")
        folds.append((int(fold), train_positions, valid_positions))
    return features, target, groups, folds


def _holm(values):
    values = np.asarray(values, dtype=float)
    order = np.argsort(values)
    adjusted = np.empty(len(values))
    adjusted[order] = np.minimum(1, np.maximum.accumulate(
        values[order] * np.arange(len(values), 0, -1)
    ))
    return adjusted


def _wilson(positive, total):
    if not total:
        return None, None
    z = norm.ppf(.975)
    proportion = positive / total
    center = (proportion + z * z / (2 * total)) / (1 + z * z / total)
    radius = z * np.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return 100 * (center - radius), 100 * (center + radius)


def missingness_diagnostics(features, target):
    missing = features.isna()
    summary = pd.DataFrame({"column": features.columns, "missing": missing.sum().to_numpy(),
                            "percent": 100 * missing.mean().to_numpy()})
    summary = summary.loc[summary.missing.gt(0)].reset_index(drop=True)
    masks = {
        "income_missing": missing.MonthlyIncome,
        "dependents_missing": missing.NumberOfDependents,
        "delinquency_sentinel": missing.loc[:, list(LATE_COLUMNS)].any(axis=1),
    }
    target_associations, feature_associations = [], []
    for name, mask in masks.items():
        a = int(target.loc[mask].eq(1).sum())
        b = int(mask.sum()) - a
        c = int(target.loc[~mask].eq(1).sum())
        d = int((~mask).sum()) - c
        result = fisher_exact([[a, b], [c, d]])
        low_missing, high_missing = _wilson(a, a + b)
        low_observed, high_observed = _wilson(c, c + d)
        target_associations.append({
            "pattern": name, "missing_records": a + b, "observed_records": c + d,
            "positive_missing": a, "positive_observed": c,
            "positive_rate_missing_percent": 100 * a / (a + b),
            "positive_rate_observed_percent": 100 * c / (c + d),
            "risk_difference_pp": 100 * (a / (a + b) - c / (c + d)),
            "missing_rate_wilson_low": low_missing, "missing_rate_wilson_high": high_missing,
            "observed_rate_wilson_low": low_observed, "observed_rate_wilson_high": high_observed,
            "odds_ratio": float(result.statistic), "p_value": float(result.pvalue),
        })
        for variable, values in {
            "age": features.age,
            "open_credit_lines": features.NumberOfOpenCreditLinesAndLoans,
            "log1p_debt_ratio": np.log1p(features.DebtRatio),
        }.items():
            first, second = values.loc[mask].dropna(), values.loc[~mask].dropna()
            rank = mannwhitneyu(first, second, alternative="two-sided", method="asymptotic")
            deviation = np.sqrt((first.var(ddof=1) + second.var(ddof=1)) / 2)
            feature_associations.append({
                "pattern": name, "observed_feature": variable,
                "median_when_missing": float(first.median()), "median_when_observed": float(second.median()),
                "standardized_mean_difference": float((first.mean() - second.mean()) / deviation) if deviation else 0.0,
                "rank_biserial": float(2 * rank.statistic / (len(first) * len(second)) - 1),
                "p_value": float(rank.pvalue),
            })
    target_table = pd.DataFrame(target_associations)
    feature_table = pd.DataFrame(feature_associations)
    target_table["p_holm"] = _holm(target_table.p_value)
    feature_table["p_holm"] = _holm(feature_table.p_value)
    patterns = pd.DataFrame({name: mask.astype(int) for name, mask in masks.items()})
    patterns["target"] = target
    joint = patterns.groupby(list(masks), dropna=False).agg(
        records=("target", "size"), positives=("target", "sum")
    ).reset_index()
    joint["positive_percent"] = 100 * joint.positives / joint.records
    return {"missing_summary": summary, "target_associations": target_table,
            "feature_associations": feature_table, "joint_patterns": joint}


def make_policy_preprocessor(policy):
    settings = POLICIES[policy]
    return Pipeline([
        ("imputer", SimpleImputer(**settings, keep_empty_features=True)),
        ("log1p", FunctionTransformer(np.log1p, feature_names_out="one-to-one")),
        ("scaler", StandardScaler()),
    ])


def make_reference_pipeline(policy):
    return Pipeline([
        ("preprocessing", make_policy_preprocessor(policy)),
        ("classifier", LogisticRegression(C=1.0, solver="lbfgs", max_iter=2000, tol=1e-6)),
    ])


def probability_metrics(target, probabilities):
    target = np.asarray(target, dtype=int)
    decision = probabilities >= DECISION_THRESHOLD
    false_negative = int(np.sum((target == 1) & ~decision))
    false_positive = int(np.sum((target == 0) & decision))
    true_positive = int(np.sum((target == 1) & decision))
    return {
        "records": len(target), "false_negative": false_negative, "false_positive": false_positive,
        "cost": 12 * false_negative + false_positive,
        "cost_per_1000": 1000 * (12 * false_negative + false_positive) / len(target),
        "roc_auc": float(roc_auc_score(target, probabilities)) if np.unique(target).size == 2 else None,
        "average_precision": float(average_precision_score(target, probabilities)) if target.sum() else None,
        "brier": float(brier_score_loss(target, probabilities)),
        "recall": true_positive / int(target.sum()) if target.sum() else None,
    }


def compare_policies(features, target, groups, folds, *, progress=print):
    fold_results, fit_audit, oof = [], [], {}
    for policy in POLICIES:
        probabilities = np.full(len(features), np.nan)
        for fold, train_positions, valid_positions in folds:
            train, valid = features.iloc[train_positions], features.iloc[valid_positions]
            pipeline = make_reference_pipeline(policy)
            with warnings.catch_warnings(record=True) as caught, threadpool_limits(limits=1):
                warnings.simplefilter("always", ConvergenceWarning)
                pipeline.fit(train, target.iloc[train_positions])
            convergence_warnings = [str(w.message) for w in caught if issubclass(w.category, ConvergenceWarning)]
            if convergence_warnings:
                raise RuntimeError(f"Reference classifier did not converge: {policy}, fold {fold}.")
            predictions = pipeline.predict_proba(valid)[:, 1]
            probabilities[valid_positions] = predictions
            metrics = probability_metrics(target.iloc[valid_positions], predictions)
            metrics.update({"policy": policy, "fold": fold, "training_records": len(train),
                            "output_features": int(pipeline.named_steps["classifier"].n_features_in_)})
            fold_results.append(metrics)
            preprocessing = pipeline.named_steps["preprocessing"]
            imputer = preprocessing.named_steps["imputer"]
            id_overlap = np.intersect1d(train.index.to_numpy(), valid.index.to_numpy()).size
            group_overlap = np.intersect1d(groups[train_positions], groups[valid_positions]).size
            fit_audit.append({
                "policy": policy, "fold": fold, "fit_records": len(train), "validation_records": len(valid),
                "id_overlap": int(id_overlap), "identical_input_group_overlap": int(group_overlap),
                "training_id_sha256": hashlib.sha256(train.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
                "validation_id_sha256": hashlib.sha256(valid.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
                "imputer_statistics": dict(zip(features.columns, imputer.statistics_.astype(float).tolist())),
                "indicator_columns": [str(features.columns[index]) for index in imputer.indicator_.features_] if imputer.add_indicator else [],
                "scaler_fit_samples": int(preprocessing.named_steps["scaler"].n_samples_seen_),
                "scaler_training_mean": preprocessing.named_steps["scaler"].mean_.tolist(),
                "classifier_iterations": int(pipeline.named_steps["classifier"].n_iter_.max()),
            })
            if progress:
                progress(f"{policy}, fold {fold}: cost/1000={metrics['cost_per_1000']:.3f}, AUC={metrics['roc_auc']:.4f}")
        if np.isnan(probabilities).any():
            raise RuntimeError("Every development row must receive exactly one out-of-fold prediction.")
        oof[policy] = probabilities

    fold_table = pd.DataFrame(fold_results)
    summaries, subgroups = [], []
    missing_any = features.isna().any(axis=1).to_numpy()
    for policy, probabilities in oof.items():
        summary = probability_metrics(target, probabilities)
        policy_folds = fold_table.loc[fold_table.policy.eq(policy)]
        summary.update({"policy": policy, "fold_cost_mean": float(policy_folds.cost_per_1000.mean()),
                        "fold_cost_std": float(policy_folds.cost_per_1000.std(ddof=1)),
                        "fold_auc_std": float(policy_folds.roc_auc.std(ddof=1)),
                        "output_features": int(policy_folds.output_features.max()),
                        "scored_coverage_percent": 100.0})
        summaries.append(summary)
        for label, mask in [("any_missing", missing_any), ("complete", ~missing_any)]:
            row = probability_metrics(target.to_numpy()[mask], probabilities[mask])
            row.update({"policy": policy, "subgroup": label})
            subgroups.append(row)
    summary_table = pd.DataFrame(summaries)
    summary_table["_tie_priority"] = summary_table.policy.map({name: rank for rank, name in enumerate(POLICIES)})
    summary_table = summary_table.sort_values(
        ["cost_per_1000", "output_features", "_tie_priority"]
    ).drop(columns="_tie_priority").reset_index(drop=True)
    paired = fold_table.pivot(index="fold", columns="policy", values="cost_per_1000").reset_index()
    for policy in POLICIES:
        if policy != "median":
            paired[f"{policy}_minus_median"] = paired[policy] - paired["median"]
    return {"summary": summary_table, "fold_results": fold_table,
            "paired_fold_costs": paired, "subgroup_results": pd.DataFrame(subgroups),
            "fit_audit": fit_audit, "selected_policy": str(summary_table.iloc[0].policy)}


def save_study(output_dir, partition, database, features, target, diagnostics, comparison):
    output_dir = Path(output_dir)
    selected = comparison["selected_policy"]
    # This development fit is for inspection/application after policy selection;
    # it is never reused as a pre-fitted transformer inside cross-validation.
    preprocessor = make_policy_preprocessor(selected)
    with threadpool_limits(limits=1):
        preprocessor.fit(features)
    imputer = preprocessor.named_steps["imputer"]
    choice = {
        "selected_policy": selected, **POLICIES[selected],
        "feature_columns": list(FEATURES), "sentinel_values": [96, 98],
        "sentinel_columns": list(LATE_COLUMNS),
        "selection_metric": "Pooled development OOF decision cost per 1000; lower is better",
        "tie_break": "Prefer fewer output features, then the declared policy order; median precedes most_frequent at equal cost.",
        "policy_priority": list(POLICIES),
        "decision_threshold": DECISION_THRESHOLD,
        "threshold_note": "Fixed cost-derived reference threshold; no validation/test threshold tuning",
        "common_transforms": ["log1p", "StandardScaler fitted within each training fold"],
        "source_fingerprint": partition["source_fingerprint"],
        "development_fit_statistics": dict(zip(features.columns, imputer.statistics_.astype(float).tolist())),
        "development_fit_records": len(features),
        "future_cv_rule": "Construct a fresh preprocessor and fit it inside every training fold; do not reuse the development-fitted object.",
        "holdout_evaluations": 0,
    }
    write_json(output_dir / "section_02_selected_policy.json", choice)
    result = {
        "section": "Challenge 3 Section 2: missingness", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"database": database, "table": "credit.accepted", "source_fingerprint": partition["source_fingerprint"]},
        "versions": {"numpy": np.__version__, "pandas": pd.__version__, "scipy": scipy.__version__, "scikit-learn": sklearn.__version__},
        "partition": {"seed": partition["seed"], "development_records": len(features),
                      "holdout_records": len(partition["holdout_row_ids"]), "holdout_evaluations": 0,
                      "development_positive_percent": 100 * float(target.mean()), "development_cv_folds": len(partition["development_validation_row_ids"]),
                      "grouping": partition["grouping"], "holdout_group_overlap": 0},
        "diagnostics": {name: records(value) for name, value in diagnostics.items()},
        "comparison": {name: records(comparison[name]) for name in ["summary", "fold_results", "paired_fold_costs", "subgroup_results"]},
        "fit_audit": comparison["fit_audit"], "selected_policy": choice,
        "statistical_scope": "Observed associations can contradict MCAR; these data cannot identify MAR versus MNAR or prove a causal missingness mechanism.",
        "selection_scope": "Development OOF scores compare three policies under one fixed reference classifier; selection scores are not unbiased final performance.",
        "prior_observation": "Section 1 described the whole table structurally. No claim is made that the holdout was never described before its reservation here.",
        "data_changes": "No database or accepted CSV changes; derived missing values and flags exist only in analysis/model inputs.",
    }
    write_json(output_dir / "section_02_missingness.json", result)
    return result, preprocessor
