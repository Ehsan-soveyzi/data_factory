"""Deterministic Olist preparation, quarantine and atomic local publication.

This extends the same data_factory package. It reuses the factory's canonical
JSON, hashes, publication checks and per-execution manifest conventions. It does
not connect to PostgreSQL or modify the source CSVs.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import tempfile
from uuid import uuid4

import numpy as np
import pandas as pd

from .exceptions import DataError
from .population import _publish
from .profiling import canonical_json
from .quality import _atomic_json, _digest, _hash_json, _write_json
from .sources.olist import load_olist_snapshot


CLEANING_VERSION = 1
AUDIT_COLUMNS = ("source_row", "quality_flags")


def _flag(flags, mask, code):
    for index in mask.index[mask.fillna(False)]:
        flags[index].add(code)


def _error(errors, mask, column, code):
    for index in mask.index[mask.fillna(False)]:
        errors[index].add((column, code))


def normalize_table(raw, definition):
    """Normalize typed values while retaining source record ordinals and errors."""
    frame = pd.DataFrame(index=raw.index)
    flags, errors = defaultdict(set), defaultdict(set)
    changes = []
    for column in definition["columns"]:
        source, target, kind = column["source"], column["target"], column["kind"]
        original = raw[source].astype("string").fillna("")
        text = original.str.strip()
        blank = text.eq("")
        present = ~blank
        invalid = pd.Series(False, index=raw.index)
        missing_required = blank & (not column["nullable"])
        _error(errors, missing_required, source, "MISSING_REQUIRED")
        zero_unknown = pd.Series(False, index=raw.index)
        padded = pd.Series(False, index=raw.index)
        if kind == "text":
            if pattern := column.get("pattern"):
                invalid = present & ~text.str.fullmatch(pattern)
            # Preserve actual review wording and formatting; null only blanks.
            value = original.where(~blank, pd.NA) if source.startswith("review_comment_") else text.where(~blank, pd.NA)
        elif kind == "postal":
            invalid = present & ~text.str.fullmatch(r"[0-9]{1,5}")
            padded = present & ~invalid & text.str.len().lt(5)
            value = text.str.zfill(5).where(present & ~invalid, pd.NA)
        elif kind in ("timestamp", "date"):
            parsed = pd.to_datetime(text.where(present), format="%Y-%m-%d %H:%M:%S", errors="coerce")
            invalid = present & parsed.isna()
            if kind == "date":
                invalid |= parsed.notna() & parsed.ne(parsed.dt.normalize())
                value = parsed.where(~invalid).dt.date
            else:
                value = parsed.where(~invalid)
        elif kind == "money":
            lookup = {}
            for item in text[present].unique():
                try:
                    exact = Decimal(item)
                    quantum = Decimal(1).scaleb(-column["scale"])
                    limit = Decimal(10) ** (column["precision"] - column["scale"])
                    good = exact.is_finite() and abs(exact) < limit and exact == exact.quantize(quantum)
                    good = good and exact >= Decimal(str(column.get("minimum", 0)))
                    lookup[item] = exact.quantize(quantum) if good else None
                except InvalidOperation:
                    lookup[item] = None
            value = text.map(lookup)
            invalid = present & value.isna()
        elif kind in ("integer", "real"):
            parsed = pd.to_numeric(text.where(present), errors="coerce")
            good = parsed.notna() & np.isfinite(parsed.astype(float))
            if kind == "integer":
                lower, upper = (-32768, 32767) if column["sql_type"] == "SMALLINT" else (-2147483648, 2147483647)
                good &= parsed.mod(1).eq(0) & parsed.ge(lower) & parsed.le(upper)
            if column.get("zero_policy") == "null_with_quality_flag":
                zero_unknown = present & parsed.eq(0).fillna(False)
            good &= parsed.ge(column.get("minimum", -np.inf)) & parsed.le(column.get("maximum", np.inf))
            invalid = present & ~good.fillna(False) & ~zero_unknown
            if kind == "integer":
                value = parsed.where(good & ~zero_unknown).astype("Int64")
            else:
                # Keep the source decimal text in CSV; the DB sink will map it
                # to DOUBLE PRECISION. Validation must not round coordinates.
                value = text.where(good, pd.NA)
        else:
            raise DataError(f"Unsupported Olist column kind: {kind}")
        if column["nullable"]:
            value = value.mask(invalid)
            _flag(flags, invalid, f"INVALID_OPTIONAL:{target}")
        else:
            _error(errors, invalid, source, "INVALID_VALUE")
        _flag(flags, zero_unknown, f"UNKNOWN_ZERO:{target}")
        frame[target] = value
        changes.append({
            "column": source, "target": target, "blank_to_null_rows": int(blank.sum()),
            "whitespace_only_to_null_rows": int((blank & original.ne("")).sum()),
            "trimmed_nonblank_rows": int((present & original.ne(text)).sum()) if not source.startswith("review_comment_") else 0,
            "zero_to_null_rows": int(zero_unknown.sum()), "invalid_optional_to_null_rows": int(invalid.sum()) if column["nullable"] else 0,
            "zero_padded_rows": int(padded.sum()), "invalid_required_rows": int(invalid.sum()) if not column["nullable"] else 0,
        })
    frame["source_row"] = pd.Series(np.arange(1, len(raw) + 1), index=raw.index, dtype="Int64")
    primary = definition["primary_key"]
    if set(primary).issubset(raw.columns):
        target_by_source = {column["source"]: column["target"] for column in definition["columns"]}
        key_columns = [target_by_source[key] for key in primary]
        complete = frame[key_columns].notna().all(axis=1)
        repeated = pd.Series(False, index=frame.index)
        repeated.loc[complete] = frame.loc[complete, key_columns].duplicated(keep=False)
        _error(errors, repeated, "+".join(key_columns), "DUPLICATE_PRIMARY_KEY")
    return frame, flags, errors, changes


def prepare_olist(frames, contract):
    """Return clean tables and complete audit accounting, without filesystem IO."""
    cleaned, flags, errors, column_changes = {}, {}, {}, []
    for name, definition in contract["tables"].items():
        frame, table_flags, table_errors, changes = normalize_table(frames[name], definition)
        cleaned[name], flags[name], errors[name] = frame, table_flags, table_errors
        column_changes.extend({"table": name, **change} for change in changes)
    candidate = {name: frame.drop(index=list(errors[name])).copy() for name, frame in cleaned.items()}

    # Complete the category reference from valid products, preserving untranslated
    # categories rather than dropping their products or inventing English names.
    categories = candidate["category_translation"]
    product_names = candidate["products"]["product_category_name"].dropna()
    missing_names = sorted(set(product_names) - set(categories["product_category_name"]))
    generated = []
    additions = []
    for name in missing_names:
        additions.append({"product_category_name": name, "product_category_name_english": pd.NA, "source_row": pd.NA})
        source_indices = candidate["products"].index[candidate["products"]["product_category_name"].eq(name).fillna(False)]
        generated.append({"table": "product_categories", "key": name, "reason": "MISSING_CATEGORY_TRANSLATION",
                          "source_table": "products", "source_rows": [int(index) + 1 for index in source_indices]})
    if additions:
        candidate["category_translation"] = pd.concat([categories, pd.DataFrame(additions)], ignore_index=True)
    untranslated = candidate["products"]["product_category_name"].isin(missing_names)
    _flag(flags["products"], untranslated, "MISSING_CATEGORY_TRANSLATION")

    # Remove all required-orphan children, including cascades through rejected
    # parents; gather every missing-parent error before removing a batch.
    while True:
        rejected = defaultdict(set)
        for relation in contract["relationships"]:
            child, parent, column = relation["child"], relation["parent"], relation["column"]
            values = candidate[child][column]
            unknown = values.notna() & ~values.isin(candidate[parent][relation["parent_column"]].dropna())
            _error(errors[child], unknown, column, "MISSING_PARENT")
            rejected[child].update(unknown.index[unknown])
        if not any(rejected.values()):
            break
        for name, indices in rejected.items():
            candidate[name] = candidate[name].drop(index=list(indices))

    # Deduplicate all canonical source columns, never just the postal prefix.
    geo = candidate["geolocation"]
    geo_columns = [column["target"] for column in contract["tables"]["geolocation"]["columns"]]
    duplicate_mask = geo.duplicated(geo_columns, keep="first")
    duplicate_audit = []
    if duplicate_mask.any():
        discarded = geo.loc[duplicate_mask, geo_columns + ["source_row"]]
        kept = geo.loc[~duplicate_mask, geo_columns + ["source_row"]]
        pairs = discarded.merge(kept, on=geo_columns, suffixes=("_discarded", "_kept"), validate="many_to_one")
        for discarded_row, kept_row in pairs[["source_row_discarded", "source_row_kept"]].sort_values("source_row_discarded").itertuples(index=False, name=None):
            duplicate_audit.append({"table": "geolocation", "source_row": int(discarded_row),
                                    "kept_source_row": int(kept_row), "reason": "EXACT_CANONICAL_DUPLICATE"})
    candidate["geolocation"] = geo.loc[~duplicate_mask].copy()

    # Preserve chronology inconsistencies, with explicit eligibility for each
    # affected duration. A reversed carrier event does not erase other facts.
    orders = candidate["orders"]
    for start, end, output, reason in (
        ("order_purchase_timestamp", "order_approved_at", "purchase_to_approval_duration_valid", "APPROVAL_BEFORE_PURCHASE"),
        ("order_approved_at", "order_delivered_carrier_date", "approval_to_carrier_duration_valid", "CARRIER_BEFORE_APPROVAL"),
        ("order_delivered_carrier_date", "order_delivered_customer_date", "carrier_to_customer_duration_valid", "CUSTOMER_BEFORE_CARRIER"),
        ("order_purchase_timestamp", "order_delivered_customer_date", "purchase_to_customer_duration_valid", "CUSTOMER_BEFORE_PURCHASE"),
    ):
        both = orders[start].notna() & orders[end].notna()
        reverse = both & orders[end].lt(orders[start])
        _flag(flags["orders"], reverse, reason)
        orders[output] = both & ~reverse
    delivered = orders["order_status"].eq("delivered")
    actual = orders["order_delivered_customer_date"].notna()
    _flag(flags["orders"], delivered & ~actual, "DELIVERED_WITHOUT_ACTUAL_DATE")
    _flag(flags["orders"], ~orders["order_id"].isin(candidate["order_payments"]["order_id"]), "ORDER_WITHOUT_PAYMENT")
    orders["delivery_delay_eligible"] = delivered & actual & orders["order_estimated_delivery_date"].notna() & orders["purchase_to_customer_duration_valid"]

    # Normalize the postal reference to unique prefixes without selecting an
    # arbitrary geographic point or rejecting customers outside point coverage.
    geo_prefixes = set(candidate["geolocation"]["geolocation_zip_code_prefix"].dropna())
    prefixes = set(geo_prefixes)
    for name, column in (("customers", "customer_zip_code_prefix"), ("sellers", "seller_zip_code_prefix")):
        prefixes.update(candidate[name][column].dropna())
        _flag(flags[name], ~candidate[name][column].isin(geo_prefixes), "NO_GEOLOCATION_MATCH")
    postal = pd.DataFrame({"zip_code_prefix": sorted(prefixes)})
    postal["has_geolocation"] = postal["zip_code_prefix"].isin(geo_prefixes)

    rows, flag_counts, flags_by_table = [], Counter(), {}
    for name, frame in candidate.items():
        if name == "category_translation":
            # Generated records have no translation-file row number.
            values = []
            for _, record in frame.iterrows():
                values.append(["MISSING_CATEGORY_TRANSLATION"] if pd.isna(record["source_row"]) else sorted(flags[name].get(int(record["source_row"]) - 1, ())))
        else:
            values = [sorted(flags[name].get(index, ())) for index in frame.index]
        frame["source_row"] = pd.to_numeric(frame["source_row"], errors="coerce").astype("Int64")
        frame["quality_flags"] = [canonical_json(value) for value in values]
        table_counts = Counter()
        for value in values:
            flag_counts.update(value)
            table_counts.update(value)
        flags_by_table[name] = dict(sorted(table_counts.items()))
        generated_count = len(missing_names) if name == "category_translation" else 0
        duplicates = len(duplicate_audit) if name == "geolocation" else 0
        accepted = len(frame) - generated_count
        raw_count = len(frames[name])
        if raw_count != accepted + len(errors[name]) + duplicates:
            raise DataError(f"Olist row accounting failed: {name}")
        rows.append({"table": name, "output_table": "product_categories" if name == "category_translation" else name,
                     "raw_rows": raw_count, "accepted_source_rows": accepted, "quarantined_rows": len(errors[name]),
                     "deduplicated_rows": duplicates, "generated_rows": generated_count, "output_rows": len(frame)})
    candidate["postal_areas"] = postal
    rows.append({"table": "postal_areas", "output_table": "postal_areas", "raw_rows": 0,
                 "accepted_source_rows": 0, "quarantined_rows": 0, "deduplicated_rows": 0,
                 "generated_rows": len(postal), "output_rows": len(postal)})

    quarantined = []
    for name in frames:
        for index in sorted(errors[name]):
            details = [{"field": field, "code": code} for field, code in sorted(errors[name][index])]
            quarantined.append({"table": name, "source_file": contract["tables"][name]["file"],
                                "source_row": int(index) + 1, "errors": details,
                                "reason_codes": sorted({item["code"] for item in details}),
                                "record": frames[name].loc[index].fillna("").astype(str).to_dict()})
    post_checks = validate_prepared(candidate, contract)
    return candidate, {
        "rows": rows, "column_changes": column_changes, "quality_flag_counts": dict(sorted(flag_counts.items())),
        "quality_flags_by_table": flags_by_table,
        "quarantine": quarantined, "deduplicated": duplicate_audit, "generated": generated,
        "post_checks": post_checks,
    }


def validate_prepared(frames, contract):
    """Enforce nonnull/key/FK invariants on the normalized output schema."""
    checks = []
    for name, definition in contract["tables"].items():
        frame = frames[name]
        names = {column["source"]: column["target"] for column in definition["columns"]}
        primary = [names[key] for key in definition["primary_key"]] if set(definition["primary_key"]).issubset(names) else ["source_row"]
        missing_keys = int(frame[primary].isna().any(axis=1).sum())
        duplicate_keys = int(frame.duplicated(primary).sum())
        required_missing = sum(int(frame[column["target"]].isna().sum()) for column in definition["columns"] if not column["nullable"])
        if missing_keys or duplicate_keys or required_missing:
            raise DataError(f"Prepared Olist key/nonnull check failed: {name}")
        checks.append({"table": name, "missing_keys": missing_keys, "duplicate_keys": duplicate_keys,
                       "missing_required_values": required_missing})
    for relation in contract["relationships"]:
        values = frames[relation["child"]][relation["column"]]
        bad = values.notna() & ~values.isin(frames[relation["parent"]][relation["parent_column"]].dropna())
        if bad.any():
            raise DataError(f"Prepared Olist foreign-key check failed: {relation}")
    postal = frames["postal_areas"]["zip_code_prefix"]
    if postal.isna().any() or not postal.str.fullmatch(r"[0-9]{5}").all() or postal.duplicated().any():
        raise DataError("Prepared postal reference is not unique.")
    geo_duplicates = int(frames["geolocation"].duplicated([
        column["target"] for column in contract["tables"]["geolocation"]["columns"]]).sum())
    if geo_duplicates:
        raise DataError("Prepared geolocation still contains exact duplicate points.")
    return {"status": "passed", "keys": checks, "unmatched_foreign_key_rows": 0,
            "geolocation_exact_duplicate_rows": geo_duplicates}


def output_schema(contract, frames):
    """Describe explicit SQL types for every column exported in the CSV files."""
    schemas = {}
    for name, definition in contract["tables"].items():
        columns = {column["target"]: {"sql_type": column["sql_type"], "nullable": column["nullable"]}
                   for column in definition["columns"]}
        columns["source_row"] = {"sql_type": "INTEGER", "nullable": name == "category_translation"}
        columns["quality_flags"] = {"sql_type": "JSONB", "nullable": False}
        if name == "orders":
            for column in ("purchase_to_approval_duration_valid", "approval_to_carrier_duration_valid",
                           "carrier_to_customer_duration_valid", "purchase_to_customer_duration_valid", "delivery_delay_eligible"):
                columns[column] = {"sql_type": "BOOLEAN", "nullable": False}
        if set(columns) != set(frames[name].columns):
            raise DataError(f"Exported Olist columns lack explicit type mapping: {name}")
        schemas["product_categories" if name == "category_translation" else name] = columns
    schemas["postal_areas"] = {"zip_code_prefix": {"sql_type": "CHAR(5)", "nullable": False},
                               "has_geolocation": {"sql_type": "BOOLEAN", "nullable": False}}
    return schemas


def _write_jsonl(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def run_olist_cleaning(project_root):
    """Prepare raw CSVs, verify integrity, then atomically publish local tables."""
    root = Path(project_root).resolve()
    started = datetime.now(timezone.utc).isoformat()
    raw, inputs, contract, contract_hash, settings = load_olist_snapshot(root)
    code_paths = {"src/data_factory/olist_cleaning.py": Path(__file__),
                  "src/data_factory/sources/olist.py": Path(__file__).parent / "sources/olist.py",
                  **{f"src/data_factory/{name}.py": Path(__file__).parent / f"{name}.py"
                     for name in ("quality", "population", "profiling")}}
    code_hashes = {name: _digest(path) for name, path in code_paths.items()}
    identity = {"cleaning_version": CLEANING_VERSION, "inputs": inputs,
                "contract_sha256": contract_hash, "source_code_sha256": code_hashes}
    run = _hash_json(identity)
    base = root / settings["paths"]["curated_dir"] / "olist"
    base.mkdir(parents=True, exist_ok=True)
    tables, audit = prepare_olist(raw, contract)
    target = base / run
    with tempfile.TemporaryDirectory(prefix=".cleaning-", dir=base) as temporary:
        artifacts = Path(temporary) / "artifacts"
        artifacts.mkdir()
        output_files = {}
        for name, frame in tables.items():
            filename = ("product_categories" if name == "category_translation" else name) + ".csv"
            frame.to_csv(artifacts / filename, index=False, na_rep="", lineterminator="\n",
                         date_format="%Y-%m-%d %H:%M:%S")
            output_files[filename] = {"sha256": _digest(artifacts / filename), "bytes": (artifacts / filename).stat().st_size,
                                      "rows": len(frame)}
        for name, key in (("quarantine.jsonl", "quarantine"), ("deduplicated.jsonl", "deduplicated"), ("generated.jsonl", "generated")):
            _write_jsonl(artifacts / name, audit[key])
            output_files[name] = {"sha256": _digest(artifacts / name), "bytes": (artifacts / name).stat().st_size,
                                 "rows": len(audit[key])}
        for item in inputs.values():
            if _digest(root / item["file"]) != item["sha256"]:
                raise DataError("Olist raw input changed during cleaning; no output published.")
        if _digest(root / "config/olist_schema.json") != contract_hash:
            raise DataError("Olist schema changed during cleaning; no output published.")
        report = {
            **identity, "status": "passed", "database_writes": 0,
            "table_rows": audit["rows"], "column_changes": audit["column_changes"],
            "quality_flag_counts": audit["quality_flag_counts"], "quality_flags_by_table": audit["quality_flags_by_table"],
            "post_checks": audit["post_checks"], "output_schema": output_schema(contract, tables),
            "quarantined_rows": len(audit["quarantine"]), "deduplicated_rows": len(audit["deduplicated"]),
            "generated_category_rows": len(audit["generated"]), "files": output_files,
            "source_row_definition": "One-based CSV data-record ordinal excluding the header; not a physical text line in multiline CSV.",
            "csv_null_policy": "An empty CSV field represents SQL NULL; the later sink must use the schema mapping, not infer types.",
            "geolocation_policy": "Deduplicate all five canonical source fields; keep distinct points for the same postal prefix.",
        }
        _write_json(artifacts / "cleaning_report.json", report)
        _publish(artifacts, target)
    execution_id = uuid4().hex
    manifest_path = base / "executions" / execution_id / "run_manifest.json"
    manifest = {
        "execution_id": execution_id, "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(), "execution_stage": "clean-olist",
        "run": run, **identity, "files": {**output_files, "cleaning_report.json": {"sha256": _digest(target / "cleaning_report.json")}},
        "quarantined_rows": report["quarantined_rows"], "deduplicated_rows": report["deduplicated_rows"],
        "database_writes": 0,
    }
    _atomic_json(manifest_path, manifest)
    _atomic_json(base / "latest.json", {"run": run, "report": f"{run}/cleaning_report.json",
                                       "manifest": f"executions/{execution_id}/run_manifest.json", "status": "passed"})
    evidence = root / "stages/stage_02/evidence/quality/cleaning_report.json"
    _atomic_json(evidence, report)
    return {"report": report, "output_dir": str(target), "report_path": str(target / "cleaning_report.json"),
            "manifest_path": str(manifest_path), "evidence_path": str(evidence)}
