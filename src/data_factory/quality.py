"""Offline inspection and streaming validation of a committed raw snapshot."""

from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from .exceptions import ConfigurationError, DataError, QuarantineThresholdError
from .ingestion import prepare_checkpoint
from .profiling import ColumnProfiler, canonical_json
from .schema import check_batch_schema, load_schema
from .validation import normalize_record


QUALITY_VERSION = 1


def _settings(config):
    try:
        settings = config["validation"]
        for name in ("inspection_batches", "schema_drift_min_records", "profile_buffer_size"):
            if type(settings[name]) is not int or settings[name] < 1:
                raise ValueError(f"validation.{name} must be a positive integer.")
        fraction = settings["max_quarantine_fraction"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("validation.max_quarantine_fraction must be between zero and one.")
        for name in ("inspection_file", "validation_dir"):
            value = config["paths"][name]
            if not isinstance(value, str) or not value or Path(value).name != value or value in (".", ".."):
                raise ValueError(f"paths.{name} must be a local filename/directory name.")
        curated = Path(config["paths"]["curated_dir"])
        schema = load_schema(settings["schema_path"], source=config["source"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid quality settings: {exc}") from exc
    return settings, curated, schema


def _inspection_settings(config):
    """Inspection is observational and does not require a validation contract."""
    try:
        settings = config["validation"]
        for name in ("inspection_batches", "profile_buffer_size", "inspection_category_limit"):
            if type(settings[name]) is not int or settings[name] < 1:
                raise ValueError(f"validation.{name} must be a positive integer.")
        for name in ("inspection_sentinel_tokens", "inspection_categorical_fields"):
            if not isinstance(settings[name], list) or any(not isinstance(value, str) for value in settings[name]):
                raise ValueError(f"validation.{name} must be a list of strings.")
        filename = config["paths"]["inspection_file"]
        if not isinstance(filename, str) or not filename or Path(filename).name != filename or filename in (".", ".."):
            raise ValueError("paths.inspection_file must be a filename inside curated_dir.")
        curated = Path(config["paths"]["curated_dir"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid inspection settings: {exc}") from exc
    return settings, curated


def _digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _hash_json(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _write_json(path, value):
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
        file.write((canonical_json(value) + "\n").encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot(config):
    # Reuse the acquisition integrity checks and take a detached manifest copy.
    # Subsequent downloads may append new batches; this run processes only this copy.
    state = prepare_checkpoint(config)
    if not state["batches"]:
        raise DataError("No committed raw batches exist. Run the ingest stage first.")
    identity = {
        "scope_fingerprint": state["scope_fingerprint"],
        "batches": [{key: row[key] for key in ("file", "sha256", "records")} for row in state["batches"]],
        "completed": state["completed"],
    }
    return state, _hash_json(identity)


def _reject_constant(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _read_batch(raw_dir, metadata):
    path = raw_dir / metadata["file"]
    if _digest(path) != metadata["sha256"]:
        raise DataError(f"Raw file changed after the snapshot was selected: {path.name}")
    try:
        with path.open(encoding="utf-8") as file:
            batch = json.load(file, parse_constant=_reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise DataError(f"Invalid raw JSON in {path.name}: {exc}") from exc
    if not isinstance(batch, list) or len(batch) != metadata["records"]:
        raise DataError(f"Raw record count/structure mismatch in {path.name}")
    return batch


def _connect(path):
    connection = sqlite3.connect(path)
    # This database is disposable scratch space, never an output or checkpoint.
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA cache_size=-8192")
    return connection


def inspect_raw_data(config, *, progress=print):
    """Profile equally spaced committed batches; label the sample explicitly."""
    settings, curated = _inspection_settings(config)
    state, snapshot_hash = _snapshot(config)
    count = min(settings["inspection_batches"], len(state["batches"]))
    indices = [0] if count == 1 else [round(i * (len(state["batches"]) - 1) / (count - 1)) for i in range(count)]
    selected = [state["batches"][index] for index in indices]
    curated.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".inspection-", dir=curated) as temporary:
        connection = _connect(Path(temporary) / "profile.sqlite")
        try:
            profiler = ColumnProfiler(
                connection, buffer_size=settings["profile_buffer_size"],
                sentinel_tokens=settings["inspection_sentinel_tokens"],
            )
            categories = {name: Counter() for name in settings["inspection_categorical_fields"]}
            category_overflow = Counter()
            dates = Counter({name: 0 for name in (
                "created_date_missing", "closed_date_missing",
                "created_date_unparseable", "closed_date_unparseable",
                "negative_closure_intervals", "incomparable_closure_dates",
            )})
            non_objects = 0
            for metadata in selected:
                for record in _read_batch(Path(config["paths"]["raw_dir"]), metadata):
                    if isinstance(record, dict):
                        profiler.add(record)
                        for name, values in categories.items():
                            encoded = canonical_json(record.get(name))
                            if encoded in values or len(values) < settings["inspection_category_limit"]:
                                values[encoded] += 1
                            else:
                                category_overflow[name] += 1
                        parsed = {}
                        for name in ("created_date", "closed_date"):
                            value = record.get(name)
                            if value is None or (isinstance(value, str) and not value.strip()):
                                dates[f"{name}_missing"] += 1
                            else:
                                try:
                                    parsed[name] = datetime.fromisoformat(value.strip())
                                except (TypeError, ValueError, AttributeError):
                                    dates[f"{name}_unparseable"] += 1
                        if len(parsed) == 2:
                            try:
                                if parsed["closed_date"] < parsed["created_date"]:
                                    dates["negative_closure_intervals"] += 1
                            except TypeError:
                                dates["incomparable_closure_dates"] += 1
                    else:
                        non_objects += 1
            profile = profiler.result()
        finally:
            connection.close()
    report = {
        "kind": "equally_spaced_raw_batch_inspection",
        "snapshot_hash": snapshot_hash,
        "snapshot_completed": state["completed"],
        "snapshot_records": state["downloaded_records"],
        "selected_files": [row["file"] for row in selected],
        "selected_records": sum(row["records"] for row in selected),
        "non_object_records": non_objects,
        "profile": profile,
        "date_observations": dict(sorted(dates.items())),
        "categorical_counts": {
            name: {
                "values": [{"value": json.loads(value), "count": count} for value, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))],
                "other_count": category_overflow[name],
            }
            for name, values in categories.items()
        },
        "sentinel_note": "Text sentinels are observations, not automatic missing values; UNKNOWN may be a legitimate channel category.",
        "range_note": "Raw strings have lexicographic ranges; numeric-looking strings have not been converted to numbers.",
        "note": "This sample describes selected existing batches, not the complete 18-month population.",
    }
    path = curated / config["paths"]["inspection_file"]
    _atomic_json(path, report)
    if progress is not None:
        progress(f"Inspected {report['selected_records']:,} records from {count} batches; report: {path}")
    return report


def validate_dataset(config, *, progress=print):
    """Validate a frozen list of committed batches without any HTTP requests.

    Outputs are published as a content-addressed directory. A report is required
    to interpret the result: passing validation of an incomplete acquisition is
    still a partial snapshot, not a completed 18-month delivery.
    """
    settings, curated, schema = _settings(config)
    state, snapshot_hash = _snapshot(config)
    run_hash = _hash_json({
        "version": QUALITY_VERSION, "snapshot": snapshot_hash,
        "schema": schema,
        "rules": {key: settings[key] for key in ("max_quarantine_fraction", "schema_drift_min_records")},
    })
    root = curated / config["paths"]["validation_dir"]
    target = root / run_hash
    root.mkdir(parents=True, exist_ok=True)
    counts = Counter(raw_records=0, accepted_records=0, quarantined_records=0)
    reasons = Counter()
    warnings = Counter()
    observed = set()
    with tempfile.TemporaryDirectory(prefix=".validation-", dir=root) as temporary:
        work = Path(temporary)
        artifacts = work / "artifacts"
        artifacts.mkdir()
        connection = _connect(work / "scratch.sqlite")
        try:
            connection.execute("CREATE TABLE seen_keys (key TEXT PRIMARY KEY, raw_hash TEXT) WITHOUT ROWID")
            profiler = ColumnProfiler(
                connection,
                fields=[*schema["fields"], *schema["derived_fields"]],
                buffer_size=settings["profile_buffer_size"],
            )
            with (artifacts / "accepted.jsonl").open("w", encoding="utf-8", newline="\n") as accepted, (artifacts / "quarantine.jsonl").open("w", encoding="utf-8", newline="\n") as quarantine:
                for metadata in state["batches"]:
                    batch = _read_batch(Path(config["paths"]["raw_dir"]), metadata)
                    check_batch_schema(batch, schema, minimum_records=settings["schema_drift_min_records"])
                    for row_number, record in enumerate(batch, 1):
                        counts["raw_records"] += 1
                        if isinstance(record, dict):
                            observed.update(record)
                        clean, errors = normalize_record(record, schema, config["source"])
                        if not errors:
                            key = clean[schema["relations"]["primary_key"]]
                            raw_hash = _hash_json(record)
                            previous = connection.execute("SELECT raw_hash FROM seen_keys WHERE key=?", (key,)).fetchone()
                            if previous is not None:
                                code = "DUPLICATE_RECORD" if previous[0] == raw_hash else "DUPLICATE_KEY_CONFLICT"
                                errors = [{"field": schema["relations"]["primary_key"], "code": code}]
                            else:
                                connection.execute("INSERT INTO seen_keys VALUES (?, ?)", (key, raw_hash))
                        if errors:
                            codes = sorted({error["code"] for error in errors})
                            reasons.update(codes)
                            counts["quarantined_records"] += 1
                            quarantine.write(canonical_json({
                                "source_file": metadata["file"], "source_row": row_number,
                                "reason_codes": codes, "errors": errors, "record": record,
                            }) + "\n")
                        else:
                            accepted.write(canonical_json(clean) + "\n")
                            profiler.add(clean)
                            warnings.update(clean["quality_flags"])
                            counts["accepted_records"] += 1
                    if progress is not None:
                        progress(f"Validated {metadata['file']}: accepted={counts['accepted_records']:,}, quarantined={counts['quarantined_records']:,}")
            profile = profiler.result()
        finally:
            connection.close()
        if counts["raw_records"] != state["downloaded_records"]:
            raise DataError("Validation counts disagree with the committed snapshot.")
        if counts["raw_records"] != counts["accepted_records"] + counts["quarantined_records"]:
            raise DataError("Validation accounting failed.")
        fraction = counts["quarantined_records"] / counts["raw_records"]
        passed = fraction <= settings["max_quarantine_fraction"]
        _write_json(artifacts / "quality_profile.json", profile)
        report = {
            "quality_version": QUALITY_VERSION,
            "status": "passed" if passed else "failed",
            "snapshot_hash": snapshot_hash,
            "schema_hash": _hash_json(schema),
            "snapshot_completed": state["completed"],
            "scope": state["scope"],
            "input_batches": len(state["batches"]),
            "raw_records": counts["raw_records"],
            "accepted_records": counts["accepted_records"],
            "quarantined_records": counts["quarantined_records"],
            "quarantine_fraction": fraction,
            "max_quarantine_fraction": settings["max_quarantine_fraction"],
            "schema_drift_min_records": settings["schema_drift_min_records"],
            "reason_counts": dict(sorted(reasons.items())),
            "reason_counts_may_overlap": True,
            "warning_counts": dict(sorted(warnings.items())),
            "source_fields_observed": sorted(observed),
            "source_fields_outside_projection": sorted(observed - set(schema["fields"])),
            "scope_note": "Full acquisition" if state["completed"] else "Partial acquisition; the full selected 18-month scope is not yet available.",
            "files": {name: {"sha256": _digest(artifacts / name)} for name in ("accepted.jsonl", "quarantine.jsonl", "quality_profile.json")},
        }
        _write_json(artifacts / "validation_report.json", report)
        if target.exists():
            for name in (*report["files"], "validation_report.json"):
                if not (target / name).is_file() or _digest(target / name) != _digest(artifacts / name):
                    raise DataError("An existing validation result differs for the same input/contract; refusing to overwrite it.")
        else:
            try:
                os.rename(artifacts, target)
            except FileExistsError as exc:
                raise DataError("Another validation run published this snapshot concurrently; rerun validation.") from exc
    _atomic_json(root / "latest.json", {"run": run_hash, "report": f"{run_hash}/validation_report.json", "status": report["status"]})
    report_path = target / "validation_report.json"
    if not passed:
        raise QuarantineThresholdError(
            f"Quarantine fraction {fraction:.2%} exceeds {settings['max_quarantine_fraction']:.2%}; diagnostics: {report_path}",
            report_path=report_path,
        )
    return {**report, "report_path": str(report_path), "output_dir": str(target)}
