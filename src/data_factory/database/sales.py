"""Read-only Q1 execution and repeatable baseline measurement on PostgreSQL."""

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from sqlalchemy import text


QUERY_FILE = "01_monthly_category_sales.sql"
PLAN_FILE = "01_monthly_category_sales_plan.sql"
EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)\n"


def json_value(value):
    """Preserve exact NUMERIC values as strings in portable evidence."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported evidence type: {type(value).__name__}")


def run_sales_baseline(engine, project_root, *, start_date=date(2016, 1, 1),
                       end_date=date(2019, 1, 1), repetitions=7,
                       warmups=2, budget_ms=200):
    """Run Q1 in one repeatable-read, read-only snapshot; never load data.

    Timings include execute + full result fetch, excluding connection setup,
    displays, file writes and EXPLAIN instrumentation. This is a warm-cache
    baseline, not a concurrent-dashboard or cold-start guarantee.
    """
    if type(start_date) is not date or type(end_date) is not date or start_date >= end_date:
        raise ValueError("Use date objects with start_date < end_date.")
    if repetitions < 3 or warmups < 1 or budget_ms <= 0:
        raise ValueError("Use at least three repeats, one warmup and a positive budget.")
    root = Path(project_root)
    query_bytes = (root / "sql/queries" / QUERY_FILE).read_bytes()
    query_sql = query_bytes.decode("utf-8").replace("\r\n", "\n")
    plan_sql = (root / "sql/queries" / PLAN_FILE).read_text(encoding="utf-8")
    if plan_sql != EXPLAIN_PREFIX + query_sql:
        raise ValueError("The static EXPLAIN file must contain the exact Q1 SQL.")
    query = text(query_sql)
    params = {"start_date": start_date, "end_date": end_date, "order_status": "delivered"}
    timings = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = '60s'"))
            environment = dict(conn.execute(text("""
                SELECT current_database() AS database_name,
                       current_setting('server_version') AS server_version,
                       current_setting('work_mem') AS work_mem,
                       current_setting('shared_buffers') AS shared_buffers,
                       current_setting('max_parallel_workers_per_gather') AS max_parallel_workers_per_gather,
                       current_setting('jit') AS jit,
                       current_setting('TimeZone') AS session_timezone
            """)).mappings().one())
            indexes = [dict(row) for row in conn.execute(text("""
                SELECT tablename, indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'olist'
                  AND tablename IN ('orders', 'order_items', 'products', 'product_categories')
                ORDER BY tablename, indexname
            """)).mappings()]
            statuses = [dict(row) for row in conn.execute(text("""
                SELECT order_status, COUNT(*) AS order_count
                FROM olist.orders
                WHERE order_purchase_timestamp >= :start_date
                  AND order_purchase_timestamp < :end_date
                GROUP BY order_status ORDER BY order_status
            """), params).mappings()]
            for _ in range(warmups):
                conn.execute(query, params).fetchall()
            previous = None
            for _ in range(repetitions):
                started = perf_counter()
                rows = [dict(row) for row in conn.execute(query, params).mappings().all()]
                timings.append((perf_counter() - started) * 1000)
                if previous is not None and rows != previous:
                    raise AssertionError("Q1 changed within the repeatable-read snapshot.")
                previous = rows
            plan = conn.execute(text(plan_sql), params).scalar_one()
            control = dict(conn.execute(text("""
                SELECT COUNT(*) AS eligible_item_count,
                       COUNT(DISTINCT o.order_id) AS eligible_order_count,
                       SUM(oi.price) AS gross_product_sales_brl,
                       MIN(o.order_purchase_timestamp) AS first_purchase,
                       MAX(o.order_purchase_timestamp) AS last_purchase,
                       COUNT(*) FILTER (WHERE p.product_category_name IS NULL) AS uncategorized_item_count
                FROM olist.orders AS o
                JOIN olist.order_items AS oi ON oi.order_id = o.order_id
                JOIN olist.products AS p ON p.product_id = oi.product_id
                WHERE o.order_status = :order_status
                  AND o.order_purchase_timestamp >= :start_date
                  AND o.order_purchase_timestamp < :end_date
            """), params).mappings().one())
    result_total = sum((row["gross_product_sales_brl"] for row in rows), Decimal("0"))
    control_total = control["gross_product_sales_brl"] or Decimal("0")
    if result_total != control_total:
        raise AssertionError("Monthly-category sales do not reconcile to eligible item prices.")
    row_bytes = (json.dumps(rows, ensure_ascii=False, indent=2, default=json_value,
                           allow_nan=False) + "\n").encode("utf-8")
    report = {
        "question": "Q1 monthly category merchandise sales",
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment, "parameters": params,
        "semantics": {
            "month": "purchase month, source timestamp without timezone conversion",
            "sales": "SUM(item price) in nominal BRL, delivered orders only, excluding freight",
            "average": "category sales / unique orders containing that category, rounded to 2 decimals",
            "missing_category": "NULL key retained; label Uncategorized",
            "missing_translation": "original Portuguese category retained",
            "absent_month_category": "no invented zero rows",
            "order_count_additive_across_categories": False,
        },
        "query_sha256": hashlib.sha256(query_bytes).hexdigest(),
        "results_sha256": hashlib.sha256(row_bytes).hexdigest(),
        "result_row_count": len(rows), "coverage_by_status": statuses,
        "independent_control": control,
        "reconciliation": {"passed": True, "result_sales_brl": result_total},
        "indexes_at_measurement": indexes,
        "benchmark": {
            "method": "perf_counter: SQLAlchemy execute and full fetch on one connection",
            "cache": "warm; no cache flush; no concurrency load",
            "warmups": warmups, "repetitions": repetitions,
            "elapsed_ms": timings, "min_ms": min(timings),
            "median_ms": median(timings), "max_ms": max(timings),
            "budget_ms": budget_ms,
            "all_measured_runs_within_budget": max(timings) < budget_ms,
            "explain_execution_ms": plan[0]["Execution Time"],
            "explain_planning_ms": plan[0]["Planning Time"],
        },
        "scope": "Q1 only; no new indexes, views, snapshots or other questions",
    }
    return rows, report, plan


def save_sales_evidence(project_root, rows, report, plan):
    """Save results, actual samples and instrumented plan outside timed work."""
    base = Path(project_root) / "stages/stage_02/evidence"
    files = {
        "quality/q1_monthly_category_sales.json": rows,
        "benchmarks/q1_baseline.json": report,
        "plans/q1_baseline.json": plan,
    }
    for relative, value in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   default=json_value, allow_nan=False) + "\n",
                        encoding="utf-8", newline="\n")
