"""Section 3: raw-schema sklearn pipelines with training-only learned state."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import warnings

import joblib
import numpy as np
import pandas as pd
import sklearn
from pandas.api.types import is_bool_dtype, is_numeric_dtype
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler
from sklearn.utils.validation import check_is_fitted
from threadpoolctl import threadpool_limits

from . import credit_missingness as missingness


FEATURES = missingness.FEATURES
LATE_COLUMNS = missingness.LATE_COLUMNS
TARGET = missingness.TARGET
MISSING_FEATURES = tuple(name for name in FEATURES if name in (*LATE_COLUMNS, "MonthlyIncome", "NumberOfDependents"))
INTEGER_FEATURES = tuple(name for name in FEATURES if name not in (
    "RevolvingUtilizationOfUnsecuredLines", "DebtRatio", "MonthlyIncome"
))
FAMILIES = ("logistic", "hist_gradient_boosting")


def semantic_inputs(frame):
    """Validate the fixed accepted schema and apply only stateless source rules."""
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Input must be a DataFrame with named accepted-data columns.")
    if not frame.columns.is_unique:
        raise ValueError("Duplicate input column names are not allowed.")
    absent = set(FEATURES) - set(frame.columns)
    extra = set(frame.columns) - set(FEATURES) - {"row_id", TARGET}
    if absent or extra:
        raise ValueError(f"Schema mismatch: missing={sorted(absent)}, unexpected={sorted(extra)}")
    output = frame.loc[:, list(FEATURES)].copy()
    for name in FEATURES:
        column = output[name]
        boolean_values = (not is_numeric_dtype(column.dtype) and
                          column.dropna().map(lambda value: isinstance(value, (bool, np.bool_))).any())
        if is_bool_dtype(column.dtype) or boolean_values:
            raise ValueError(f"Boolean input is not a numeric measurement: {name}")
        try:
            output[name] = pd.to_numeric(column, errors="raise").astype("float64")
        except (TypeError, ValueError):
            raise ValueError(f"Invalid numeric input in {name}") from None
    output.loc[:, list(LATE_COLUMNS)] = output.loc[:, list(LATE_COLUMNS)].mask(
        output.loc[:, list(LATE_COLUMNS)].isin([96, 98])
    )
    unexpected_missing = [name for name in FEATURES if name not in MISSING_FEATURES and output[name].isna().any()]
    if unexpected_missing:
        raise ValueError(f"Missing values violate the accepted schema: {unexpected_missing}")
    if np.isinf(output.to_numpy()).any() or output.lt(0).any().any():
        raise ValueError("Numeric inputs must be finite/nonnegative, apart from allowed missing values.")
    if output.age.lt(1).any():
        raise ValueError("Age must be positive.")
    for name in INTEGER_FEATURES:
        observed = output[name].dropna()
        if observed.ne(np.floor(observed)).any():
            raise ValueError(f"A whole-number count/year is required in {name}")
    return output


class CreditSchemaTransformer(TransformerMixin, BaseEstimator):
    """Drop ID/target, enforce column order, and mask codes inside the pipeline."""

    def fit(self, X, y=None):
        clean = semantic_inputs(X)
        empty = clean.columns[clean.isna().all()].tolist()
        if empty:
            raise ValueError(f"Cannot learn an imputation statistic from all-missing training columns: {empty}")
        if not len(clean):
            raise ValueError("Training data is empty.")
        self.feature_names_in_ = np.asarray(FEATURES, dtype=object)
        self.n_features_in_ = len(FEATURES)
        self.fit_rows_ = len(clean)
        return self

    def transform(self, X):
        check_is_fitted(self, "feature_names_in_")
        return semantic_inputs(X)

    def get_feature_names_out(self, input_features=None):
        check_is_fitted(self, "feature_names_in_")
        return self.feature_names_in_.copy()


def make_preprocessor(*, family="logistic", policy="median_indicator"):
    """Create an UNFITTED pipeline. Use a fresh instance in each CV fold."""
    if family not in FAMILIES:
        raise ValueError(f"Unknown model family: {family}")
    settings = missingness.POLICIES[policy]
    branches = [("values", SimpleImputer(strategy=settings["strategy"]), list(FEATURES))]
    if settings["add_indicator"]:
        # The indicator schema is fixed by source semantics, not by validation
        # values or which nullable columns happened to be missing in a fold.
        branches.append(("missing", MissingIndicator(features="all", error_on_new=False), list(MISSING_FEATURES)))
    steps = [
        ("schema", CreditSchemaTransformer()),
        ("columns", ColumnTransformer(branches, remainder="drop", sparse_threshold=0,
                                       verbose_feature_names_out=False)),
    ]
    if family == "logistic":
        steps += [("log1p", FunctionTransformer(np.log1p, feature_names_out="one-to-one")),
                  ("scaler", StandardScaler())]
    return Pipeline(steps)


def make_model_pipeline(*, family="logistic", policy="median_indicator"):
    """Keep preprocessing and estimation in the same fit/predict boundary."""
    if family == "logistic":
        classifier = LogisticRegression(C=1.0, solver="lbfgs", max_iter=2000, tol=1e-6)
    elif family == "hist_gradient_boosting":
        classifier = HistGradientBoostingClassifier(max_iter=100, max_leaf_nodes=31,
            learning_rate=.1, l2_regularization=1.0, categorical_features=None,
            early_stopping=False, random_state=42)
    else:
        raise ValueError(f"Unknown model family: {family}")
    return Pipeline([("preprocessing", make_preprocessor(family=family, policy=policy)),
                     ("classifier", classifier)])


def transform_frame(preprocessor, frame):
    return pd.DataFrame(preprocessor.transform(frame), index=frame.index,
                        columns=preprocessor.get_feature_names_out())


def load_development(engine, evidence_dir):
    """Reuse the Section 2 partition without returning the final holdout."""
    evidence_dir = Path(evidence_dir)
    path = evidence_dir / "section_02_partition.json"
    if not path.exists():
        raise ValueError("Reserve the Section 2 partition before preparing Section 3.")
    previous = json.loads(path.read_text(encoding="utf-8"))
    choice = json.loads((evidence_dir / "section_02_selected_policy.json").read_text(encoding="utf-8"))
    frame, database = missingness.read_credit_data(engine)
    partition = missingness.reserve_partition(frame, path, seed=previous["seed"], n_splits=previous["n_splits"])
    if choice["source_fingerprint"] != partition["source_fingerprint"]:
        raise ValueError("The selected policy belongs to a different data snapshot.")
    features, target, groups, folds = missingness.development_data(frame, partition)
    raw_development = frame.set_index("row_id", drop=False).loc[features.index].copy()
    return raw_development, target, groups, folds, partition, choice, database


def learned_state(preprocessor):
    columns = preprocessor.named_steps["columns"]
    state = {
        "schema_fit_rows": preprocessor.named_steps["schema"].fit_rows_,
        "input_features": list(FEATURES),
        "imputation_statistics": dict(zip(FEATURES, columns.named_transformers_["values"].statistics_.tolist())),
        "output_features": preprocessor.get_feature_names_out().tolist(),
        "indicator_source_columns": list(MISSING_FEATURES) if "missing" in columns.named_transformers_ else [],
        "learned_categorical_vocabularies": {},
    }
    if "scaler" in preprocessor.named_steps:
        scaler = preprocessor.named_steps["scaler"]
        state["scaler"] = {"fit_rows": int(scaler.n_samples_seen_), "mean": scaler.mean_.tolist(),
                           "variance_ddof0": scaler.var_.tolist(), "scale": scaler.scale_.tolist()}
    else:
        state["scaler"] = None
    return state


def _state_hash(preprocessor):
    raw = json.dumps(learned_state(preprocessor), sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def audit_fold_boundaries(raw, groups, folds, policy):
    """Execute preprocessing within every saved fold and retain fitted statistics."""
    rows, states = [], []
    first_preprocessors = {}
    legacy_max_error = None
    for family in FAMILIES:
        for fold, train_positions, valid_positions in folds:
            train, valid = raw.iloc[train_positions], raw.iloc[valid_positions]
            preprocessor = make_preprocessor(family=family, policy=policy)
            with threadpool_limits(limits=1):
                preprocessor.fit(train)
                before = _state_hash(preprocessor)
                transformed = transform_frame(preprocessor, valid)
            clean_train = semantic_inputs(train)
            strategy = missingness.POLICIES[policy]["strategy"]
            expected = clean_train.median() if strategy == "median" else clean_train.mode().iloc[0]
            state = learned_state(preprocessor)
            median_error = float(np.max(np.abs(np.asarray(list(state["imputation_statistics"].values())) - expected.loc[list(FEATURES)].to_numpy())))
            scaler_mean_error = 0.0
            scaler_variance_error = 0.0
            if family == "logistic":
                train_imputed = preprocessor.named_steps["columns"].transform(clean_train)
                training_values = np.log1p(train_imputed)
                expected_means = training_values.mean(axis=0)
                scaler_mean_error = float(np.max(np.abs(preprocessor.named_steps["scaler"].mean_ - expected_means)))
                scaler_variance_error = float(np.max(np.abs(
                    preprocessor.named_steps["scaler"].var_ - training_values.var(axis=0, ddof=0))))
            overlap = int(np.intersect1d(train.index.to_numpy(), valid.index.to_numpy()).size)
            group_overlap = int(np.intersect1d(groups[train_positions], groups[valid_positions]).size)
            finite = bool(np.isfinite(transformed.to_numpy()).all())
            unchanged = before == _state_hash(preprocessor)
            if (overlap or group_overlap or not finite or not unchanged or
                    max(median_error, scaler_mean_error, scaler_variance_error) > 1e-12):
                raise RuntimeError(f"Preprocessing fit-boundary audit failed: {family}, fold {fold}.")
            rows.append({"family": family, "fold": fold, "train_records": len(train),
                         "validation_records": len(valid), "row_overlap": overlap, "group_overlap": group_overlap,
                         "output_columns": transformed.shape[1], "finite_output": finite,
                         "training_fill_max_error": median_error, "scaler_training_mean_max_error": scaler_mean_error,
                         "scaler_training_variance_max_error": scaler_variance_error,
                         "fit_state_unchanged_after_transform": unchanged})
            states.append({"family": family, "fold": fold, "training_ids_sha256": hashlib.sha256(train.index.to_numpy(dtype="int64").tobytes()).hexdigest(),
                           "validation_ids_sha256": hashlib.sha256(valid.index.to_numpy(dtype="int64").tobytes()).hexdigest(), **state})
            if fold == folds[0][0]:
                first_preprocessors[family] = preprocessor
                if family == "logistic":
                    # Verify the raw-schema wrapper preserves the selected
                    # Section 2 transform on this snapshot, not a new policy.
                    legacy = missingness.make_policy_preprocessor(policy)
                    with threadpool_limits(limits=1):
                        legacy.fit(missingness.prepare_features(train))
                    old_values = legacy.transform(missingness.prepare_features(valid))
                    if old_values.shape != transformed.shape:
                        raise RuntimeError("Selected Section 2 output schema changed on this snapshot.")
                    legacy_max_error = float(np.max(np.abs(old_values - transformed.to_numpy())))
                    if legacy_max_error > 1e-12:
                        raise RuntimeError("The wrapped preprocessing differs from the selected Section 2 policy.")
    return pd.DataFrame(rows), states, first_preprocessors, legacy_max_error


def audit_new_batches(preprocessors, batch):
    """Demonstrate schema/order invariance and transform-only handling of new values."""
    batch = batch.head(32).copy()
    rows = []
    for family, preprocessor in preprocessors.items():
        baseline = transform_frame(preprocessor, batch)
        fingerprint = _state_hash(preprocessor)
        features_only = batch.loc[:, list(reversed(FEATURES))]
        reordered = transform_frame(preprocessor, features_only)
        metadata_changed = batch.copy()
        metadata_changed["row_id"] = np.arange(len(batch)) + 1_000_000
        metadata_changed[TARGET] = 1 - metadata_changed[TARGET]
        metadata_output = transform_frame(preprocessor, metadata_changed)
        shifted = features_only.copy()
        shifted["MonthlyIncome"] = np.nan
        shifted["NumberOfDependents"] = np.nan
        shifted.loc[shifted.index[::2], "RevolvingUtilizationOfUnsecuredLines"] = 1e12
        shifted["NumberOfTime30-59DaysPastDueNotWorse"] = 96.0
        shifted["NumberOfTime60-89DaysPastDueNotWorse"] = 98.0
        new_output = transform_frame(preprocessor, shifted)
        order_error = float(np.max(np.abs(baseline.to_numpy() - reordered.to_numpy())))
        metadata_error = float(np.max(np.abs(baseline.to_numpy() - metadata_output.to_numpy())))
        same_names = baseline.columns.equals(new_output.columns)
        unchanged = fingerprint == _state_hash(preprocessor)
        finite = bool(np.isfinite(new_output.to_numpy()).all())
        if order_error or metadata_error or not same_names or not unchanged or not finite:
            raise RuntimeError("New-batch transform changed the learned state or input contract.")
        rows.append({"family": family, "records": len(batch), "reordered_columns_max_error": order_error,
                     "changed_id_target_max_error": metadata_error, "new_missing_and_extreme_values_finite": finite,
                     "same_output_schema": same_names, "learned_parameters_unchanged": unchanged})
    invalid_batches = {
        "missing_column": batch.drop(columns="age"),
        "unexpected_column": batch.assign(unexpected_feature=1),
        "invalid_numeric_category": batch.assign(age="unknown"),
        "nonpositive_age": batch.assign(age=0),
        "unexpected_missing_age": batch.assign(age=np.nan),
        "negative_income": batch.assign(MonthlyIncome=-1),
        "fractional_dependents": batch.assign(NumberOfDependents=.5),
        "infinite_debt_ratio": batch.assign(DebtRatio=np.inf),
        "duplicate_header": pd.concat([batch, batch.loc[:, ["age"]]], axis=1),
    }
    rejections = []
    preprocessor = preprocessors["logistic"]
    for name, invalid in invalid_batches.items():
        try:
            preprocessor.transform(invalid)
        except ValueError as exc:
            rejections.append({"case": name, "rejected": True, "reason": str(exc)})
        else:
            raise RuntimeError(f"Invalid input was silently accepted: {name}")
    return pd.DataFrame(rows), pd.DataFrame(rejections)


def audit_model_execution(raw, target, folds, policy):
    """Fit two integration references on one development fold, without scoring."""
    _, train_positions, valid_positions = folds[0]
    train, probe = raw.iloc[train_positions], raw.iloc[valid_positions].head(32)
    rows = []
    for family in FAMILIES:
        pipeline = make_model_pipeline(family=family, policy=policy)
        with warnings.catch_warnings(record=True) as caught, threadpool_limits(limits=1):
            warnings.simplefilter("always", ConvergenceWarning)
            pipeline.fit(train, target.iloc[train_positions])
            probabilities = pipeline.predict_proba(probe.loc[:, list(FEATURES)])
        if any(issubclass(item.category, ConvergenceWarning) for item in caught):
            raise RuntimeError(f"Integration reference did not converge: {family}")
        valid_probabilities = bool(np.isfinite(probabilities).all() and
            np.all((probabilities >= 0) & (probabilities <= 1)) and
            np.allclose(probabilities.sum(axis=1), 1))
        if not valid_probabilities:
            raise RuntimeError(f"Invalid probability output: {family}")
        rows.append({"family": family, "fit_records": len(train), "prediction_probe_records": len(probe),
                     "preprocessing_inside_model_pipeline": True, "valid_probabilities": valid_probabilities,
                     "scaled": "scaler" in pipeline.named_steps["preprocessing"].named_steps,
                     "performance_metrics_computed": False})
    return pd.DataFrame(rows)


def save_preprocessors(raw, policy, partition, path):
    """Fit only on development data; this artifact is for inference, never CV fit."""
    preprocessors = {}
    for family in FAMILIES:
        preprocessor = make_preprocessor(family=family, policy=policy)
        with threadpool_limits(limits=1):
            preprocessor.fit(raw)
        preprocessors[family] = preprocessor
    metadata = {"source_fingerprint": partition["source_fingerprint"], "policy": policy,
                "fit_records": len(raw), "fit_partition": "development_only",
                "holdout_evaluations": 0, "input_features": list(FEATURES),
                "indicator_fields": list(MISSING_FEATURES),
                "versions": {"numpy": np.__version__, "pandas": pd.__version__, "scikit-learn": sklearn.__version__, "joblib": joblib.__version__},
                "cv_usage": "Build and fit a new make_model_pipeline inside each training fold; this fitted artifact is inference-only."}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    joblib.dump({"metadata": metadata, "preprocessors": preprocessors}, temporary, compress=3)
    temporary.replace(path)
    reloaded = joblib.load(path)
    parity = []
    new_batch = raw.head(32).loc[:, list(reversed(FEATURES))]
    for family, preprocessor in preprocessors.items():
        before = transform_frame(preprocessor, new_batch)
        after = transform_frame(reloaded["preprocessors"][family], new_batch)
        error = float(np.max(np.abs(before.to_numpy() - after.to_numpy())))
        if error or not before.columns.equals(after.columns):
            raise RuntimeError("Reloaded preprocessing artifact changed the output.")
        parity.append({"family": family, "reload_max_error": error, "output_columns": len(before.columns),
                       "same_names_after_reload": True})
    metadata["artifact_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return reloaded, metadata, pd.DataFrame(parity)


def save_evidence(path, *, database, partition, policy, fold_audit, fold_states,
                  batch_audit, schema_rejections, model_execution, artifact_metadata,
                  reload_audit, legacy_max_error):
    result = {
        "section": "Challenge 3 Section 3: leakage-safe preprocessing",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"database": database, "table": "credit.accepted", "source_fingerprint": partition["source_fingerprint"]},
        "selected_policy": policy, "development_records": artifact_metadata["fit_records"],
        "holdout_records": len(partition["holdout_row_ids"]), "holdout_evaluations": 0,
        "encoding": {"nominal_input_columns": [], "missing_indicator_encoding": "fixed binary 0/1",
                     "indicator_columns": list(MISSING_FEATURES), "categories_inferred_from_validation": False},
        "scaling": {"logistic": "log1p then StandardScaler fitted only on training; L2 penalty and optimization depend on scale",
                    "hist_gradient_boosting": "no log1p/scaler; threshold/bin ordering does not require standardization"},
        "fold_audit": missingness.records(fold_audit), "fold_fitted_state": fold_states,
        "new_batch_audit": missingness.records(batch_audit), "schema_rejections": missingness.records(schema_rejections),
        "model_integration": missingness.records(model_execution),
        "section2_transform_max_error": legacy_max_error,
        "artifact": artifact_metadata, "reload_audit": missingness.records(reload_audit),
        "scope": "Two model references fitted only for pipeline integration on one development fold; no model selection or validation/holdout performance scoring.",
        "data_changes": "No database/CSV changes; 10 numerical inputs plus 5 binary missingness indicators in model space only.",
    }
    missingness.write_json(path, result)
    return result
