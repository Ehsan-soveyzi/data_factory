"""Pinned population reference acquisition and a streaming, row-preserving join."""

import base64
import binascii
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .exceptions import ConfigurationError, DataError
from .http_client import HttpClient
from .profiling import canonical_json
from .quality import _atomic_json, _digest, _hash_json, _write_json


POPULATION_VERSION = 1
JOIN_FIELDS = {
    "population_area", "population_geography", "reference_population",
    "population_reference_date", "reference_borough", "reference_neighborhood",
    "population_join_status", "borough_comparison", "population_rate_eligible",
}


def _settings(config):
    try:
        settings = config["population"]
        for key in ("endpoint", "source_url", "documentation_url"):
            if not isinstance(settings[key], str) or not settings[key].startswith("https://"):
                raise ValueError(f"population.{key} must be an HTTPS URL.")
        if not re.fullmatch(r"[0-9a-f]{64}", settings["expected_sha256"]):
            raise ValueError("population.expected_sha256 must pin the source content.")
        if not re.fullmatch(r"[0-9a-f]{40}", settings["commit"]):
            raise ValueError("population.commit must pin a full Git commit.")
        if settings["geography"] != "MODZCTA":
            raise ValueError("This adapter supports the documented MODZCTA geography.")
        datetime.strptime(settings["reference_date"], "%Y-%m-%d")
        columns = settings["columns"]
        if set(columns) != {"area", "zip_codes", "population", "borough", "neighborhood"}:
            raise ValueError("population.columns must declare all five reference roles.")
        if any(not isinstance(v, str) or not v.strip() for v in columns.values()) or len(set(columns.values())) != 5:
            raise ValueError("Reference source column names must be distinct, nonempty strings.")
        boroughs = settings["allowed_boroughs"]
        if not isinstance(boroughs, list) or not boroughs or any(not isinstance(v, str) or not v or v != v.upper() for v in boroughs):
            raise ValueError("population.allowed_boroughs must contain uppercase names.")
        for key in ("max_reference_bytes", "max_reference_rows"):
            if type(settings[key]) is not int or settings[key] < 1:
                raise ValueError(f"population.{key} must be a positive integer.")
        for key in ("validation_dir", "population_dir"):
            value = config["paths"][key]
            if not isinstance(value, str) or not value or Path(value).name != value or value in (".", ".."):
                raise ValueError(f"paths.{key} must be a local directory name.")
        if config["paths"]["validation_dir"] == config["paths"]["population_dir"]:
            raise ValueError("Validation and population output directories must differ.")
        reference_dir = Path(config["paths"]["reference_dir"])
        curated = Path(config["paths"]["curated_dir"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConfigurationError(f"Invalid population settings: {exc}") from exc
    return settings, reference_dir, curated


def parse_reference(csv_text, settings):
    """Validate every reference row; refuse ambiguous aliases and duplicate areas.

    Population belongs to a MODZCTA, including all its listed ZIP aliases. It is
    never divided between aliases or added once for every alias.
    """
    if not isinstance(csv_text, str) or len(csv_text.encode("utf-8")) > settings["max_reference_bytes"]:
        raise DataError("Population reference is not text or exceeds its configured size limit.")
    columns = settings["columns"]
    reader = csv.DictReader(io.StringIO(csv_text, newline=""), strict=True)
    areas, aliases = {}, {}
    try:
        names = reader.fieldnames
        if not names or len(names) != len(set(names)) or not set(columns.values()).issubset(names):
            raise ValueError("Population source columns are missing or duplicated.")
        for row_number, row in enumerate(reader, 2):
            if len(areas) >= settings["max_reference_rows"]:
                raise ValueError("Population reference exceeds max_reference_rows.")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV row {row_number}.")
            values = {role: row[name].strip() for role, name in columns.items()}
            area = values["area"]
            if not re.fullmatch(r"[0-9]{5}", area) or area == "00000" or area in areas:
                raise ValueError(f"Invalid or duplicate population area at row {row_number}.")
            borough = values["borough"].upper()
            if borough not in settings["allowed_boroughs"]:
                raise ValueError(f"Unknown reference borough at row {row_number}.")
            population = float(values["population"])
            if not math.isfinite(population) or population < 0:
                raise ValueError(f"Invalid reference population at row {row_number}.")
            zip_codes = [value.strip() for value in values["zip_codes"].split(",")]
            if not zip_codes or any(not re.fullmatch(r"[0-9]{5}", value) or value == "00000" for value in zip_codes):
                raise ValueError(f"Invalid ZIP alias at row {row_number}.")
            if len(set(zip_codes)) != len(zip_codes) or area not in zip_codes:
                raise ValueError(f"Repeated ZIP alias or absent area alias at row {row_number}.")
            if any(value in aliases for value in zip_codes):
                raise ValueError(f"A ZIP maps to multiple population areas at row {row_number}.")
            areas[area] = {
                "population_area": area, "zip_codes": sorted(zip_codes),
                "reference_population": population, "reference_borough": borough,
                "reference_neighborhood": values["neighborhood"] or None,
            }
            aliases.update({value: area for value in zip_codes})
    except (ValueError, OverflowError, csv.Error) as exc:
        raise DataError(f"Invalid population reference: {exc}") from exc
    if not areas:
        raise DataError("Population reference has no data rows.")
    return areas, aliases


def _read_json(path):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object.")
        return result
    except (ValueError, UnicodeError) as exc:
        raise DataError(f"Invalid JSON in {path.name}: {exc}") from exc


def _publish(artifacts, target):
    if target.exists():
        if set(p.name for p in artifacts.iterdir()) != set(p.name for p in target.iterdir()):
            raise DataError("Existing population output has a different file set; refusing to overwrite it.")
        for path in artifacts.iterdir():
            existing = target / path.name
            if not existing.is_file() or _digest(existing) != _digest(path):
                raise DataError("Existing population output differs for the same inputs; refusing to overwrite it.")
    else:
        try:
            os.rename(artifacts, target)
        except FileExistsError as exc:
            raise DataError("Another population run published these inputs; rerun the stage.") from exc


def prepare_population_reference(config, *, csv_text=None, acquired_via=None, session=None):
    """Use a verified local cache, or acquire the pinned file through GitHub JSON.

    csv_text imports a copy retrieved through another verified transport (for
    example the connected GitHub tool). Its exact bytes must match the same pin.
    A cache hit performs no HTTP requests. Changes require a new source pin.
    """
    settings, reference_dir, _ = _settings(config)
    identity = _hash_json(settings)
    target = reference_dir / identity
    if csv_text is not None:
        if not isinstance(csv_text, str) or hashlib.sha256(csv_text.encode("utf-8")).hexdigest() != settings["expected_sha256"]:
            raise DataError("Imported reference does not match population.expected_sha256.")
    if target.exists():
        metadata = _read_json(target / "provenance.json")
        raw = (target / "source.csv").read_bytes()
        if metadata.get("source_fingerprint") != identity or metadata.get("sha256") != settings["expected_sha256"] or hashlib.sha256(raw).hexdigest() != settings["expected_sha256"]:
            raise DataError("Population cache checksum/provenance changed; refusing to use it.")
        text = raw.decode("utf-8")
        areas, aliases = parse_reference(text, settings)
        return {"areas": areas, "aliases": aliases, "provenance": metadata, "cache_dir": str(target)}
    if csv_text is None:
        with HttpClient(config["http"], session=session) as client:
            payload = client.request_json("GET", settings["endpoint"], params={"ref": settings["commit"]})
        try:
            if not isinstance(payload, dict) or payload["encoding"] != "base64" or type(payload["size"]) is not int or not 0 < payload["size"] <= settings["max_reference_bytes"]:
                raise ValueError("Expected a bounded GitHub base64 file response.")
            raw = base64.b64decode(payload["content"].replace("\n", "").replace("\r", ""), validate=True)
            if len(raw) != payload["size"] or hashlib.sha256(raw).hexdigest() != settings["expected_sha256"]:
                raise ValueError("Population source size/checksum does not match its pin.")
            csv_text = raw.decode("utf-8")
        except (KeyError, TypeError, ValueError, AttributeError, binascii.Error) as exc:
            raise DataError(f"Invalid population download: {exc}") from exc
        acquired_via = "GitHub Contents API through shared HttpClient"
    areas, aliases = parse_reference(csv_text, settings)
    metadata = {
        "source_fingerprint": identity, "source_url": settings["source_url"],
        "documentation_url": settings["documentation_url"], "commit": settings["commit"],
        "sha256": settings["expected_sha256"], "reference_date": settings["reference_date"],
        "geography": settings["geography"], "area_count": len(areas), "zip_alias_count": len(aliases),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "acquired_via": acquired_via or "Imported verified UTF-8 source copy",
    }
    reference_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".population-reference-", dir=reference_dir) as temporary:
        artifacts = Path(temporary) / "artifacts"
        artifacts.mkdir()
        (artifacts / "source.csv").write_bytes(csv_text.encode("utf-8"))
        _write_json(artifacts / "provenance.json", metadata)
        if target.exists():
            # Another fetch may have a different retrieval timestamp; verify its
            # actual committed cache instead of overwriting its provenance.
            return prepare_population_reference(config)
        _publish(artifacts, target)
    return {"areas": areas, "aliases": aliases, "provenance": metadata, "cache_dir": str(target)}


def _validated_input(config, curated):
    root = curated / config["paths"]["validation_dir"]
    try:
        latest = _read_json(root / "latest.json")
        run = latest["run"]
        if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{64}", run) or latest["report"] != f"{run}/validation_report.json":
            raise ValueError("Invalid validation pointer.")
        report_path = root / run / "validation_report.json"
        report = _read_json(report_path)
        if latest["status"] != "passed" or report["status"] != "passed":
            raise ValueError("Population join requires a passed validation result.")
        if type(report["accepted_records"]) is not int or report["accepted_records"] < 0:
            raise ValueError("Invalid accepted record count.")
        for name in ("accepted.jsonl", "quarantine.jsonl", "quality_profile.json"):
            if _digest(report_path.parent / name) != report["files"][name]["sha256"]:
                raise ValueError(f"Validated output checksum changed: {name}")
    except (KeyError, TypeError, ValueError, FileNotFoundError) as exc:
        raise DataError(f"No trustworthy validated input: {exc}. Run --stage validate first.") from exc
    return report_path, report


def enrich_case(record, areas, aliases, settings):
    """Attach one reference at most, preserving source borough and quality flags."""
    if not isinstance(record, dict) or JOIN_FIELDS.intersection(record):
        raise DataError("Accepted input is not a case object or already contains population columns.")
    zip_code = record.get("incident_zip")
    if zip_code is not None and (not isinstance(zip_code, str) or not re.fullmatch(r"[0-9]{5}", zip_code)):
        raise DataError("Validated ZIP must be five-digit text or null.")
    area = areas.get(aliases.get(zip_code))
    status = "matched" if area else "missing_zip" if zip_code is None else "unmatched_zip"
    source_borough = record.get("borough")
    comparison = "unavailable"
    if area and source_borough is not None:
        comparison = "match" if source_borough == area["reference_borough"] else "mismatch"
    added = {
        "population_area": area["population_area"] if area else None,
        "population_geography": settings["geography"] if area else None,
        "reference_population": area["reference_population"] if area else None,
        "population_reference_date": settings["reference_date"] if area else None,
        "reference_borough": area["reference_borough"] if area else None,
        "reference_neighborhood": area["reference_neighborhood"] if area else None,
        "population_join_status": status, "borough_comparison": comparison,
        "population_rate_eligible": bool(area and area["reference_population"] > 0 and comparison != "mismatch"),
    }
    return {**record, **added}


def join_population(config, *, progress=print):
    """Left join all accepted cases, preserving order/counts and unmatched cases."""
    settings, _, curated = _settings(config)
    report_path, validation = _validated_input(config, curated)
    reference = prepare_population_reference(config)
    areas, aliases = reference["areas"], reference["aliases"]
    run_hash = _hash_json({"version": POPULATION_VERSION, "validation_report": _digest(report_path), "reference": settings})
    root = curated / config["paths"]["population_dir"]
    target = root / run_hash
    root.mkdir(parents=True, exist_ok=True)
    counts = Counter(input_records=0, output_records=0, rate_eligible_records=0)
    statuses, comparisons, unmatched, mismatch_pairs, area_cases = Counter(), Counter(), Counter(), Counter(), Counter()
    with tempfile.TemporaryDirectory(prefix=".population-join-", dir=root) as temporary:
        artifacts = Path(temporary) / "artifacts"
        artifacts.mkdir()
        with (report_path.parent / "accepted.jsonl").open(encoding="utf-8") as source, \
                (artifacts / "enriched_cases.jsonl").open("w", encoding="utf-8", newline="\n") as output, \
                (artifacts / "join_issues.jsonl").open("w", encoding="utf-8", newline="\n") as issues:
            for line_number, line in enumerate(source, 1):
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    raise DataError(f"Invalid accepted JSON at line {line_number}.") from exc
                clean = enrich_case(record, areas, aliases, settings)
                counts["input_records"] += 1
                counts["output_records"] += 1
                counts["rate_eligible_records"] += int(clean["population_rate_eligible"])
                statuses[clean["population_join_status"]] += 1
                comparisons[clean["borough_comparison"]] += 1
                if clean["population_area"] is not None:
                    area_cases[clean["population_area"]] += 1
                if clean["population_join_status"] == "unmatched_zip":
                    unmatched[clean["incident_zip"]] += 1
                if clean["borough_comparison"] == "mismatch":
                    mismatch_pairs[(clean["incident_zip"], clean["borough"], clean["reference_borough"])] += 1
                if clean["population_join_status"] != "matched" or clean["borough_comparison"] != "match" or not clean["population_rate_eligible"]:
                    issues.write(canonical_json({"accepted_row": line_number, "unique_key": clean.get("unique_key"),
                        "incident_zip": clean["incident_zip"], "population_join_status": clean["population_join_status"],
                        "source_borough": clean.get("borough"), "reference_borough": clean["reference_borough"],
                        "borough_comparison": clean["borough_comparison"], "population_rate_eligible": clean["population_rate_eligible"]}) + "\n")
                output.write(canonical_json(clean) + "\n")
        # Detect input replacement during the streaming pass before publishing.
        if _digest(report_path.parent / "accepted.jsonl") != validation["files"]["accepted.jsonl"]["sha256"]:
            raise DataError("Accepted input changed during population join.")
        if counts["input_records"] != validation["accepted_records"] or counts["input_records"] != counts["output_records"]:
            raise DataError("Population join row accounting failed.")
        with (artifacts / "population_areas.jsonl").open("w", encoding="utf-8", newline="\n") as file:
            for area, row in sorted(areas.items()):
                file.write(canonical_json({**row, "population_geography": settings["geography"],
                    "population_reference_date": settings["reference_date"], "matched_case_count": area_cases[area]}) + "\n")
        report = {
            "population_version": POPULATION_VERSION, "status": "passed", **counts,
            "snapshot_completed": validation["snapshot_completed"], "scope": validation["scope"],
            "scope_note": validation["scope_note"], "validation_report_sha256": _digest(report_path),
            "reference_source_url": settings["source_url"], "reference_documentation_url": settings["documentation_url"],
            "reference_sha256": settings["expected_sha256"], "reference_commit": settings["commit"],
            "reference_date": settings["reference_date"], "population_geography": settings["geography"],
            "reference_areas": len(areas), "reference_zip_aliases": len(aliases),
            "join_status_counts": dict(sorted(statuses.items())), "borough_comparison_counts": dict(sorted(comparisons.items())),
            "unmatched_zip_counts": dict(sorted(unmatched.items())),
            "borough_mismatches": [{"incident_zip": key[0], "source_borough": key[1], "reference_borough": key[2], "records": value}
                                   for key, value in sorted(mismatch_pairs.items())],
            "matched_reference_areas": len(area_cases),
            "population_policy": "Population is per MODZCTA, not per ZIP or case. Use population_areas.jsonl once per area for denominators; never sum population across case rows or ZIP aliases.",
            "limitations": ["The 2019 population baseline predates the selected cases; it is not a current population estimate.",
                "Published ZIP aliases are an approximate geography mapping, not an address-level spatial match.",
                "Borough mismatches are retained as findings and excluded from rate eligibility, not silently corrected.",
                "Missing source borough is retained; a matched reference may supply a separate reference borough for later aggregation."],
            "files": {name: {"sha256": _digest(artifacts / name)} for name in ("enriched_cases.jsonl", "join_issues.jsonl", "population_areas.jsonl")},
        }
        _write_json(artifacts / "population_join_report.json", report)
        _publish(artifacts, target)
    _atomic_json(root / "latest.json", {"run": run_hash, "report": f"{run_hash}/population_join_report.json", "status": "passed"})
    if progress is not None:
        progress(f"Population join: {counts['input_records']:,} -> {counts['output_records']:,} cases; matched={statuses['matched']:,}; borough mismatches={comparisons['mismatch']:,}")
    return {**report, "report_path": str(target / "population_join_report.json"), "output_dir": str(target),
            "reference_provenance_path": str(Path(reference["cache_dir"]) / "provenance.json")}
