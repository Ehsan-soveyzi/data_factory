"""Apply the frozen Stage 03 classifier to new CSV records without fitting."""

import csv
import hashlib
import json
from pathlib import Path
import tempfile

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.model_selection import FixedThresholdClassifier
from sklearn.utils.validation import check_is_fitted
from threadpoolctl import threadpool_limits

from .credit_missingness import FEATURES, TARGET
from .credit_preprocessing import learned_state


DEFAULT_MODEL = "stages/stage_03/evidence/section_07_fitted_classifier.joblib"
RECEIPT_FILE = "section_07_evaluation.json"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_classifier(model_path=DEFAULT_MODEL):
    """Load the local frozen artifact after checking its saved receipt and recipe."""
    model_path = Path(model_path).resolve()
    receipt = json.loads(model_path.with_name(RECEIPT_FILE).read_text(encoding="utf-8"))
    if _sha256(model_path) != receipt["model_artifact"]["sha256"]:
        raise ValueError("Model checksum differs from the frozen Section 7 receipt.")
    version = receipt["training_audit"]["versions"]["scikit-learn"]
    if sklearn.__version__ != version:
        raise ValueError(f"This saved model requires scikit-learn {version}; install requirements.txt.")
    bundle = joblib.load(model_path)
    classifier, metadata = bundle["classifier"], bundle["metadata"]
    selection = metadata["selection"]
    recipe = hashlib.sha256(json.dumps(selection, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if (recipe != receipt["recipe_sha256"] or recipe != metadata["recipe_sha256"] or
            selection != receipt["selection"]):
        raise ValueError("The saved classifier and frozen decision receipt have different recipes.")
    if not isinstance(classifier, FixedThresholdClassifier):
        raise ValueError("Expected the saved FixedThresholdClassifier containing the full pipeline.")
    check_is_fitted(classifier)
    if (not np.array_equal(classifier.classes_, [0, 1]) or selection["positive_label"] != 1 or
            selection["comparison"] != ">=" or classifier.pos_label != 1 or
            float(classifier.threshold).hex() != selection["threshold_hex"] or
            float(classifier.threshold) != selection["threshold"] or
            learned_state(classifier.estimator_.named_steps["preprocessing"]) != metadata["fitted_preprocessing"]):
        raise ValueError("Saved threshold, positive class or fitted preprocessing is inconsistent.")
    return classifier, metadata


def predict_frame(frame, classifier):
    """Return aligned probabilities/classes; the fitted pipeline handles raw inputs."""
    check_is_fitted(classifier)
    if not len(frame):
        raise ValueError("Prediction input is empty.")
    prep = classifier.estimator_.named_steps["preprocessing"]
    before = learned_state(prep)
    with threadpool_limits(limits=1):
        probabilities = classifier.predict_proba(frame)
    if (probabilities.shape != (len(frame), 2) or not np.isfinite(probabilities).all() or
            np.any((probabilities < 0) | (probabilities > 1)) or
            not np.allclose(probabilities.sum(axis=1), 1) or learned_state(prep) != before):
        raise ValueError("Inference changed preprocessing or returned invalid probabilities.")
    positive = int(np.flatnonzero(classifier.classes_ == 1)[0])
    probability = probabilities[:, positive]
    output = pd.DataFrame({"probability_default": probability,
        "prediction": (probability >= float(classifier.threshold)).astype(np.uint8)}, index=frame.index)
    if "row_id" in frame:
        output.insert(0, "row_id", frame.row_id.to_numpy())
    return output


def _csv_batches(path, batch_size):
    """Preserve order/IDs and reject duplicate headers or ragged CSV records."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        header = next(reader, None)
        if not header or len(set(header)) != len(header):
            raise ValueError("A nonempty CSV with unique named columns is required.")
        absent = set(FEATURES) - set(header)
        extra = set(header) - set(FEATURES) - {"row_id", TARGET}
        if absent or extra:
            raise ValueError(f"CSV schema mismatch: missing={sorted(absent)}, unexpected={sorted(extra)}")
        rows = []
        for row in reader:
            if not row:  # Ignore physically blank lines, as pandas CSV reading does.
                continue
            if len(row) != len(header):
                raise ValueError(f"CSV line {reader.line_num}: expected {len(header)} fields, got {len(row)}.")
            rows.append(row)
            if len(rows) == batch_size:
                yield _numeric_batch(rows, header)
                rows = []
        if rows:
            yield _numeric_batch(rows, header)


def _numeric_batch(rows, header):
    frame = pd.DataFrame(rows, columns=header)
    for name in FEATURES:
        try:
            frame[name] = pd.to_numeric(frame[name].replace("", np.nan), errors="raise")
        except (ValueError, TypeError):
            raise ValueError(f"Invalid numeric CSV value in {name}; use an empty field for missing data.") from None
    return frame


def predict_csv(input_path, output_path, *, model_path=DEFAULT_MODEL, batch_size=10000):
    """Predict in bounded batches and publish a complete CSV by atomic replacement."""
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer.")
    input_path, output_path, model_path = map(lambda p: Path(p).resolve(), (input_path, output_path, model_path))
    receipt_path = model_path.with_name(RECEIPT_FILE)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    protected = {input_path, model_path, receipt_path,
                 model_path.parent / receipt["prediction_artifact"]["file"]}
    if output_path in protected:
        raise ValueError("Prediction output must not overwrite the input or frozen Section 7 artifacts.")
    input_sha, model_sha = _sha256(input_path), _sha256(model_path)
    classifier, metadata = load_classifier(model_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    records = positive = batches = 0
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", delete=False,
                prefix=output_path.name + ".", suffix=".tmp", dir=output_path.parent) as stream:
            temporary = Path(stream.name)
            for frame in _csv_batches(input_path, batch_size):
                result = predict_frame(frame, classifier)
                result.insert(0, "record_number", np.arange(records + 1, records + len(result) + 1))
                result.to_csv(stream, index=False, header=not records, float_format="%.17g")
                records += len(result)
                positive += int(result.prediction.sum())
                batches += 1
        if not records:
            raise ValueError("CSV has a header but contains no prediction records.")
        if _sha256(input_path) != input_sha or _sha256(model_path) != model_sha:
            raise ValueError("Input/model changed during inference; no new output was published.")
        output_sha = _sha256(temporary)
        temporary.replace(output_path)
    except csv.Error as error:
        raise ValueError(f"Malformed CSV: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"status": "passed", "input_file": str(input_path), "output_file": str(output_path),
        "input_sha256": input_sha, "output_sha256": output_sha, "model_file": str(model_path),
        "model_sha256": model_sha, "recipe_sha256": metadata["recipe_sha256"],
        "model": metadata["selection"]["model"], "threshold": float(classifier.threshold),
        "threshold_hex": float(classifier.threshold).hex(), "positive_label": 1,
        "records": records, "predicted_default": positive, "batches": batches,
        "batch_size": batch_size, "model_fits": 0, "database_reads": 0,
        "holdout_evaluations": 0, "preprocessing_unchanged": True}
