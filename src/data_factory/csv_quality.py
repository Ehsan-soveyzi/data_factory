"""Offline CSV validation with immutable raw input and atomic run publication."""

from collections import Counter
from contextlib import ExitStack
import codecs
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .csv_contract import check_keys, load_contract, normalize_row, read_json
from .exceptions import ConfigurationError, DataError, QuarantineThresholdError, SchemaDriftError
from .profiling import ColumnProfiler, canonical_json


CSV_QUALITY_VERSION = 1


def _digest(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _json(path, value):
    Path(path).write_text(canonical_json(value) + "\n", encoding="utf-8")


def _latest(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write((canonical_json(value) + "\n").encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _settings(path):
    path = Path(path).resolve()
    config = read_json(path)
    try:
        check_keys(config, {"source", "contract", "output_dir"}, {"source", "contract", "output_dir"}, "CSV config")
        source = config["source"]
        check_keys(source, {"path", "encoding", "delimiter", "expected_sha256"}, {"path"}, "CSV source")
        for value in (source["path"], config["contract"], config["output_dir"]):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Source, contract and output paths must be nonempty strings.")
        raw = (path.parent / source["path"]).resolve()
        contract_path = (path.parent / config["contract"]).resolve()
        output = (path.parent / config["output_dir"]).resolve()
        if any(p.is_relative_to(output) for p in (raw, contract_path, path)):
            raise ValueError("Output directory must not contain source/config/contract files.")
        encoding, delimiter = source.get("encoding", "utf-8-sig"), source.get("delimiter", ",")
        if not isinstance(encoding, str):
            raise ValueError("encoding must be a string.")
        codecs.lookup(encoding)
        if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n"\0':
            raise ValueError("delimiter must be a single character other than newline, quote or NUL.")
        expected = source.get("expected_sha256")
        if expected is not None and (not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
            raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters.")
    except (ValueError, TypeError, KeyError, LookupError) as exc:
        raise ConfigurationError(f"Invalid CSV settings: {exc}") from exc
    return raw, contract_path, output, encoding, delimiter, expected


def _check_header(header, contract):
    if not header:
        raise SchemaDriftError("CSV has no header.")
    if len(header) != len(set(header)):
        raise SchemaDriftError("CSV has duplicate column headers.")
    declared = {f["source"] for f in contract["fields"]}
    missing = {f["source"] for f in contract["fields"] if f["required_presence"]} - set(header)
    extra = set(header) - declared
    if missing or (extra and contract["extra_columns"] == "reject"):
        raise SchemaDriftError(f"CSV header differs from contract: missing={sorted(missing)!r}, extra={sorted(extra)!r}")
    return sorted(extra)


def validate_csv(config_path, *, progress=print):
    """Execute a CSV contract; all paths are relative to the config file.

    Repeated identical input/contract/code produces identical outputs. Failed
    quarantine thresholds publish diagnostics before raising, structural errors
    publish nothing, and neither case changes the raw file or NYC checkpoint.
    """
    raw, contract_path, root, encoding, delimiter, expected = _settings(config_path)
    contract = load_contract(contract_path)
    source_hash = _digest(raw)
    if expected is not None and source_hash != expected:
        raise DataError(f"CSV checksum differs from expected_sha256: {raw}")
    contract_hash = hashlib.sha256(canonical_json(contract).encode("utf-8")).hexdigest()
    implementation = hashlib.sha256()
    for filename in ("csv_quality.py", "csv_contract.py", "profiling.py"):
        implementation.update((Path(__file__).parent / filename).read_bytes())
    implementation_hash = implementation.hexdigest()
    identity = {"version": CSV_QUALITY_VERSION, "source_file": raw.name,
                "source_sha256": source_hash, "contract_sha256": contract_hash,
                "implementation_sha256": implementation_hash, "encoding": encoding, "delimiter": delimiter}
    run = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    target = root / run
    counts = Counter(raw_records=0, accepted_records=0, quarantined_records=0)
    reasons, warnings = Counter(), Counter()
    targets = [f["name"] for f in contract["fields"] if f["role"] == "target"]
    target_counts = {name: Counter() for name in targets}
    columns = [f["name"] for f in contract["fields"]]
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".csv-validation-", dir=root) as temporary:
        temporary = Path(temporary)
        artifacts = temporary / "artifacts"
        artifacts.mkdir()
        with ExitStack() as stack:
            connection = sqlite3.connect(temporary / "quality.sqlite")
            stack.callback(connection.close)
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("CREATE TABLE seen_keys (key TEXT PRIMARY KEY, raw_hash TEXT) WITHOUT ROWID")
            profiler = ColumnProfiler(connection, fields=columns)
            accepted = stack.enter_context((artifacts / "accepted.jsonl").open("w", encoding="utf-8", newline="\n"))
            quarantine = stack.enter_context((artifacts / "quarantine.jsonl").open("w", encoding="utf-8", newline="\n"))
            accepted_csv = stack.enter_context((artifacts / "accepted.csv").open("w", encoding="utf-8", newline=""))
            writer = csv.DictWriter(accepted_csv, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            source = stack.enter_context(raw.open(encoding=encoding, newline=""))
            reader = csv.reader(source, delimiter=delimiter, strict=True)
            try:
                header = next(reader, None)
                extras = _check_header(header, contract)
                for row_number, values in enumerate(reader, 1):
                    counts["raw_records"] += 1
                    flags, clean = [], {}
                    if len(values) != len(header):
                        record = {"header": header, "values": values}
                        errors = [{"field": None, "code": "CSV_ROW_WIDTH_MISMATCH"}]
                    else:
                        record = dict(zip(header, values))
                        clean, errors, flags = normalize_row(record, contract)
                    if not errors and contract["primary_key"]:
                        key = canonical_json([clean[name] for name in contract["primary_key"]])
                        raw_hash = hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()
                        previous = connection.execute("SELECT raw_hash FROM seen_keys WHERE key=?", (key,)).fetchone()
                        if previous is None:
                            connection.execute("INSERT INTO seen_keys VALUES (?, ?)", (key, raw_hash))
                        else:
                            code = "DUPLICATE_RECORD" if previous[0] == raw_hash else "DUPLICATE_KEY_CONFLICT"
                            errors.append({"field": contract["primary_key"], "code": code})
                    provenance = {"source_file": raw.name, "source_row": row_number}
                    if errors:
                        codes = sorted({e["code"] for e in errors})
                        counts["quarantined_records"] += 1
                        reasons.update(codes)
                        quarantine.write(canonical_json({**provenance, "source_line_end": reader.line_num,
                            "reason_codes": codes, "errors": errors, "record": record}) + "\n")
                    else:
                        counts["accepted_records"] += 1
                        warnings.update(flags)
                        profiler.add(clean)
                        for name in targets:
                            target_counts[name][canonical_json(clean[name])] += 1
                        row = {**clean, "quality_flags": flags, **provenance}
                        accepted.write(canonical_json(row) + "\n")
                        writer.writerow(clean)
                    if progress is not None and counts["raw_records"] % 25000 == 0:
                        progress(f"Validated {counts['raw_records']:,} CSV records: accepted={counts['accepted_records']:,}, quarantined={counts['quarantined_records']:,}")
            except (csv.Error, UnicodeError) as exc:
                raise DataError(f"Malformed CSV near physical line {reader.line_num}: {exc}") from exc
            if not counts["raw_records"]:
                raise DataError("CSV has a header but no data records.")
            profile = profiler.result()
        if _digest(raw) != source_hash:
            raise DataError("CSV changed during validation; no result was published.")
        if counts["raw_records"] != counts["accepted_records"] + counts["quarantined_records"]:
            raise DataError("CSV validation accounting failed.")
        fraction = counts["quarantined_records"] / counts["raw_records"]
        passed = fraction <= contract["max_quarantine_fraction"]
        _json(artifacts / "quality_profile.json", profile)
        _json(artifacts / "contract.json", contract)
        report = {**identity, "dataset": contract["dataset"], "status": "passed" if passed else "failed",
                  **dict(counts), "quarantine_fraction": fraction,
                  "max_quarantine_fraction": contract["max_quarantine_fraction"],
                  "reason_counts": dict(sorted(reasons.items())), "reason_counts_may_overlap": True,
                  "warning_counts": dict(sorted(warnings.items())), "warnings_scope": "Accepted records only",
                  "target_counts": {name: dict(sorted(c.items())) for name, c in target_counts.items()},
                  "source_columns": header, "ignored_source_columns": extras,
                  "source_row_definition": "One-based CSV data record excluding header; source_line_end is the physical line including header.",
                  "scope_note": "Entire supplied local CSV; this does not certify completeness of an external dataset.",
                  "feature_columns": [f["name"] for f in contract["fields"] if f["role"] == "feature"],
                  "primary_key": contract["primary_key"],
                  "files": {name: {"sha256": _digest(artifacts / name)} for name in
                            ("accepted.jsonl", "accepted.csv", "quarantine.jsonl", "quality_profile.json", "contract.json")}}
        _json(artifacts / "validation_report.json", report)
        if target.exists():
            for name in (*report["files"], "validation_report.json"):
                if not (target / name).is_file() or _digest(target / name) != _digest(artifacts / name):
                    raise DataError(f"Existing CSV validation artifact differs; refusing to overwrite: {target / name}")
        else:
            try:
                os.rename(artifacts, target)
            except FileExistsError as exc:
                raise DataError("Another run published concurrently; rerun validation.") from exc
    _latest(root / "latest.json", {"run": run, "report": f"{run}/validation_report.json", "status": report["status"]})
    report_path = target / "validation_report.json"
    if not passed:
        raise QuarantineThresholdError(f"CSV quarantine fraction {fraction:.2%} exceeds {contract['max_quarantine_fraction']:.2%}; diagnostics: {report_path}", report_path=report_path)
    return {**report, "output_dir": str(target), "report_path": str(report_path)}
