"""Resolve and inspect the factory's saved NYC JSONL artifacts in bounded batches."""

from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

from ..exceptions import DataError
from ..quality import _atomic_json, _digest


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _inside(directory, relative):
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise DataError("NYC artifact path leaves its publication directory.")
    return path


def _valid_population(value):
    try:
        number = Decimal(str(value))
        return number.is_finite() and 0 < number < Decimal("1e12") and number == number.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return False


def resolve_nyc_delivery(project_root):
    """Locate the current saved cases and matching population reference by hash."""
    root = Path(project_root).resolve()
    config = _read(root / "config/setting.json")
    curated = root / config["paths"]["curated_dir"]
    delivery = curated / config["paths"]["delivery_dir"]
    pointer_path = delivery / "latest.json"
    pointer = _read(pointer_path)
    run = pointer["run"]
    if not re.fullmatch(r"[0-9a-f]{64}", run) or pointer["report"] != f"{run}/delivery_report.json":
        raise DataError("Invalid NYC delivery pointer.")
    manifest_path = _inside(delivery, pointer["manifest"])
    manifest = _read(manifest_path)
    report_path = _inside(delivery, pointer["report"])
    if _digest(report_path) != manifest["files"]["delivery_report.json"]["sha256"]:
        raise DataError("NYC delivery report checksum changed.")
    report = _read(report_path)
    if any(value.get("status") != "passed" for value in (pointer, manifest, report)):
        raise DataError("NYC inspection requires a passed saved delivery.")
    if manifest["artifact_run"] != run:
        raise DataError("NYC manifest and latest pointer select different runs.")
    cases_meta = manifest["files"]["analytical_cases.jsonl"]
    if cases_meta["path"] != f"{run}/analytical_cases.jsonl":
        raise DataError("NYC cases path does not belong to the selected run.")
    cases = _inside(delivery, cases_meta["path"])
    if cases.stat().st_size != cases_meta["bytes"] or _digest(cases) != cases_meta["sha256"]:
        raise DataError("NYC analytical cases checksum or size changed.")
    population = curated / config["paths"]["population_dir"]
    population_pointer_path = population / "latest.json"
    population_pointer = _read(population_pointer_path)
    population_report_path = _inside(population, population_pointer["report"])
    if _digest(population_report_path) != report["join_report_sha256"]:
        raise DataError("The current population reference belongs to a different NYC delivery.")
    population_report = _read(population_report_path)
    areas = population_report_path.parent / "population_areas.jsonl"
    if population_report.get("status") != "passed" or _digest(areas) != population_report["files"]["population_areas.jsonl"]["sha256"]:
        raise DataError("NYC population reference checksum changed or its join failed.")
    proposal_path = root / "config/nyc_schema.json"
    proposal = _read(proposal_path)
    inputs = {path.relative_to(root).as_posix(): _digest(path) for path in (
        pointer_path, manifest_path, report_path, cases, population_pointer_path, population_report_path, areas, proposal_path,
    )}
    return {"root": root, "path": cases, "areas_path": areas, "report": report,
            "manifest": manifest, "population_report": population_report, "proposal": proposal, "inputs": inputs}


def read_nyc_batches(source, chunksize=10000):
    """Return a context-managed JSONL reader, preserving IDs and source dates."""
    if type(chunksize) is not int or chunksize < 1:
        raise ValueError("chunksize must be a positive integer.")
    dtypes = {column["source"]: "string" for column in source["proposal"]["case_columns"] if column["kind"] in ("string", "datetime", "date")}
    return pd.read_json(source["path"], lines=True, chunksize=chunksize, dtype=dtypes,
                        convert_dates=False, precise_float=True, encoding="utf-8")


def _verify_inputs(source):
    for relative, digest in source["inputs"].items():
        if _digest(source["root"] / relative) != digest:
            raise DataError(f"NYC input changed during inspection: {relative}")


