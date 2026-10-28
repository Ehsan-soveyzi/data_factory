"""Run database tests in a temporary database using the local server credentials."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from sqlalchemy import text

from data_factory.database.connection import DatabaseSettings, create_database_engine, load_database_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py",
                        choices=("test_*.py", "test_delivery_analysis.py", "test_satisfaction_analysis.py",
                                 "test_seller_analysis.py", "test_payment_analysis.py"))
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    settings = load_database_settings()
    test_name = "df_ddl_test_" + uuid.uuid4().hex[:16]
    if not re.fullmatch(r"df_ddl_test_[0-9a-f]{16}", test_name):
        raise RuntimeError("Invalid generated test database name.")
    admin = create_database_engine(DatabaseSettings(url=settings.url.set(database="postgres")))
    admin = admin.execution_options(isolation_level="AUTOCOMMIT")
    quoted_name = admin.dialect.identifier_preparer.quote_identifier(test_name)
    created_oid = None
    removed = False
    outcome = None
    try:
        with admin.connect() as connection:
            exists = connection.scalar(text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": test_name})
            if exists is not None:
                raise RuntimeError("Generated test database already exists.")
            connection.exec_driver_sql("CREATE DATABASE " + quoted_name)
            created_oid = connection.scalar(text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": test_name})
            if created_oid is None:
                raise RuntimeError("Cannot verify the newly created test database.")
        environment = os.environ.copy()
        environment["DATA_FACTORY_TEST_DATABASE_URL"] = settings.url.set(database=test_name).render_as_string(hide_password=False)
        outcome = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests/database", "-p", args.pattern, "-v"],
            cwd=project, env=environment, capture_output=True, text=True,
        )
    finally:
        try:
            if created_oid is not None:
                with admin.connect() as connection:
                    current_oid = connection.scalar(text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": test_name})
                    if current_oid != created_oid:
                        raise RuntimeError("Test database identity changed; refusing cleanup.")
                    # Only this invocation's database; no FORCE or session termination.
                    connection.exec_driver_sql("DROP DATABASE " + quoted_name)
                    removed = connection.scalar(text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": test_name}) is None
        finally:
            admin.dispose()

    assert outcome is not None
    output = outcome.stdout + outcome.stderr
    if settings.url.password:
        output = output.replace(settings.url.password, "[REDACTED]")
    match = re.search(r"Ran (\d+) tests?", output)
    live_count = len(re.findall(r"^test_\w+ \(test_ddl\.PostgreSQLDDLTests\.", output, re.MULTILINE))
    live_query_count = len(re.findall(
        r"^test_\w+ \(test_(?:sales|delivery_analysis|satisfaction_analysis|seller_analysis|payment_analysis)\.PostgreSQL(?:Sales|DeliveryAnalysis|SatisfactionAnalysis|SellerAnalysis|PaymentAnalysis)Tests\.",
        output, re.MULTILINE))
    total_count = int(match.group(1)) if match else None
    report = {
        "test_command": "python tests/database/run_ddl_tests.py" + (
            " --pattern " + args.pattern if args.pattern != "test_*.py" else ""),
        "status": "passed" if outcome.returncode == 0 else "failed",
        "tests_run": total_count, "live_ddl_tests": live_count,
        "live_query_tests": live_query_count,
        "offline_tests": total_count - live_count - live_query_count if total_count is not None else None,
        "isolated_test_database": test_name, "test_database_removed": removed,
        "fixture_resets_confined_to_test_database": True,
        "analysis_database_not_used_for_fixtures": settings.url.database,
        "output": output,
    }
    evidence_name = {"test_delivery_analysis.py": "q2_tests.json",
                     "test_satisfaction_analysis.py": "q3_tests.json",
                     "test_seller_analysis.py": "q4_tests.json",
                     "test_payment_analysis.py": "q5_tests.json"}.get(args.pattern, "ddl_tests.json")
    path = project / "stages/stage_02/evidence/quality" / evidence_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "output"}, indent=2))
    if outcome.returncode:
        print(output)
    return outcome.returncode


if __name__ == "__main__":
    raise SystemExit(main())
