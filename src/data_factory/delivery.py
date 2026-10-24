"""Offline final case delivery, disk-backed monthly summaries and run manifests."""

from collections import Counter
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import tomllib
from uuid import uuid4

from .exceptions import ConfigurationError, DataError
from .population import _publish, _read_json, _validated_input
from .profiling import canonical_json
from .quality import _atomic_json, _connect, _digest, _hash_json, _write_json


DELIVERY_VERSION = 1
METRICS = (
    "case_count", "recorded_closure_count", "missing_closure_count",
    "mean_closure_hours", "median_closure_hours", "rate_eligible_case_count",
    "rate_excluded_case_count", "complaints_per_population_scale",
)
ARTIFACTS = ("analytical_cases.jsonl", "monthly_summary_wide.csv", "delivery_report.json")


def _settings(config):
    try:
        settings = config["delivery"]
        scale = settings["population_scale"]
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
            raise ValueError("delivery.population_scale must be finite and positive.")
        if type(settings["max_months"]) is not int or not 1 <= settings["max_months"] <= 1200:
            raise ValueError("delivery.max_months must be between 1 and 1200.")
        if type(settings["decimal_places"]) is not int or not 0 <= settings["decimal_places"] <= 12:
            raise ValueError("delivery.decimal_places must be between 0 and 12.")
        name = config["paths"]["delivery_dir"]
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("paths.delivery_dir must be a local directory name.")
        if name in (config["paths"]["validation_dir"], config["paths"]["population_dir"]):
            raise ValueError("Delivery, validation and population directories must differ.")
        created = Path(config["paths"]["created_dir"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid delivery settings: {exc}") from exc
    return settings, created


def _months(scope, limit):
    try:
        start = datetime.fromisoformat(scope["start_date"])
        end = datetime.fromisoformat(scope["end_date_exclusive"])
        if start.tzinfo is not None or end.tzinfo is not None or start >= end:
            raise ValueError("Expected an increasing floating date interval.")
        if scope["order"] != "created_date ASC, unique_key ASC":
            raise ValueError("Month coverage requires chronological source acquisition.")
        month = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months = []
        while month < end:
            if len(months) >= limit:
                raise ValueError("Selected scope exceeds delivery.max_months.")
            following = month.replace(year=month.year + (month.month == 12), month=month.month % 12 + 1)
            months.append((month.strftime("%Y-%m"), month, following))
            month = following
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DataError(f"Invalid delivery scope: {exc}") from exc
    return start, end, months


def _input(config, created):
    validation_path, validation = _validated_input(config, created)
    root = created / config["paths"]["population_dir"]
    try:
        pointer = _read_json(root / "latest.json")
        run = pointer["run"]
        if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{64}", run) or pointer["report"] != f"{run}/population_join_report.json":
            raise ValueError("Invalid population pointer.")
        path = root / run / "population_join_report.json"
        joined = _read_json(path)
        if pointer["status"] != "passed" or joined["status"] != "passed":
            raise ValueError("Delivery requires a passed population join.")
        if joined["validation_report_sha256"] != _digest(validation_path):
            raise ValueError("Population join is stale relative to latest validation.")
        if joined["scope"] != validation["scope"] or joined["snapshot_completed"] != validation["snapshot_completed"]:
            raise ValueError("Population and validation scopes disagree.")
        for key in ("start_date", "end_date_exclusive"):
            if datetime.fromisoformat(joined["scope"][key]) != datetime.fromisoformat(config["source"][key]):
                raise ValueError("Configured time scope changed; rerun upstream stages.")
        for key in ("complaint_types", "boroughs"):
            if sorted(set(joined["scope"][key])) != sorted(set(config["source"][key])):
                raise ValueError("Configured source selection changed; rerun upstream stages.")
        if joined["scope"]["endpoint"] != config["source"]["endpoint"]:
            raise ValueError("Configured source endpoint changed.")
        for report_key, setting_key in (("reference_sha256", "expected_sha256"), ("reference_commit", "commit"), ("reference_date", "reference_date")):
            if joined[report_key] != config["population"][setting_key]:
                raise ValueError("Population reference changed; rerun the join.")
        for name in ("enriched_cases.jsonl", "join_issues.jsonl", "population_areas.jsonl"):
            if _digest(path.parent / name) != joined["files"][name]["sha256"]:
                raise ValueError(f"Population output checksum changed: {name}")
        if joined["output_records"] != validation["accepted_records"]:
            raise ValueError("Population and validation row counts disagree.")
        if validation["raw_records"] != validation["accepted_records"] + validation["quarantined_records"]:
            raise ValueError("Validation row accounting failed.")
    except (KeyError, TypeError, ValueError, FileNotFoundError) as exc:
        raise DataError(f"No trustworthy population input: {exc}. Run --stage population first.") from exc
    return path, joined, validation_path, validation


def _lines(path):
    with path.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Expected a case object.")
            except (ValueError, UnicodeError) as exc:
                raise DataError(f"Invalid {path.name} row {number}: {exc}") from exc
            yield value


def code_version():
    """Fingerprint installed Python sources, including uncommitted implementation."""
    package = Path(__file__).resolve().parent
    sources = {path.name: _digest(path) for path in sorted(package.glob("*.py"))}
    project = package.parent.parent
    pyproject = project / "pyproject.toml"
    version = "unknown"
    if pyproject.is_file():
        sources["pyproject.toml"] = _digest(pyproject)
        version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    try:
        result = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        commit = result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        commit = None
    return {"package_version": version, "git_commit": commit, "source_sha256": _hash_json(sources)}


def _load_areas(connection, path, expected_count):
    connection.execute("CREATE TABLE areas (area_key TEXT PRIMARY KEY, area TEXT, borough TEXT, neighborhood TEXT, population REAL)")
    count = 0
    for row in _lines(path):
        try:
            area = row["population_area"]
            population = row["reference_population"]
            if not isinstance(area, str) or not re.fullmatch(r"[0-9]{5}", area):
                raise ValueError("Invalid area ID.")
            if isinstance(population, bool) or not isinstance(population, (int, float)) or not math.isfinite(population) or population < 0:
                raise ValueError("Invalid population.")
            connection.execute("INSERT INTO areas VALUES (?, ?, ?, ?, ?)",
                (f"MODZCTA:{area}", area, row["reference_borough"], row["reference_neighborhood"], population))
            count += 1
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
            raise DataError(f"Invalid population area table: {exc}") from exc
    if count != expected_count:
        raise DataError("Population area count disagrees with join report.")


def _case(record, start, end, complaints, connection):
    try:
        key = record["unique_key"]
        created = datetime.fromisoformat(record["created_date"])
        if not isinstance(key, str) or not key or not start <= created < end:
            raise ValueError("Invalid identifier or creation date.")
        complaint = record["complaint_type"]
        if complaint not in complaints or record["created_month"] != created.strftime("%Y-%m"):
            raise ValueError("Complaint or month is outside the validated scope.")
        closed = datetime.fromisoformat(record["closed_date"]) if record["closed_date"] is not None else None
        hours = record["closure_hours"]
        expected = (closed - created).total_seconds() / 3600 if closed is not None else None
        if expected is None:
            if hours is not None or record["has_recorded_closure"] is not False:
                raise ValueError("Missing closure must retain a null duration.")
        elif isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours < 0 or not math.isclose(hours, expected, abs_tol=1e-9) or record["has_recorded_closure"] is not True:
            raise ValueError("Closure duration disagrees with recorded dates.")
        area = record["population_area"]
        eligible = record["population_rate_eligible"]
        if type(eligible) is not bool:
            raise ValueError("Invalid rate eligibility flag.")
        if area is not None:
            area_key = f"MODZCTA:{area}"
            reference = connection.execute("SELECT population, borough FROM areas WHERE area_key=?", (area_key,)).fetchone()
            if reference is None or record["reference_population"] != reference[0] or record["reference_borough"] != reference[1]:
                raise ValueError("Case population disagrees with unique area reference.")
            if eligible != (reference[0] > 0 and record["borough_comparison"] != "mismatch"):
                raise ValueError("Inconsistent rate eligibility.")
        else:
            zip_code = record["incident_zip"]
            area_key = f"UNMATCHED_ZIP:{zip_code}" if zip_code is not None else "MISSING_ZIP"
            if eligible or record["reference_population"] is not None:
                raise ValueError("Unmatched cases cannot have population or rate eligibility.")
            connection.execute("INSERT OR IGNORE INTO areas VALUES (?, NULL, NULL, NULL, NULL)", (area_key,))
        if "analysis_area_key" in record or "closure_days" in record:
            raise ValueError("Input already contains final delivery columns.")
        return {**record, "analysis_area_key": area_key, "closure_days": hours / 24 if hours is not None else None}, created
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DataError(f"Invalid analytical case: {exc}") from exc


def _coverage(months, start, end, completed, observed_end):
    result = {}
    for name, left, right in months:
        scope_left, scope_right = max(left, start), min(right, end)
        full = scope_left == left and scope_right == right
        if completed or (observed_end is not None and observed_end >= scope_right):
            result[name] = "complete" if full else "partial_scope"
        elif observed_end is not None and observed_end >= scope_left:
            result[name] = "partial_acquisition"
        else:
            result[name] = "not_acquired"
    return result


def _summary(connection, path, months, coverage, settings, borough_filter):
    connection.executescript("""
        CREATE INDEX cases_groups ON cases (area_key, complaint, month);
        CREATE TABLE monthly AS SELECT area_key, complaint, month,
            COUNT(*) AS cases, COUNT(hours) AS closed, AVG(hours) AS mean_hours,
            SUM(eligible) AS eligible FROM cases GROUP BY area_key, complaint, month;
        CREATE TABLE medians AS
            SELECT area_key, complaint, month, AVG(hours) AS median_hours FROM (
                SELECT area_key, complaint, month, hours,
                    ROW_NUMBER() OVER (PARTITION BY area_key, complaint, month ORDER BY hours, id) AS rn,
                    COUNT(*) OVER (PARTITION BY area_key, complaint, month) AS n
                FROM cases WHERE hours IS NOT NULL
            ) WHERE rn IN ((n+1)/2, (n+2)/2) GROUP BY area_key, complaint, month;
        CREATE UNIQUE INDEX monthly_groups ON monthly(area_key, complaint, month);
        CREATE UNIQUE INDEX median_groups ON medians(area_key, complaint, month);
    """)
    count = 0
    fields = ["analysis_area_key", "population_area", "reference_borough", "reference_neighborhood",
              "reference_population", "complaint_type", "metric", *coverage]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(fields)
        groups = connection.execute("SELECT a.*, c.name FROM areas a CROSS JOIN complaints c ORDER BY a.area_key, c.name")
        for area_key, area, borough, neighborhood, population, complaint in groups:
            values = {row[0]: row[1:] for row in connection.execute("""
                SELECT m.month, m.cases, m.closed, m.mean_hours, m.eligible, d.median_hours
                FROM monthly m LEFT JOIN medians d USING(area_key, complaint, month)
                WHERE m.area_key=? AND m.complaint=? ORDER BY m.month
            """, (area_key, complaint))}
            for metric in METRICS:
                cells = []
                for name, _, _ in months:
                    if coverage[name] == "not_acquired":
                        cells.append(None)
                        continue
                    cases, closed, mean, eligible, median = values.get(name, (0, 0, None, 0, None))
                    rate = None
                    if population and coverage[name] == "complete" and not borough_filter:
                        rate = eligible / population * settings["population_scale"]
                    metrics = (cases, closed, cases - closed, mean, median, eligible, cases - eligible, rate)
                    value = metrics[METRICS.index(metric)]
                    cells.append(round(value, settings["decimal_places"]) if isinstance(value, float) else value)
                writer.writerow([area_key, area, borough, neighborhood, population, complaint, metric, *cells])
                count += 1
    return count


def build_delivery(config, *, progress=print, started_at=None, execution_stage="finalize"):
    """Publish deterministic analytics and a separate manifest for this execution.

    Clock timestamps are deliberately excluded from analytical content hashes.
    Every attempt gets its own manifest rather than forging repeatable timings.
    """
    started_at = started_at or datetime.now(timezone.utc).isoformat()
    settings, created = _settings(config)
    join_path, joined, validation_path, validation = _input(config, created)
    start, end, months = _months(joined["scope"], settings["max_months"])
    version = code_version()
    identity = {"delivery_version": DELIVERY_VERSION, "join_report_sha256": _digest(join_path),
                "validation_report_sha256": _digest(validation_path), "settings": settings, "code_version": version}
    run = _hash_json(identity)
    root = created / config["paths"]["delivery_dir"]
    target = root / run
    root.mkdir(parents=True, exist_ok=True)
    counts = Counter(analytical_records=0, recorded_closure_records=0, rate_eligible_records=0)
    first = last = None
    input_hashes = {p: _digest(p) for p in (
        join_path, validation_path,
        *(join_path.parent / name for name in ("enriched_cases.jsonl", "join_issues.jsonl", "population_areas.jsonl")),
        *(validation_path.parent / name for name in ("accepted.jsonl", "quarantine.jsonl", "quality_profile.json")),
    )}
    with tempfile.TemporaryDirectory(prefix=".delivery-", dir=root) as temporary:
        work = Path(temporary)
        artifacts = work / "artifacts"
        artifacts.mkdir()
        connection = _connect(work / "summary.sqlite")
        try:
            _load_areas(connection, join_path.parent / "population_areas.jsonl", joined["reference_areas"])
            connection.execute("CREATE TABLE cases (id TEXT PRIMARY KEY, area_key TEXT, complaint TEXT, month TEXT, hours REAL, eligible INTEGER)")
            connection.execute("CREATE TABLE complaints (name TEXT PRIMARY KEY)")
            complaints = sorted(set(joined["scope"]["complaint_types"]))
            connection.executemany("INSERT INTO complaints VALUES (?)", ((c,) for c in complaints))
            with (artifacts / "analytical_cases.jsonl").open("w", encoding="utf-8", newline="\n") as output:
                for record in _lines(join_path.parent / "enriched_cases.jsonl"):
                    clean, date = _case(record, start, end, complaints, connection)
                    if last is not None and date < last:
                        raise DataError("Analytical input is not chronologically ordered.")
                    first = date if first is None else first
                    last = date
                    try:
                        connection.execute("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?)",
                            (clean["unique_key"], clean["analysis_area_key"], clean["complaint_type"],
                             clean["created_month"], clean["closure_hours"], int(clean["population_rate_eligible"])))
                    except sqlite3.IntegrityError as exc:
                        raise DataError("Duplicate final case identifier.") from exc
                    counts["analytical_records"] += 1
                    counts["recorded_closure_records"] += clean["closure_hours"] is not None
                    counts["rate_eligible_records"] += clean["population_rate_eligible"]
                    output.write(canonical_json(clean) + "\n")
            if counts["analytical_records"] != joined["output_records"] or counts["rate_eligible_records"] != joined["rate_eligible_records"]:
                raise DataError("Final case counts disagree with population join.")
            coverage = _coverage(months, start, end, joined["snapshot_completed"], last)
            wide_rows = _summary(connection, artifacts / "monthly_summary_wide.csv", months, coverage,
                                 settings, joined["scope"]["boroughs"])
            grouped = connection.execute("SELECT COALESCE(SUM(cases),0), COALESCE(SUM(eligible),0) FROM monthly").fetchone()
            if grouped != (counts["analytical_records"], counts["rate_eligible_records"]):
                raise DataError("Monthly aggregation row accounting failed.")
        finally:
            connection.close()
        for path, digest in input_hashes.items():
            if _digest(path) != digest:
                raise DataError("Delivery input changed while processing; no output published.")
        report = {**identity, "status": "passed", "delivery_complete": joined["snapshot_completed"],
            "scope": joined["scope"], "scope_note": joined["scope_note"], **counts,
            "raw_records": validation["raw_records"], "accepted_records": validation["accepted_records"],
            "quarantined_records": validation["quarantined_records"], "reason_counts": validation["reason_counts"],
            "snapshot_hash": validation.get("snapshot_hash"), "schema_hash": validation.get("schema_hash"),
            "observed_created_min": first.isoformat() if first else None,
            "observed_created_max": last.isoformat() if last else None,
            "monthly_summary_rows": wide_rows, "month_coverage": coverage,
            "metrics": list(METRICS), "reference_date": joined["reference_date"],
            "rate_definition": "Eligible case count / unique MODZCTA population * population_scale; complete months only. Borough-filtered scopes have no rates because the denominator cannot be spatially restricted safely.",
            "duration_definition": "Recorded closed_date minus created_date, grouped by creation month; null closures excluded from means and medians, zero intervals included. This is not proof of real-world resolution.",
            "limitations": [*joined["limitations"],
                "Incomplete months have counts so far and blank rates; unacquired months have blank counts, not zeros.",
                "Complete-month coverage on a partial snapshot is inferred conservatively from chronologically ordered accepted records.",
                "Closure summaries describe recorded closures in this source snapshot; open cases and unequal follow-up can bias comparisons.",
                "Population rates describe only the configured complaint selection, not all NYC 311 complaints."],
            "files": {name: {"sha256": _digest(artifacts / name), "bytes": (artifacts / name).stat().st_size}
                      for name in ARTIFACTS[:2]}}
        _write_json(artifacts / "delivery_report.json", report)
        _publish(artifacts, target)
    execution_id = uuid4().hex
    manifest_path = root / "executions" / execution_id / "run_manifest.json"
    manifest = {"manifest_version": 1, "execution_id": execution_id, "execution_stage": execution_stage,
        "started_at_utc": started_at, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed", "delivery_complete": report["delivery_complete"], "scope": joined["scope"],
        "scope_note": joined["scope_note"], "observed_created_min": report["observed_created_min"],
        "observed_created_max": report["observed_created_max"], "raw_records": validation["raw_records"],
        "accepted_records": validation["accepted_records"], "quarantined_records": validation["quarantined_records"],
        "quarantine_reason_counts": validation["reason_counts"], "reason_counts_may_overlap": True,
        "snapshot_hash": validation.get("snapshot_hash"), "schema_hash": validation.get("schema_hash"),
        "quarantine_fraction": validation.get("quarantine_fraction"),
        "max_quarantine_fraction": validation.get("max_quarantine_fraction"),
        "analytical_records": counts["analytical_records"], "code_version": version, "artifact_run": run,
        "input_reports": {"validation_sha256": input_hashes[validation_path], "population_sha256": input_hashes[join_path]},
        "files": {name: {"path": f"{run}/{name}", "sha256": _digest(target / name), "bytes": (target / name).stat().st_size}
                  for name in ARTIFACTS},
        "determinism_policy": "Analytical files and delivery_report.json are byte-stable for identical frozen inputs, settings and code. This per-execution audit manifest has actual clock times and a unique execution ID, so its own hash intentionally varies. A manifest cannot contain its own hash."}
    _atomic_json(manifest_path, manifest)
    _atomic_json(root / "latest.json", {"run": run, "report": f"{run}/delivery_report.json", "status": "passed",
        "delivery_complete": report["delivery_complete"], "manifest": f"executions/{execution_id}/run_manifest.json"})
    if progress is not None:
        progress(f"Final delivery: {counts['analytical_records']:,} cases; {wide_rows:,} wide rows; complete={report['delivery_complete']}")
    return {**report, "output_dir": str(target), "report_path": str(target / "delivery_report.json"),
            "manifest_path": str(manifest_path)}
