"""Section 4: auditable credit features and development-only subset comparison."""

from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
import sklearn
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import roc_auc_score
from sklearn.utils.validation import check_is_fitted
from threadpoolctl import threadpool_limits

from . import credit_missingness as missingness
from . import credit_preprocessing as preprocessing


FEATURE_DEFINITIONS = (
    {
        "name": "TotalDelinquencyCount",
        "label": "مجموع دیرکردها",
        "sources": list(missingness.LATE_COLUMNS),
        "formula": "late30 + late60 + late90",
        "hypothesis": "تجمیع شمارش‌های دیرکرد، نشانه ریسک را بهتر از هر ستون به‌تنهایی نشان می‌دهد.",
        "availability": "سابقه اعتباری موجود هنگام درخواست وام؛ بدون استفاده از پیامد دو سال بعد.",
    },
    {
        "name": "SeverityWeightedDelinquency",
        "label": "دیرکرد با وزن شدت",
        "sources": list(missingness.LATE_COLUMNS),
        "formula": "late30 + 2*late60 + 3*late90",
        "hypothesis": "تکرار دیرکردهای شدیدتر، نشانه ریسک بیشتری است؛ وزن‌های ۱، ۲ و ۳ از پیش ثابت‌اند.",
        "availability": "همان سابقه پیش از تصمیم؛ وزن‌ها از برچسب یا داده ارزیابی آموخته نمی‌شوند.",
    },
    {
        "name": "RealEstateToOpenCreditRatio",
        "label": "نسبت وام ملکی به خطوط اعتباری",
        "sources": ["NumberRealEstateLoansOrLines", "NumberOfOpenCreditLinesAndLoans"],
        "formula": "real_estate / open_credit if open_credit > 0 else 0",
        "hypothesis": "ترکیب وام‌های ملکی و سایر خطوط اعتباری با ریسک ارتباط دارد؛ مخرج صفر، خروجی صفر دارد.",
        "availability": "ترکیب تعهدات موجود هنگام درخواست؛ بدون استفاده از نتیجه وام جدید.",
    },
    {
        "name": "UtilizationDebtInteraction",
        "label": "ترکیب استفاده از اعتبار و بدهی",
        "sources": ["RevolvingUtilizationOfUnsecuredLines", "DebtRatio"],
        "formula": "utilization * debt_ratio",
        "hypothesis": "هم‌زمانی استفاده زیاد از اعتبار و بار بدهی بالا، ریسک مضاعفی را نشان می‌دهد.",
        "availability": "نسبت‌های ثبت‌شده در پرونده هنگام تصمیم؛ بدون اطلاعات بازپرداخت آینده.",
    },
)
CANDIDATES = tuple(item["name"] for item in FEATURE_DEFINITIONS)
FALSE_NEGATIVE_COST = 12
FALSE_POSITIVE_COST = 1
THRESHOLD = missingness.DECISION_THRESHOLD


def candidate_subsets():
    return tuple(subset for size in range(len(CANDIDATES) + 1)
                 for subset in combinations(CANDIDATES, size))


def _canonical(features):
    if len(features) != len(set(features)) or set(features) - set(CANDIDATES):
        raise ValueError("Unknown or duplicate engineered feature names.")
    return tuple(name for name in CANDIDATES if name in features)


def subset_key(features):
    return "+".join(_canonical(features)) or "original"


def _input_names(policy):
    names = list(missingness.FEATURES)
    if missingness.POLICIES[policy]["add_indicator"]:
        names += ["missingindicator_" + name for name in preprocessing.MISSING_FEATURES]
    return tuple(names)


