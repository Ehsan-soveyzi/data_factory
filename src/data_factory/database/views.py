"""Section 7: ordinary views and a transactionally replaced Q5 result snapshot.

The analysis scope is deliberately fixed to delivered orders purchased in
[2016-01-01, 2019-01-01), exactly like the validated Q5 notebook analysis.
No Function/Procedure or automatic refresh scheduler is installed here.
"""

from datetime import date
import hashlib
import json
from pathlib import Path
from time import perf_counter

from sqlalchemy import text

from data_factory.database.query_benchmark import evidence_bytes, run_readonly_benchmark


PARAMETERS = {"start_date": date(2016, 1, 1), "end_date": date(2019, 1, 1),
              "order_status": "delivered"}
VIEW = "olist.v_payment_analysis"
SNAPSHOT = "olist.payment_analysis_snapshot"
ORDER_BY = ("analysis_level, payment_method_kind, payment_type NULLS LAST, "
            "payment_installments NULLS LAST, installment_kind")


def _rows(connection, relation):
    # Relation names come only from the constants in this module.
    return [dict(row) for row in connection.execute(text(
        f"SELECT * FROM {relation} ORDER BY {ORDER_BY}"
    )).mappings()]


def install_views(connection, project_root):
    """Run versioned DDL inside a caller-owned transaction; preserve snapshot data."""
    root = Path(project_root)
    for filename in ("sql/views/001_payment_analysis.sql", "sql/ddl/003_payment_snapshot.sql"):
        connection.exec_driver_sql((root / filename).read_text(encoding="utf-8"))


def refresh_snapshot(connection):
    """Replace only derived Q5 rows plus metadata in the caller's transaction.

    The exclusive snapshot lock serializes rebuilds and can briefly block readers.
    A failing statement must roll back the caller's transaction/savepoint.
    """
    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    connection.execute(text("SET LOCAL statement_timeout = '60s'"))
    connection.execute(text(f"LOCK TABLE {SNAPSHOT} IN ACCESS EXCLUSIVE MODE"))
    source = dict(connection.execute(text("""
        SELECT clock_timestamp() AS source_snapshot_at,
               pg_current_snapshot()::text AS source_snapshot_id,
               pg_get_viewdef('olist.v_payment_analysis'::regclass, true) AS definition
    """)).mappings().one())
    # DELETE preserves normal MVCC behavior for older readers; only ~58 result rows.
    connection.execute(text(f"DELETE FROM {SNAPSHOT}"))
    connection.execute(text(f"INSERT INTO {SNAPSHOT} SELECT * FROM {VIEW}"))
    count = connection.scalar(text(f"SELECT COUNT(*) FROM {SNAPSHOT}"))
    fingerprint = hashlib.sha256(source["definition"].encode("utf-8")).hexdigest()
    metadata = dict(connection.execute(text("""
        INSERT INTO olist.analysis_snapshot_metadata
            (snapshot_name, source_snapshot_at, source_snapshot_id, refreshed_at,
             row_count, view_definition_sha256)
        VALUES ('payment_analysis_snapshot', :at, :snapshot, clock_timestamp(), :count, :sha)
        ON CONFLICT (snapshot_name) DO UPDATE SET
            source_snapshot_at = EXCLUDED.source_snapshot_at,
            source_snapshot_id = EXCLUDED.source_snapshot_id,
            refreshed_at = EXCLUDED.refreshed_at, row_count = EXCLUDED.row_count,
            view_definition_sha256 = EXCLUDED.view_definition_sha256
        RETURNING *
    """), {"at": source["source_snapshot_at"], "snapshot": source["source_snapshot_id"],
           "count": count, "sha": fingerprint}).mappings().one())
    connection.exec_driver_sql(f"ANALYZE {SNAPSHOT}")
    return metadata


