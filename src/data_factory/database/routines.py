"""Section 8: PostgreSQL numeric Function and monthly snapshot Procedure."""

from datetime import date
import hashlib
from pathlib import Path
from time import perf_counter

from sqlalchemy import text

from data_factory.database.delivery_analysis import verify_delivery_results
from data_factory.database.query_benchmark import evidence_bytes


ROUTINE_FILES = (
    "sql/functions/001_safe_percentage.sql",
    "sql/ddl/004_monthly_delivery_snapshot.sql",
    "sql/procedures/001_refresh_monthly_delivery.sql",
)
CALL_MONTH = text("CALL olist.refresh_monthly_delivery_snapshot(CAST(:month AS date))")


def install_routines(connection, project_root):
    """Install the versioned derived tables and routines in the caller's transaction."""
    for relative in ROUTINE_FILES:
        connection.exec_driver_sql((Path(project_root) / relative).read_text(encoding="utf-8"))


def read_month(connection, month):
    return [dict(r) for r in connection.execute(text("""
        SELECT * FROM olist.monthly_delivery_snapshot
        WHERE purchase_month = :month ORDER BY purchase_month, customer_state
    """), {"month": month}).mappings()]


def run_monthly_routines(engine, project_root, month=date(2017, 1, 1)):
    """Run one real month twice, validate all fields, and preserve every other month.

    This is a correctness/idempotence demonstration, not a tuning benchmark.
    The caller owns engine disposal; installation and each validated CALL commit
    atomically. SQLAlchemy binds values; only trusted versioned SQL is executed.
    """
    if type(month) is not date:
        raise ValueError("month must be a Python date")
    month = month.replace(day=1)
    end = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    root = Path(project_root)
    with engine.begin() as conn:
        install_routines(conn, root)
    original = text((root / "sql/queries/02_monthly_state_delivery.sql").read_text(encoding="utf-8"))
    params = {"start_date": month, "end_date": end, "order_status": "delivered"}
    measurements = []
    previous = None
    for run in (1, 2):
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text("SET LOCAL statement_timeout = '60s'"))
                other_before = conn.execute(text("""
                    SELECT * FROM olist.monthly_delivery_snapshot WHERE purchase_month <> :month
                    ORDER BY purchase_month, customer_state
                """), {"month": month}).all()
                started = perf_counter()
                conn.execute(CALL_MONTH, {"month": month})
                elapsed = (perf_counter() - started) * 1000
                rows = read_month(conn, month)
                expected = [dict(r) for r in conn.execute(original, params).mappings()]
                if rows != expected:
                    raise AssertionError("Monthly snapshot differs from original Q2; CALL rolled back.")
                checks = verify_delivery_results(conn, params, rows)
                other_after = conn.execute(text("""
                    SELECT * FROM olist.monthly_delivery_snapshot WHERE purchase_month <> :month
                    ORDER BY purchase_month, customer_state
                """), {"month": month}).all()
                if other_before != other_after:
                    raise AssertionError("Refresh changed another month; CALL rolled back.")
                if previous is not None and rows != previous:
                    raise AssertionError("Repeated refresh produced different rows; CALL rolled back.")
                metadata = dict(conn.execute(text("""
                    SELECT * FROM olist.monthly_delivery_snapshot_runs WHERE purchase_month = :month
                """), {"month": month}).mappings().one())
                overall_pct = conn.scalar(text("""
                    SELECT olist.safe_percentage(SUM(late_order_count), SUM(eligible_order_count))
                    FROM olist.monthly_delivery_snapshot WHERE purchase_month = :month
                """), {"month": month})
        previous = rows
        measurements.append({"run": run, "call_ms_excluding_commit_and_validation": elapsed,
                             "state_rows": len(rows), "matches_original_q2": True,
                             "independent_python_check_passed": checks["passed"],
                             "other_months_preserved": True})
    report = {"month": month, "end_exclusive": end, "order_status": "delivered",
              "measurements": measurements, "same_rows_after_second_call": True,
              "results_sha256": hashlib.sha256(evidence_bytes(rows)).hexdigest(),
              "monthly_weighted_late_pct": overall_pct, "independent_control": checks,
              "metadata": metadata,
              "source_files": [{"file": r, "sha256": hashlib.sha256((root / r).read_bytes()).hexdigest()}
                               for r in ROUTINE_FILES],
              "transaction_isolation": "REPEATABLE READ", "automatic_refresh": False}
    directory = root / "stages/stage_02/evidence/quality/routines"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "monthly_delivery_results.json").write_bytes(evidence_bytes(rows))
    (directory / "execution.json").write_bytes(evidence_bytes(report))
    return rows, report