class CreditBusinessFeatures(TransformerMixin, BaseEstimator):
    """Append row-local features after training-only imputation, before scaling."""

    def __init__(self, *, features=(), input_names=missingness.FEATURES):
        self.features = features
        self.input_names = input_names

    def _values(self, X):
        values = np.asarray(X, dtype=float)
        if values.ndim != 2 or values.shape[1] != len(self.input_names):
            raise ValueError("Unexpected preprocessed input width.")
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError("Business features require finite nonnegative imputed inputs.")
        return values

    def fit(self, X, y=None):
        selected = _canonical(self.features)
        if tuple(self.input_names[:len(missingness.FEATURES)]) != missingness.FEATURES:
            raise ValueError("The first ten columns must match the accepted feature order.")
        values = self._values(X)
        if not len(values):
            raise ValueError("Cannot fit business features on an empty training set.")
        self.n_features_in_ = values.shape[1]
        self.feature_names_in_ = np.asarray(self.input_names, dtype=object)
        self.feature_names_out_ = np.asarray((*self.input_names, *selected), dtype=object)
        self.fit_rows_ = len(values)
        return self

    def transform(self, X):
        check_is_fitted(self, "feature_names_out_")
        values = self._values(X)
        selected = _canonical(self.features)
        if not selected:
            return values.copy()
        index = {name: i for i, name in enumerate(missingness.FEATURES)}
        late30 = values[:, index["NumberOfTime30-59DaysPastDueNotWorse"]]
        late60 = values[:, index["NumberOfTime60-89DaysPastDueNotWorse"]]
        late90 = values[:, index["NumberOfTimes90DaysLate"]]
        real_estate = values[:, index["NumberRealEstateLoansOrLines"]]
        open_credit = values[:, index["NumberOfOpenCreditLinesAndLoans"]]
        derived = {
            "TotalDelinquencyCount": late30 + late60 + late90,
            "SeverityWeightedDelinquency": late30 + 2 * late60 + 3 * late90,
            "RealEstateToOpenCreditRatio": np.divide(
                real_estate, open_credit, out=np.zeros_like(real_estate), where=open_credit > 0),
            "UtilizationDebtInteraction": (
                values[:, index["RevolvingUtilizationOfUnsecuredLines"]] * values[:, index["DebtRatio"]]),
        }
        output = np.column_stack([values, *[derived[name] for name in selected]])
        if not np.isfinite(output).all():
            raise ValueError("Business feature arithmetic produced a nonfinite value.")
        return output

    def get_feature_names_out(self, input_features=None):
        check_is_fitted(self, "feature_names_out_")
        if input_features is not None and tuple(input_features) != tuple(self.input_names):
            raise ValueError("Input feature names changed.")
        return self.feature_names_out_.copy()


def make_feature_pipeline(*, family="logistic", features=(), policy="median_indicator"):
    """Return a fresh sklearn model Pipeline; never reuse a fitted CV artifact."""
    selected = _canonical(features)
    pipeline = preprocessing.make_model_pipeline(family=family, policy=policy)
    preparation = pipeline.named_steps["preprocessing"]
    preparation.steps.insert(2, ("business_features", CreditBusinessFeatures(
        features=selected, input_names=_input_names(policy))))
    return pipeline


def _metrics(target, probabilities):
    prediction = probabilities >= THRESHOLD
    actual = np.asarray(target, dtype=int)
    false_negative = int(np.sum((actual == 1) & ~prediction))
    false_positive = int(np.sum((actual == 0) & prediction))
    cost = FALSE_NEGATIVE_COST * false_negative + FALSE_POSITIVE_COST * false_positive
    return {"records": len(actual), "false_negative": false_negative,
            "false_positive": false_positive, "cost": cost,
            "cost_per_1000": 1000 * cost / len(actual),
            "roc_auc": float(roc_auc_score(actual, probabilities))}


def _state(preprocessor):
    return preprocessing._state_hash(preprocessor)