def prepare_payment_snapshot(engine, project_root):
    """Install views and build/validate one snapshot; include commit in build timing."""
    root = Path(project_root)
    with engine.begin() as conn:
        install_views(conn, root)
    started = perf_counter()
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        with conn.begin():
            metadata = refresh_snapshot(conn)
            refresh_and_analyze_ms = (perf_counter() - started) * 1000
            original = text((root / "sql/queries/05_payment_methods_installments.sql").read_text(encoding="utf-8"))
            expected = [dict(row) for row in conn.execute(original, PARAMETERS).mappings()]
            actual = _rows(conn, SNAPSHOT)
            if actual != expected:
                raise AssertionError("Snapshot differs from the original Q5 SQL; rebuild rolled back.")
    validation_and_commit_ms = (perf_counter() - started) * 1000
    with engine.connect() as conn:
        storage = [dict(row) for row in conn.execute(text("""
            SELECT c.relname AS object_name, c.relkind AS object_kind,
                   pg_relation_size(c.oid) AS relation_bytes,
                   pg_total_relation_size(c.oid) AS total_bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'olist' AND c.relname IN
                ('v_orders', 'v_payment_analysis', 'payment_analysis_snapshot', 'analysis_snapshot_metadata')
            ORDER BY c.relname
        """)).mappings()]
    report = {"metadata": metadata, "refresh_and_analyze_ms": refresh_and_analyze_ms,
              "validated_refresh_transaction_ms": validation_and_commit_ms,
              "validation_included_in_transaction_total": True,
              "all_rows_match_original_q5": True, "storage": storage,
              "parameters": PARAMETERS, "automatic_refresh": False}
    target = root / "stages/stage_02/evidence/quality/payment_snapshot_build.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(evidence_bytes(report))
    return report


def compare_payment_view_snapshot(engine, project_root):
    """One bounded comparison: one warmup and three full-fetch timings per path."""
    root = Path(project_root)
    reference = json.loads((root / "stages/stage_02/evidence/quality/q5_payment_methods_installments.json").read_text(encoding="utf-8"))
    def verify(conn, params, rows):
        if json.loads(evidence_bytes(rows)) != reference:
            raise AssertionError("View/snapshot output differs from the validated original Q5 result.")
        return {"passed": True, "all_rows_and_columns_match_original_q5": True}

    base = root / "stages/stage_02/evidence"
    reports = {}
    for label, filename in (("live_view", "07_payment_live_view.sql"),
                            ("snapshot", "07_payment_snapshot.sql")):
        rows, report, plan = run_readonly_benchmark(
            engine, root / "sql/queries" / filename, {},
            relations=("orders", "order_items", "order_payments", "payment_analysis_snapshot"),
            verify=verify, warmups=1, repetitions=3,
        )
        for relative, value in {f"benchmarks/views/{label}.json": report,
                                f"plans/views/{label}.json": plan,
                                f"quality/views/{label}_results.json": rows}.items():
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(evidence_bytes(value))
        reports[label] = report
    with engine.connect() as conn:
        freshness = dict(conn.execute(text("""
            SELECT *, clock_timestamp() AS checked_at,
                   EXTRACT(EPOCH FROM (clock_timestamp() - source_snapshot_at)) AS age_seconds
            FROM olist.analysis_snapshot_metadata
            WHERE snapshot_name = 'payment_analysis_snapshot'
        """)).mappings().one())
    live, cached = (reports[k]["benchmark"] for k in ("live_view", "snapshot"))
    comparison = {"timings": [{"path": name, **report["benchmark"]} for name, report in reports.items()],
                  "median_speedup": live["median_ms"] / cached["median_ms"],
                  "same_results": reports["live_view"]["results_sha256"] == reports["snapshot"]["results_sha256"],
                  "freshness": freshness, "scope": PARAMETERS,
                  "runs_per_path": 3, "automatic_refresh": False,
                  "freshness_limit": "Age describes build time only; equality was checked at measurement, not continuously."}
    (base / "benchmarks/views/comparison.json").write_bytes(evidence_bytes(comparison))
    return comparison
