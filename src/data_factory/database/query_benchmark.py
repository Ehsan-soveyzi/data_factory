"""Read-only analytical query measurement with a single SQL source file."""

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from sqlalchemy import text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import ClauseElement, Executable


class Explain(Executable, ClauseElement):
    """Wrap a trusted SQLAlchemy statement; preserve its bound parameters."""

    inherit_cache = False

    def __init__(self, statement):
        self.statement = statement


@compiles(Explain, "postgresql")
def compile_explain(element, compiler, **kwargs):
    # SQLAlchemy compiles the SQL and its binds; no parameter values enter SQL text.
    return "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)\n" + compiler.process(element.statement, **kwargs)


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported evidence type: {type(value).__name__}")


def evidence_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, default=json_value,
                       allow_nan=False) + "\n").encode("utf-8")


def run_readonly_benchmark(engine, sql_path, params, *, relations, verify,
                           warmups=2, repetitions=7, budget_ms=200):
    """Measure execute/full-fetch, then EXPLAIN and verify in the same snapshot.

    `verify(connection, params, rows)` runs outside the timed section. The caller
    owns engine disposal; every connection here is returned by its context.
    """
    if type(warmups) is not int or warmups < 1 or type(repetitions) is not int or repetitions < 3:
        raise ValueError("Use at least one warmup and three measurement repeats.")
    if budget_ms <= 0:
        raise ValueError("Use a positive query budget.")
    query_bytes = Path(sql_path).read_bytes()
    query = text(query_bytes.decode("utf-8"))
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
                WHERE schemaname = 'olist' AND tablename = ANY(:relations)
                ORDER BY tablename, indexname
            """), {"relations": list(relations)}).mappings()]
            for _ in range(warmups):
                conn.execute(query, params).fetchall()
            previous = None
            for _ in range(repetitions):
                started = perf_counter()
                rows = [dict(row) for row in conn.execute(query, params).mappings().all()]
                timings.append((perf_counter() - started) * 1000)
                if previous is not None and rows != previous:
                    raise AssertionError("Query results changed within the fixed snapshot.")
                previous = rows
            plan = conn.execute(Explain(query), params).scalar_one()
            checks = verify(conn, params, rows)
    report = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment, "parameters": params,
        "query_file": Path(sql_path).name,
        "query_sha256": hashlib.sha256(query_bytes).hexdigest(),
        "results_sha256": hashlib.sha256(evidence_bytes(rows)).hexdigest(),
        "result_row_count": len(rows), "indexes_at_measurement": indexes,
        "verification": checks,
        "benchmark": {
            "method": "perf_counter: SQLAlchemy execute and full result fetch on one connection",
            "cache": "warm; no cache flush; no concurrency load",
            "warmups": warmups, "repetitions": repetitions,
            "elapsed_ms": timings, "min_ms": min(timings),
            "median_ms": median(timings), "max_ms": max(timings),
            "budget_ms": budget_ms,
            "all_measured_runs_within_budget": max(timings) < budget_ms,
            "explain_execution_ms": plan[0]["Execution Time"],
            "explain_planning_ms": plan[0]["Planning Time"],
        },
    }
    return rows, report, plan


def save_query_evidence(project_root, stem, rows, report, plan):
    """Keep exact result bytes and their reported SHA-256 consistent."""
    base = Path(project_root) / "stages/stage_02/evidence"
    files = {f"quality/{stem}.json": rows,
             f"benchmarks/{stem}_baseline.json": report,
             f"plans/{stem}_baseline.json": plan}
    for name, value in files.items():
        target = base / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(evidence_bytes(value))