def audit_feature_contract(raw, folds, policy):
    """Inspect formulas, source lineage, metadata exclusion and transform-only use."""
    _, training, validation = folds[0]
    preprocessor = make_feature_pipeline(features=CANDIDATES, policy=policy).named_steps["preprocessing"]
    with threadpool_limits(limits=1):
        preprocessor.fit(raw.iloc[training])
    probe = raw.iloc[validation].head(32).copy()
    before = _state(preprocessor)
    expected = preprocessing.transform_frame(preprocessor, probe)
    metadata = probe.copy()
    metadata[missingness.TARGET] = 1 - metadata[missingness.TARGET]
    metadata["row_id"] = np.arange(len(probe)) + 10_000_000
    altered = preprocessing.transform_frame(preprocessor, metadata)
    reordered = preprocessing.transform_frame(
        preprocessor, probe.loc[:, list(reversed(missingness.FEATURES))])
    metadata_error = float(np.max(np.abs(expected.to_numpy() - altered.to_numpy())))
    order_error = float(np.max(np.abs(expected.to_numpy() - reordered.to_numpy())))

    edge = probe.head(2).copy()
    edge["NumberOfTime30-59DaysPastDueNotWorse"] = [2, 96]
    edge["NumberOfTime60-89DaysPastDueNotWorse"] = [3, 98]
    edge["NumberOfTimes90DaysLate"] = [4, 96]
    edge["NumberOfOpenCreditLinesAndLoans"] = 0
    edge["NumberRealEstateLoansOrLines"] = 3
    edge["RevolvingUtilizationOfUnsecuredLines"] = .5
    edge["DebtRatio"] = 2.
    # Examine business features before log/scaling, using the actual fitted imputer.
    prepared_edge = preprocessor[:3].transform(edge)
    imputer = preprocessor.named_steps["columns"].named_transformers_["values"]
    fills = dict(zip(missingness.FEATURES, imputer.statistics_))
    expected_edge = np.asarray([
        [9., 20., 0., 1.],
        [sum(fills[name] for name in missingness.LATE_COLUMNS),
         fills[missingness.LATE_COLUMNS[0]] + 2 * fills[missingness.LATE_COLUMNS[1]] +
         3 * fills[missingness.LATE_COLUMNS[2]], 0., 1.],
    ])
    formula_error = float(np.max(np.abs(prepared_edge[:, -len(CANDIDATES):] - expected_edge)))
    # Changing the other rows in an inference batch must not change this row.
    alone = preprocessing.transform_frame(preprocessor, probe.head(1))
    batch = probe.copy()
    batch.loc[batch.index[1:], "DebtRatio"] = 1e9
    first_with_other_rows = preprocessing.transform_frame(preprocessor, batch).iloc[[0]]
    batch_error = float(np.max(np.abs(alone.to_numpy() - first_with_other_rows.to_numpy())))
    unchanged = before == _state(preprocessor)
    if metadata_error or order_error or formula_error or batch_error or not unchanged:
        raise RuntimeError("Feature lineage/formula/transform-boundary audit failed.")
    sources_valid = all(set(item["sources"]).issubset(missingness.FEATURES)
                        for item in FEATURE_DEFINITIONS)
    if not sources_valid:
        raise RuntimeError("A business feature depends on an unapproved source column.")
    return {
        "feature_definitions": list(FEATURE_DEFINITIONS),
        "approved_sources_only": sources_valid, "target_and_id_max_error": metadata_error,
        "reordered_features_max_error": order_error, "formula_edge_cases_max_error": formula_error,
        "other_inference_rows_max_error": batch_error,
        "state_unchanged_after_transform": unchanged,
        "sentinels_masked_before_training_only_imputation": True,
        "division_by_zero_result": 0,
        "learned_feature_parameters": [],
        "temporal_assumption": "Input credit-file attributes are available at application time, as assumed by the challenge. The supplied data has no observation timestamps to independently verify this; the dictionary also does not explicitly date the 90+ delinquency count. Aggregates are indices, not counts over a verified common observation window.",
        "future_outcome_used_as_input": False,
    }


