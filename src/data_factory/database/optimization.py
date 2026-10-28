"""One fixed index-only attempt on unchanged Q1/Q3/Q4/Q5; no tuning search."""

from datetime import date
import hashlib
import json
from pathlib import Path
from sqlalchemy import text
from data_factory.database.query_benchmark import evidence_bytes, run_readonly_benchmark

QUERY_FILES = {
    1: ("01_monthly_category_sales.sql", "q1_monthly_category_sales.json"),
    3: ("03_delivery_review_satisfaction.sql", "q3_delivery_review_satisfaction.json"),
    4: ("04_top_seller_performance.sql", "q4_top_seller_performance.json"),
    5: ("05_payment_methods_installments.sql", "q5_payment_methods_installments.json"),
}
INDEX_NAMES = ("idx_qbudget_reviews_latest",)

def index_inventory(connection):
    return [dict(row) for row in connection.execute(text("""
        SELECT ic.relname AS index_name, tc.relname AS table_name,
               pg_get_indexdef(ic.oid) AS definition, pg_relation_size(ic.oid) AS index_bytes,
               pg_relation_size(tc.oid) AS table_bytes, i.indisvalid AS is_valid
        FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid
        JOIN pg_class tc ON tc.oid=i.indrelid JOIN pg_namespace ns ON ns.oid=tc.relnamespace
        WHERE ns.nspname='olist' AND ic.relname = ANY(:names)
        ORDER BY ic.relname
    """), {"names": list(INDEX_NAMES)}).mappings()]

def _canonical(value):
    lines = [line for line in value.splitlines() if not line.lstrip().startswith("--")]
    return "".join(" ".join(lines).lower().replace(" using btree", "").rstrip("; ").split())

def apply_index_structure(engine, project_root):
    """Apply the single versioned index file atomically, or verify an exact rerun."""
    source = (Path(project_root) / "sql/indexes/001_query_budget_indexes.sql").read_text(encoding="utf-8")
    statements = [part.strip() for part in source.split(";") if part.strip()]
    expected = dict(zip(INDEX_NAMES, statements))
    if len(statements) != len(INDEX_NAMES):
        raise ValueError("Unexpected fixed index script.")
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout='5s'"))
        existing = index_inventory(conn)
        if existing:
            if {r["index_name"] for r in existing} != set(INDEX_NAMES):
                raise ValueError("The fixed index set is incomplete; no change applied.")
            if any(not r["is_valid"] or _canonical(r["definition"]) != _canonical(expected[r["index_name"]]) for r in existing):
                raise ValueError("Existing index definitions differ from the versioned structure.")
            status = "already_present"
        else:
            conn.exec_driver_sql(source)
            status = "created"
        inventory = index_inventory(conn)
    return {"status": status, "indexes": inventory,
            "total_index_bytes": sum(row["index_bytes"] for row in inventory)}

def _plan_summary(node):
    result = {"new_indexes_used": [], "disk_sorts": [], "cte_scans": [], "sequential_scans": []}
    def visit(current):
        if current.get("Index Name") in INDEX_NAMES:
            result["new_indexes_used"].append(current["Index Name"])
        if current.get("Sort Space Type") == "Disk":
            result["disk_sorts"].append({"rows": current["Actual Rows"], "space_kb": current["Sort Space Used"]})
        if current["Node Type"] == "CTE Scan":
            result["cte_scans"].append(current.get("CTE Name"))
        if current["Node Type"] == "Seq Scan":
            result["sequential_scans"].append(current.get("Relation Name"))
        for child in current.get("Plans", []): visit(child)
    visit(node)
    for field in ["new_indexes_used", "cte_scans", "sequential_scans"]:
        result[field] = sorted(set(result[field]))
    return result

def measure_indexing_query(engine, project_root, number, phase):
    if number not in QUERY_FILES or phase not in ("before", "after"):
        raise ValueError("Only Q1/Q3/Q4/Q5 and before/after phases are supported.")
    root = Path(project_root)
    filename, reference = QUERY_FILES[number]
    params = {"start_date": date(2016,1,1), "end_date": date(2019,1,1), "order_status": "delivered"}
    if number == 4: params["top_n"] = 20
    expected = json.loads((root / "stages/stage_02/evidence/quality" / reference).read_text(encoding="utf-8"))
    def verify(connection, values, rows):
        actual = json.loads(evidence_bytes(rows))
        key = lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False)
        if sorted(actual, key=key) != sorted(expected, key=key):
            raise AssertionError("Index-only execution changed the validated query result.")
        return {"passed": True, "all_columns_and_rows_match_validated_result": True}
    rows, report, plan = run_readonly_benchmark(
        engine, root / "sql/queries" / filename, params,
        relations=("orders", "order_items", "products", "sellers", "order_reviews", "order_payments"),
        verify=verify, warmups=2, repetitions=7,
    )
    report.update(query_number=number, phase=phase, query_sql_unchanged=True,
                  optimizer_settings_unchanged=True, plan_summary=_plan_summary(plan[0]["Plan"]))
    base = root / "stages/stage_02/evidence"
    for relative, value in {f"benchmarks/indexing/{phase}/q{number}.json": report,
                            f"plans/indexing/{phase}/q{number}.json": plan}.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(evidence_bytes(value))
    return report

def load_or_measure_before(engine, project_root, number):
    root = Path(project_root)
    with engine.connect() as conn: installed = bool(index_inventory(conn))
    if not installed: return measure_indexing_query(engine, root, number, "before")
    path = root / f"stages/stage_02/evidence/benchmarks/indexing/before/q{number}.json"
    if not path.is_file(): raise ValueError("Indexes already exist without a saved pre-index measurement.")
    report = json.loads(path.read_text(encoding="utf-8"))
    if hashlib.sha256((root / "sql/queries" / QUERY_FILES[number][0]).read_bytes()).hexdigest() != report["query_sha256"]:
        raise ValueError("Query changed since the saved pre-index measurement.")
    return report