def inspect_nyc_delivery(project_root, source=None, chunksize=10000):
    """Inspect every saved case; do not confuse a preview with the full dataset."""
    source = source or resolve_nyc_delivery(project_root)
    _verify_inputs(source)
    reference = pd.read_json(source["areas_path"], lines=True, convert_dates=False, precise_float=True,
                             dtype={"population_area": "string", "population_reference_date": "string"})
    aliases = reference[["population_area", "zip_codes"]].explode("zip_codes").rename(columns={"zip_codes": "incident_zip"})
    aliases["incident_zip"] = aliases["incident_zip"].astype("string")
    reference_errors = {
        "missing_area_keys": int(reference["population_area"].isna().sum()),
        "duplicate_area_keys": int(reference["population_area"].duplicated().sum()),
        "invalid_area_keys": int((~reference["population_area"].str.fullmatch(r"[0-9]{5}").fillna(False)).sum()),
        "duplicate_zip_aliases": int(aliases["incident_zip"].duplicated().sum()),
        "invalid_zip_aliases": int((~aliases["incident_zip"].str.fullmatch(r"[0-9]{5}").fillna(False)).sum()),
        "invalid_reference_population": int((~reference["reference_population"].map(_valid_population)).sum()),
    }
    ref = reference.set_index("population_area")
    reference_fields = source["proposal"]["tables"]["population_areas"]["columns"]
    reference_missing = {name: int(reference[name].isna().sum()) for name, spec in reference_fields.items() if not spec["nullable"]}
    reference_errors["missing_required_reference_values"] = sum(reference_missing.values())
    if any(reference_errors.values()):
        raise DataError(f"Invalid NYC population dimension: {reference_errors}")
    proposal = source["proposal"]["case_columns"]
    missing, invalid, lengths, dates = Counter(), Counter(), Counter(), {}
    seen, duplicates, rows = set(), 0, 0
    case_areas, join_status, borough_status, quality_flags = Counter(), Counter(), Counter(), Counter()
    checks = Counter({key: 0 for key in (
        "unknown_population_area_rows", "population_value_mismatch_rows", "reference_attribute_mismatch_rows",
        "population_alias_mismatch_rows", "population_join_status_mismatch_rows", "rate_eligibility_mismatch_rows",
        "negative_closure_rows", "closure_hours_mismatch_rows", "closure_days_mismatch_rows",
        "closure_flag_mismatch_rows", "created_month_mismatch_rows",
    )})
    known_fields = {column["source"] for column in proposal}
    with read_nyc_batches(source, chunksize) as batches:
        for frame in batches:
            if set(frame.columns) != known_fields:
                raise DataError("NYC analytical field set differs from the explicit schema proposal.")
            rows += len(frame)
            parsed = {}
            for column in proposal:
                name, kind = column["source"], column["kind"]
                values = frame[name]
                absent = values.isna()
                if kind in ("string", "datetime", "date"):
                    absent |= values.fillna("").str.strip().eq("")
                    lengths[name] = max(lengths[name], int(values.str.len().max()) if values.notna().any() else 0)
                missing[name] += int(absent.sum())
                present = ~absent
                if kind in ("datetime", "date"):
                    converted = pd.to_datetime(values, format="ISO8601" if kind == "datetime" else "%Y-%m-%d", errors="coerce")
                    if converted.dt.tz is not None:
                        raise DataError("NYC timestamps unexpectedly carry a timezone; review the SQL proposal.")
                    invalid[name] += int((present & converted.isna()).sum())
                    parsed[name] = converted
                    if converted.notna().any():
                        bounds = dates.setdefault(name, {"min": converted.min().isoformat(), "max": converted.max().isoformat()})
                        bounds["min"] = min(bounds["min"], converted.min().isoformat())
                        bounds["max"] = max(bounds["max"], converted.max().isoformat())
                elif kind == "float":
                    numbers = pd.to_numeric(values, errors="coerce")
                    bad = present & (numbers.isna() | ~np.isfinite(numbers.astype(float)))
                    if name == "latitude": bad |= present & ~numbers.between(-90, 90)
                    if name == "longitude": bad |= present & ~numbers.between(-180, 180)
                    invalid[name] += int(bad.sum())
                elif kind == "array":
                    invalid[name] += int((present & ~values.map(lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value))).sum())
                elif kind == "boolean":
                    invalid[name] += int((present & ~values.map(lambda value: isinstance(value, (bool, np.bool_)))).sum())
            ids = frame["unique_key"].dropna()
            duplicates += int(ids.duplicated().sum()) + int(ids.drop_duplicates().isin(seen).sum())
            seen.update(ids.tolist())
            invalid["unique_key"] += int((~ids.str.fullmatch(r"[0-9]+")).sum())
            for field in ("incident_zip", "population_area"):
                values = frame[field].dropna()
                invalid[field] += int((~values.str.fullmatch(r"[0-9]{5}")).sum())
            actual_area = frame["population_area"]
            matched = actual_area.notna()
            checks["unknown_population_area_rows"] += int((matched & ~actual_area.isin(ref.index)).sum())
            case_areas.update(actual_area.dropna().tolist())
            expected_population = actual_area.map(ref["reference_population"])
            checks["population_value_mismatch_rows"] += int((matched & ~np.isclose(frame["reference_population"].fillna(-1), expected_population.fillna(-2), rtol=0, atol=1e-8)).sum())
            for field in ("reference_borough", "reference_neighborhood", "population_geography", "population_reference_date"):
                expected = actual_area.map(ref[field])
                checks["reference_attribute_mismatch_rows"] += int((matched & frame[field].fillna("").ne(expected.fillna(""))).sum())
            alias_area = frame["incident_zip"].map(aliases.set_index("incident_zip")["population_area"])
            checks["population_alias_mismatch_rows"] += int((matched & actual_area.ne(alias_area).fillna(True)).sum())
            checks["population_join_status_mismatch_rows"] += int(frame["population_join_status"].eq("matched").ne(matched).sum())
            expected_rate = matched & expected_population.gt(0) & frame["borough_comparison"].ne("mismatch")
            checks["rate_eligibility_mismatch_rows"] += int(frame["population_rate_eligible"].ne(expected_rate).sum())
            start, end = parsed["created_date"], parsed["closed_date"]
            expected_hours = (end - start).dt.total_seconds() / 3600
            available = expected_hours.notna()
            checks["negative_closure_rows"] += int(expected_hours.lt(0).sum())
            checks["closure_hours_mismatch_rows"] += int((available & ~np.isclose(frame["closure_hours"].fillna(-1), expected_hours.fillna(-2), rtol=0, atol=1e-8)).sum())
            checks["closure_hours_mismatch_rows"] += int(frame["closure_hours"].isna().ne(end.isna()).sum())
            checks["closure_days_mismatch_rows"] += int((available & ~np.isclose(frame["closure_days"].fillna(-1), expected_hours.fillna(-2) / 24, rtol=0, atol=1e-9)).sum())
            checks["closure_days_mismatch_rows"] += int(frame["closure_days"].isna().ne(end.isna()).sum())
            checks["closure_flag_mismatch_rows"] += int(frame["has_recorded_closure"].ne(end.notna()).sum())
            checks["created_month_mismatch_rows"] += int(frame["created_month"].ne(start.dt.strftime("%Y-%m")).sum())
            join_status.update(frame["population_join_status"].value_counts().to_dict())
            borough_status.update(frame["borough_comparison"].value_counts().to_dict())
            for values in frame["quality_flags"]:
                if isinstance(values, list): quality_flags.update(values)
    checks["reference_matched_case_count_mismatch_rows"] = sum(int(case_areas.get(area, 0) != record["matched_case_count"]) for area, record in ref.iterrows())
    if rows != source["report"]["analytical_records"] or rows != source["manifest"]["analytical_records"]:
        raise DataError("NYC row count disagrees with its saved delivery report or manifest.")
    _verify_inputs(source)
    fields = [{**column, "missing_rows": missing[column["source"]], "invalid_rows": invalid[column["source"]],
               "maximum_text_length": lengths[column["source"]]} for column in proposal]
    required_missing = sum(item["missing_rows"] for item in fields if not item["nullable"])
    primary_missing = missing["unique_key"]
    passed = not (required_missing or duplicates or sum(invalid.values()) or sum(checks.values()))
    return {
        "status": "passed" if passed else "failed", "rows": rows, "source_columns": len(fields),
        "missing_primary_keys": primary_missing, "duplicate_primary_key_extra_rows": duplicates,
        "missing_required_values": required_missing, "fields": fields, "consistency_checks": dict(checks),
        "reference_rows": len(reference), "alias_rows": len(aliases), "reference_checks": reference_errors,
        "reference_missing": reference_missing, "dates": dates,
        "join_status_counts": dict(sorted(join_status.items())), "borough_comparison_counts": dict(sorted(borough_status.items())),
        "quality_flag_counts": dict(sorted(quality_flags.items())),
        "delivery_complete": source["report"]["delivery_complete"], "scope": source["report"]["scope"],
        "scope_note": source["report"]["scope_note"], "inputs": source["inputs"],
        "artifact": source["path"].relative_to(source["root"]).as_posix(),
        "implementation": {"file": "src/data_factory/database/nyc_inspection.py", "sha256": _digest(Path(__file__))},
        "database_writes": 0, "source_data_modified": False,
    }


def save_nyc_evidence(project_root, profile):
    """Write diagnostic evidence while leaving all stage 01 outputs untouched."""
    root = Path(project_root).resolve()
    for relative, fingerprint in profile["inputs"].items():
        if _digest(root / relative) != fingerprint:
            raise DataError(f"NYC source changed before publishing evidence: {relative}")
    output = root / "stages/stage_02/evidence/quality/nyc_source_checks.json"
    _atomic_json(output, profile)
    return output