def compare_feature_subsets(raw, target, groups, folds, *, policy="median_indicator", progress=None):
    """Evaluate all 16 predefined subsets on the same five development folds."""
    # pandas nullable Int64 and NumPy int64 indexes may fail Index.equals
    # despite containing the same ordered source IDs.
    if not np.array_equal(raw.index.to_numpy(), target.index.to_numpy()) or not raw.index.is_unique:
        raise ValueError("Development inputs/target must have identical unique row indexes.")
    if len(groups) != len(raw) or len(folds) != 5:
        raise ValueError("The saved five-fold development partition is required.")
    fold_reference = {}
    all_validation = []
    for fold, train_positions, valid_positions in folds:
        row_overlap = int(np.intersect1d(raw.index[train_positions], raw.index[valid_positions]).size)
        group_overlap = int(np.intersect1d(groups[train_positions], groups[valid_positions]).size)
        if row_overlap or group_overlap:
            raise ValueError("Row/group overlap in the supplied development folds.")
        all_validation.extend(valid_positions)
        clean = preprocessing.semantic_inputs(raw.iloc[train_positions])
        strategy = missingness.POLICIES[policy]["strategy"]
        reference = clean.median() if strategy == "median" else clean.mode().iloc[0]
        fold_reference[fold] = reference.to_numpy(dtype=float)
    if sorted(all_validation) != list(range(len(raw))):
        raise ValueError("Validation folds must cover development exactly once.")

    summaries, fold_results, fit_audit = [], [], []
    first_preprocessors = {}
    for family in preprocessing.FAMILIES:
        for subset in candidate_subsets():
            key = subset_key(subset)
            probabilities = np.full(len(raw), np.nan)
            subset_folds = []
            if progress:
                progress(f"{family}: {key}")
            for fold, train_positions, valid_positions in folds:
                pipeline = make_feature_pipeline(family=family, features=subset, policy=policy)
                train, valid = raw.iloc[train_positions], raw.iloc[valid_positions]
                with warnings.catch_warnings(record=True) as caught, threadpool_limits(limits=1):
                    warnings.simplefilter("always", ConvergenceWarning)
                    pipeline.fit(train, target.iloc[train_positions])
                    prep = pipeline.named_steps["preprocessing"]
                    before = _state(prep)
                    probability = pipeline.predict_proba(valid)[:, 1]
                    prepared = prep.transform(valid)
                if any(issubclass(item.category, ConvergenceWarning) for item in caught):
                    raise RuntimeError(f"Model did not converge: {family}, {key}, fold {fold}.")
                if not np.isfinite(probability).all() or np.any((probability < 0) | (probability > 1)):
                    raise RuntimeError("Invalid out-of-fold probabilities.")
                if not np.isfinite(prepared).all():
                    raise RuntimeError("Nonfinite engineered model inputs.")
                state = preprocessing.learned_state(prep)
                fill_error = float(np.max(np.abs(
                    np.asarray(list(state["imputation_statistics"].values())) - fold_reference[fold])))
                unchanged = before == _state(prep)
                fit_count = prep.named_steps["business_features"].fit_rows_
                scaler_count = state["scaler"]["fit_rows"] if state["scaler"] else None
                output_count = len(_input_names(policy)) + len(subset)
                scaler_mean_error = scaler_variance_error = 0.0
                if state["scaler"]:
                    training_values = prep[:-1].transform(train)
                    scaler = prep.named_steps["scaler"]
                    scaler_mean_error = float(np.max(np.abs(scaler.mean_ - training_values.mean(axis=0))))
                    scaler_variance_error = float(np.max(np.abs(scaler.var_ - training_values.var(axis=0))))
                if (max(fill_error, scaler_mean_error, scaler_variance_error) > 1e-12 or
                        not unchanged or fit_count != len(train) or
                        scaler_count not in (None, len(train)) or prepared.shape[1] != output_count):
                    raise RuntimeError("A preprocessing statistic crossed its training boundary.")
                probabilities[valid_positions] = probability
                metrics = _metrics(target.iloc[valid_positions], probability)
                row = {"family": family, "subset": key, "fold": fold, **metrics}
                fold_results.append(row)
                subset_folds.append(row)
                fit_audit.append({"family": family, "subset": key, "fold": fold,
                    "training_records": len(train), "validation_records": len(valid),
                    "row_overlap": 0, "group_overlap": 0, "output_columns": output_count,
                    "feature_fit_records": fit_count, "scaler_fit_records": scaler_count,
                    "training_fill_max_error": fill_error,
                    "scaler_training_mean_max_error": scaler_mean_error,
                    "scaler_training_variance_max_error": scaler_variance_error,
                    "parameters_unchanged_after_validation": unchanged,
                    "training_ids_sha256": hashlib.sha256(train.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
                    "validation_ids_sha256": hashlib.sha256(valid.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
                })
                if fold == folds[0][0]:
                    first_preprocessors[(family, key)] = prep
            if not np.isfinite(probabilities).all():
                raise RuntimeError("Incomplete development out-of-fold coverage.")
            summaries.append({"family": family, "subset": key, "features": list(subset),
                "new_features": len(subset), "output_columns": output_count,
                **_metrics(target, probabilities),
                "fold_cost_std": float(np.std([r["cost_per_1000"] for r in subset_folds], ddof=1))})
    return (pd.DataFrame(summaries), pd.DataFrame(fold_results),
            pd.DataFrame(fit_audit), first_preprocessors)


def select_features(summary):
    """Minimize integer pooled cost; exact ties prefer fewer engineered columns."""
    decisions, effects, selected = [], [], {}
    for family in preprocessing.FAMILIES:
        rows = summary.loc[summary.family.eq(family)]
        by_key = {row["subset"]: row for row in rows.to_dict("records")}
        original = by_key["original"]
        # Stable predefined subset order supplies the final deterministic tie break.
        best = min(rows.to_dict("records"), key=lambda row: (row["cost"], row["new_features"]))
        names = tuple(best["features"])
        selected[family] = names
        decisions.append({"family": family, "selected_features": list(names),
            "input_columns": best["output_columns"],
            "original_cost_per_1000": original["cost_per_1000"],
            "selected_cost_per_1000": best["cost_per_1000"],
            "change_cost_per_1000": best["cost_per_1000"] - original["cost_per_1000"],
            "relative_cost_reduction_percent": 100 * (original["cost"] - best["cost"]) / original["cost"],
            "original_auc": original["roc_auc"], "selected_auc": best["roc_auc"],
            "selected_fold_cost_std": best["fold_cost_std"]})
        for name in CANDIDATES:
            singleton = by_key[subset_key((name,))]
            kept = name in names
            without = by_key[subset_key(tuple(x for x in names if x != name))] if kept else best
            with_feature = best if kept else by_key[subset_key((*names, name))]
            effect = with_feature["cost_per_1000"] - without["cost_per_1000"]
            if kept and with_feature["cost"] >= without["cost"]:
                raise RuntimeError("A retained feature adds no conditional cost improvement.")
            effects.append({"family": family, "feature": name,
                "single_addition_change_per_1000": singleton["cost_per_1000"] - original["cost_per_1000"],
                "cost_without_per_1000": without["cost_per_1000"],
                "cost_with_per_1000": with_feature["cost_per_1000"],
                "conditional_change_per_1000": effect,
                "auc_without": without["roc_auc"], "auc_with": with_feature["roc_auc"],
                "decision": "keep" if kept else "drop",
                "reason": "Lowers selected-set cost" if kept else "Does not lower selected-set cost; ties prefer fewer columns"})
    return selected, pd.DataFrame(decisions), pd.DataFrame(effects)


def save_study(evidence_dir, *, database, partition, policy, contract_audit,
               summary, fold_results, fit_audit, selected, decisions, effects):
    evidence_dir = Path(evidence_dir)
    result = {
        "section": "Challenge 3 Section 4: business features with leakage audit",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"database": database, "table": "credit.accepted",
                   "source_fingerprint": partition["source_fingerprint"]},
        "development_records": int(summary.iloc[0].records),
        "holdout_records": len(partition["holdout_row_ids"]), "holdout_evaluations": 0,
        "imputation_policy_held_fixed": policy,
        "threshold_held_fixed": THRESHOLD,
        "cost_matrix": {"false_negative": FALSE_NEGATIVE_COST, "false_positive": FALSE_POSITIVE_COST},
        "selection_rule": "Exhaustive 16-subset comparison per reference family; lowest pooled development OOF integer cost, then fewest new features, then predefined subset order.",
        "selection_scope": "Exploratory development feature selection, not unbiased final model performance or model-family selection. Keep the reserved holdout for the final evaluation.",
        "feature_contract_audit": contract_audit,
        "subsets": missingness.records(summary), "fold_results": missingness.records(fold_results),
        "fit_audit": missingness.records(fit_audit),
        "decisions": missingness.records(decisions), "feature_effects": missingness.records(effects),
        "versions": {"numpy": np.__version__, "pandas": pd.__version__, "scikit-learn": sklearn.__version__},
        "data_changes": "No database/CSV changes. Engineered features are appended inside each fresh model Pipeline, after fold-local imputation and before model-specific log/scaling.",
    }
    choice = {
        "source_fingerprint": partition["source_fingerprint"], "policy": policy,
        "selected_features": {family: list(names) for family, names in selected.items()},
        "feature_definitions": list(FEATURE_DEFINITIONS),
        "threshold_for_feature_study": THRESHOLD, "holdout_evaluations": 0,
        "usage": "make_feature_pipeline(family=family, features=selected_features[family], policy=policy); fit a fresh pipeline within each future training fold.",
        "scope": result["selection_scope"],
    }
    missingness.write_json(evidence_dir / "section_04_feature_engineering.json", result)
    missingness.write_json(evidence_dir / "section_04_selected_features.json", choice)
    return result
