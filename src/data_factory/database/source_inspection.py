"""Read-only Olist schema checks called by the stage 02 notebook.

Functions return DataFrames; presentation stays in the notebook. Only
save_evidence writes a diagnostics JSON. Source files and DataFrames are not
cleaned, quarantined or loaded into PostgreSQL by these checks.
"""

from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def sha256_file(path):
    """Fingerprint a local input without modifying it."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def summarize_sources(frames):
    """Count source rows, exact repeats and separate null/blank values."""
    source_summary = pd.DataFrame([
        {"table": name, "rows": len(frame), "columns": len(frame.columns),
         "exact_duplicate_extra_rows": int(frame.duplicated().sum())}
        for name, frame in frames.items()
    ])
    missing_rows = []
    for name, frame in frames.items():
        for column in frame.columns:
            null_count = int(frame[column].isna().sum())
            blank_count = int(frame[column].notna().mul(
                frame[column].astype("string").fillna("").str.strip().eq("")
            ).sum())
            missing_rows.append({"table": name, "column": column, "null_rows": null_count,
                                 "blank_rows": blank_count, "effective_missing_rows": null_count + blank_count})
    missing_summary = pd.DataFrame(missing_rows)
    return source_summary, missing_summary


def check_candidate_keys(frames):
    """Check complete single and composite keys without discarding duplicates."""
    key_specs = [
        ("orders", ["order_id"]), ("customers", ["customer_id"]),
        ("customers", ["customer_unique_id"]),
        ("order_items", ["order_id", "order_item_id"]),
        ("order_payments", ["order_id", "payment_sequential"]),
        ("order_reviews", ["review_id"]), ("order_reviews", ["order_id"]),
        ("order_reviews", ["review_id", "order_id"]),
        ("products", ["product_id"]), ("sellers", ["seller_id"]),
        ("category_translation", ["product_category_name"]),
        ("geolocation", ["geolocation_zip_code_prefix"]),
    ]
    key_rows = []
    for name, columns in key_specs:
        values = frames[name][columns].astype("string").apply(lambda column: column.str.strip())
        missing = values.isna().any(axis=1) | values.fillna("").eq("").any(axis=1)
        duplicate_count = int(values.loc[~missing].duplicated().sum())
        key_rows.append({"table": name, "key": " + ".join(columns), "missing_rows": int(missing.sum()),
                         "duplicate_extra_rows": duplicate_count,
                         "valid_candidate": int(missing.sum()) == 0 and duplicate_count == 0})
    key_summary = pd.DataFrame(key_rows)
    return key_summary


def check_relationships(frames):
    """Separate missing child values from nonblank references without a parent."""
    product = frames["products"]
    product_category = frames["category_translation"]
    relationships = [
        ("orders", "customer_id", "customers", "customer_id"),
        ("order_items", "order_id", "orders", "order_id"),
        ("order_items", "product_id", "products", "product_id"),
        ("order_items", "seller_id", "sellers", "seller_id"),
        ("order_payments", "order_id", "orders", "order_id"),
        ("order_reviews", "order_id", "orders", "order_id"),
        ("products", "product_category_name", "category_translation", "product_category_name"),
    ]
    foreign_key_rows = []
    for child, column, parent, parent_column in relationships:
        values = frames[child][column].astype("string").str.strip()
        parent_values = frames[parent][parent_column].astype("string").str.strip().dropna()
        present = values.notna() & values.fillna("").ne("")
        unknown = present & ~values.isin(parent_values[parent_values.ne("")])
        foreign_key_rows.append({
            "child": child, "column": column, "parent": parent,
            "missing_child_rows": int((~present).sum()), "unmatched_rows": int(unknown.sum()),
            "unmatched_distinct": int(values[unknown].nunique()),
            "unmatched_values": sorted(values[unknown].dropna().unique().tolist()),
        })
    foreign_key_summary = pd.DataFrame(foreign_key_rows)

    category_values = pd.concat([product["product_category_name"], product_category["product_category_name"]])
    category_values = category_values.astype("string").str.strip().dropna()
    category_count = int(category_values[category_values.ne("")].nunique())
    return foreign_key_summary, category_count


def schema_table(schema_proposal):
    """Present the declared source-to-SQL column mapping."""
    schema_rows = []
    for name, definition in schema_proposal["tables"].items():
        for column in definition["columns"]:
            schema_rows.append({"table": name, "source": column["source"], "target": column["target"],
                                "sql_type": column["sql_type"], "nullable": column["nullable"]})
    schema_summary = pd.DataFrame(schema_rows)
    return schema_summary


def check_numeric(frames, schema_proposal):
    """Check finite values, bounds, integer storage and exact decimal precision."""
    numeric_rows = []
    for name, definition in schema_proposal["tables"].items():
        for column in definition["columns"]:
            if column["kind"] not in ("integer", "money", "real"):
                continue
            raw = frames[name][column["source"]].astype("string").str.strip()
            present = raw.notna() & raw.fillna("").ne("")
            values = pd.to_numeric(raw, errors="coerce")
            finite = values.notna() & np.isfinite(values.astype(float))
            numeric = values[finite]
            below_minimum = numeric.lt(column.get("minimum", -np.inf))
            if column.get("zero_policy") == "null_with_quality_flag":
                below_minimum &= numeric.ne(0)
            upper_bound = 32767 if column["sql_type"] == "SMALLINT" else 2147483647
            precision_errors = 0
            if column["kind"] == "money":
                for text, count in raw[present].value_counts().items():
                    try:
                        exact = Decimal(text)
                        if not exact.is_finite() or abs(exact) >= Decimal("10000000000") or exact != exact.quantize(Decimal("0.01")):
                            precision_errors += int(count)
                    except InvalidOperation:
                        precision_errors += int(count)
            numeric_rows.append({
                "table": name, "column": column["source"],
                "invalid_or_nonfinite_rows": int((present & ~finite).sum()),
                "min": float(numeric.min()) if len(numeric) else None,
                "max": float(numeric.max()) if len(numeric) else None,
                "negative_rows": int(numeric.lt(0).sum()), "zero_rows": int(numeric.eq(0).sum()),
                "fractional_integer_rows": int(numeric.mod(1).ne(0).sum()) if column["kind"] == "integer" else 0,
                "below_rule_min_rows": int(below_minimum.sum()),
                "above_rule_max_rows": int(numeric.gt(column.get("maximum", np.inf)).sum()),
                "integer_storage_overflow_rows": int((numeric.gt(upper_bound) | numeric.lt(-upper_bound-1)).sum()) if column["kind"] == "integer" else 0,
                "zero_to_null_rows": int(numeric.eq(0).sum()) if column.get("zero_policy") == "null_with_quality_flag" else 0,
                "money_precision_errors": precision_errors,
            })
    numeric_summary = pd.DataFrame(numeric_rows)
    return numeric_summary


def check_dates(frames, schema_proposal):
    """Validate timestamp text and measure chronology without rewriting dates."""
    orders = frames["orders"]
    order_payments = frames["order_payments"]
    date_rows = []
    parsed_dates = {}
    for name, definition in schema_proposal["tables"].items():
        for column in definition["columns"]:
            if column["kind"] not in ("date", "timestamp"):
                continue
            field = column["source"]
            raw = frames[name][field].astype("string").str.strip()
            present = raw.notna() & raw.fillna("").ne("")
            values = pd.to_datetime(raw, format="%Y-%m-%d %H:%M:%S", errors="coerce")
            parsed_dates[name, field] = values
            date_rows.append({
                "table": name, "column": field, "missing_rows": int((~present).sum()),
                "invalid_nonblank_rows": int((present & values.isna()).sum()),
                "min": values.min().isoformat() if values.notna().any() else None,
                "max": values.max().isoformat() if values.notna().any() else None,
                "non_midnight_rows": int((values.notna() & values.ne(values.dt.normalize())).sum()) if column["kind"] == "date" else None,
            })
    date_summary = pd.DataFrame(date_rows)
    time_pairs = [
        ("order_purchase_timestamp", "order_approved_at"),
        ("order_approved_at", "order_delivered_carrier_date"),
        ("order_delivered_carrier_date", "order_delivered_customer_date"),
        ("order_purchase_timestamp", "order_delivered_customer_date"),
    ]
    chronology_rows = []
    for start, end in time_pairs:
        before = parsed_dates["orders", start]
        after = parsed_dates["orders", end]
        available = before.notna() & after.notna()
        chronology_rows.append({"start": start, "end": end, "comparable_rows": int(available.sum()),
                                "negative_duration_rows": int((available & after.lt(before)).sum())})
    chronology_summary = pd.DataFrame(chronology_rows)
    special_counts = {
        "delivered_without_actual_date": int((orders["order_status"].eq("delivered") & parsed_dates["orders", "order_delivered_customer_date"].isna()).sum()),
        "orders_without_payment": int((~orders["order_id"].isin(order_payments["order_id"])).sum()),
    }
    return date_summary, chronology_summary, special_counts


def check_postal(frames):
    """Validate postal prefixes and measure geography coverage on temporary Series."""
    postal_rows = []
    normalized_postal = {}
    for name, column in [("customers", "customer_zip_code_prefix"),
                         ("sellers", "seller_zip_code_prefix"),
                         ("geolocation", "geolocation_zip_code_prefix")]:
        raw = frames[name][column].astype("string").str.strip()
        present = raw.notna() & raw.fillna("").ne("")
        valid = raw.str.fullmatch(r"[0-9]{1,5}").fillna(False)
        normalized_postal[name] = raw[valid].str.zfill(5)
        postal_rows.append({"table": name, "missing_rows": int((~present).sum()),
                           "invalid_nonblank_rows": int((present & ~valid).sum()),
                           "needs_zero_padding_rows": int(raw[valid].str.len().lt(5).sum())})
    postal_summary = pd.DataFrame(postal_rows)
    geo_prefixes = set(normalized_postal["geolocation"])
    postal_summary["missing_geolocation_rows"] = postal_summary["table"].map(
        lambda name: int((~normalized_postal[name].isin(geo_prefixes)).sum())
    )
    postal_area_count = len(set().union(*(set(values) for values in normalized_postal.values())))
    return postal_summary, postal_area_count


def inspect_olist(project_root, frames):
    """Run the checks against the DataFrames already loaded by the notebook."""
    root = Path(project_root).resolve()
    settings = json.loads((root / "config/database.example.json").read_text(encoding="utf-8"))
    contract_path = root / "config/olist_schema.json"
    contract_hash = sha256_file(contract_path)
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if set(frames) != set(contract["tables"]):
        raise ValueError("Provide exactly the nine DataFrames declared in olist_schema.json.")
    raw_paths = {}
    for name, frame in frames.items():
        definition = contract["tables"][name]
        expected = [column["source"] for column in definition["columns"]]
        if list(frame.columns) != expected:
            raise ValueError(f"Source columns changed for {name}: {list(frame.columns)}")
        raw_paths[name] = (Path(settings["paths"]["olist_raw_dir"]) / definition["file"]).as_posix()
    fingerprints = {name: sha256_file(root / relative) for name, relative in raw_paths.items()}
    sources, missing = summarize_sources(frames)
    foreign_keys, categories = check_relationships(frames)
    dates, chronology, special = check_dates(frames, contract)
    postal, postal_count = check_postal(frames)
    result = {
        "sources": sources, "missing": missing, "keys": check_candidate_keys(frames),
        "foreign_keys": foreign_keys, "schema": schema_table(contract),
        "numeric": check_numeric(frames, contract), "dates": dates, "chronology": chronology,
        "postal": postal, "special_counts": special,
        "proposed_category_rows": categories, "proposed_postal_area_rows": postal_count,
        "raw_paths": raw_paths, "input_hashes": fingerprints, "contract_sha256": contract_hash,
    }
    if sha256_file(contract_path) != contract_hash:
        raise ValueError("The schema proposal changed during inspection.")
    for name, relative in raw_paths.items():
        if sha256_file(root / relative) != fingerprints[name]:
            raise ValueError(f"Raw CSV changed during inspection: {relative}")
    return result


def save_evidence(project_root, inspection):
    """Verify frozen inputs again, then publish only observational diagnostics."""
    root = Path(project_root).resolve()
    for name, relative in inspection["raw_paths"].items():
        if sha256_file(root / relative) != inspection["input_hashes"][name]:
            raise ValueError(f"Raw CSV changed before publishing evidence: {relative}")
    if sha256_file(root / "config/olist_schema.json") != inspection["contract_sha256"]:
        raise ValueError("The schema proposal changed before publishing evidence.")
    evidence = {
        "kind": "module_schema_checks", "database_writes": 0, "quarantine_executed": False,
        "inputs": {name: {"file": relative, "sha256": inspection["input_hashes"][name]}
                   for name, relative in inspection["raw_paths"].items()},
        "implementation": {"file": "src/data_factory/database/source_inspection.py", "sha256": sha256_file(__file__)},
        "contract_sha256": inspection["contract_sha256"],
    }
    for name in ("sources", "missing", "keys", "foreign_keys", "numeric", "dates", "chronology", "postal"):
        evidence[name] = json.loads(inspection[name].to_json(orient="records"))
    for name in ("special_counts", "proposed_category_rows", "proposed_postal_area_rows"):
        evidence[name] = inspection[name]
    output = root / "stages/stage_02/evidence/quality/notebook_checks.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output
