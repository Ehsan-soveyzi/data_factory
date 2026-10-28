"""Apply the two initial PostgreSQL scripts together, without replacing objects."""

from collections import Counter
import hashlib
from pathlib import Path

from sqlalchemy import MetaData, Table, func, inspect, select, text
from sqlalchemy.engine import Engine

from data_factory.database.connection import DatabaseSettings
from data_factory.exceptions import ConfigurationError


DDL_FILES = ("001_olist.sql", "002_nyc311.sql")
EXPECTED_TABLES = {
    "olist": (
        "product_categories", "customers", "sellers", "products", "geolocation",
        "orders", "order_items", "order_payments", "order_reviews",
    ),
    "nyc311": ("population_areas", "population_zip_aliases", "cases"),
}
DEFAULT_DDL_DIR = Path(__file__).resolve().parents[3] / "sql/ddl"


def _scripts(ddl_dir):
    result = []
    for filename in DDL_FILES:
        try:
            raw = (ddl_dir / filename).read_bytes()
            sql = raw.decode("utf-8-sig")
        except (OSError, UnicodeError):
            raise ConfigurationError(f"Cannot read the DDL file {filename}.") from None
        if not sql.strip():
            raise ConfigurationError(f"The DDL file {filename} is empty.")
        result.append((filename, sql, hashlib.sha256(raw).hexdigest()))
    return result


def _require_postgresql(engine):
    if engine.dialect.name != "postgresql" or engine.dialect.driver != "psycopg":
        raise ConfigurationError("Initial DDL requires the PostgreSQL psycopg engine.")


def _relations(connection):
    return {
        (row.schema_name, row.object_name): row.kind
        for row in connection.execute(text("""
            SELECT n.nspname AS schema_name, c.relname AS object_name, c.relkind AS kind
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('olist', 'nyc311')
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        """))
    }


def _inventory(connection):
    """Read actual columns, PK/FK/UNIQUE/CHECK constraints and exact row counts."""
    inspector = inspect(connection)
    identity = dict(connection.execute(text("""
        SELECT current_database() AS database_name, current_user AS database_user,
               current_setting('server_version') AS server_version
    """)).mappings().one())
    tables, columns, constraints = [], [], []
    metadata = MetaData()
    for schema, names in EXPECTED_TABLES.items():
        for name in names:
            reflected = inspector.get_columns(name, schema=schema)
            pk = inspector.get_pk_constraint(name, schema=schema)
            if not pk.get("constrained_columns"):
                raise ConfigurationError(f"Expected primary key is missing from {schema}.{name}.")
            foreign_keys = inspector.get_foreign_keys(name, schema=schema)
            unique_keys = inspector.get_unique_constraints(name, schema=schema)
            checks = inspector.get_check_constraints(name, schema=schema)
            row_count = connection.scalar(select(func.count()).select_from(
                Table(name, metadata, schema=schema)
            ))
            tables.append({
                "schema": schema, "table": name, "column_count": len(reflected),
                "primary_key": pk["constrained_columns"],
                "foreign_key_count": len(foreign_keys), "unique_count": len(unique_keys),
                "check_count": len(checks), "row_count": row_count,
            })
            for column in reflected:
                columns.append({
                    "schema": schema, "table": name, "column": column["name"],
                    "type": str(column["type"]), "nullable": column["nullable"],
                    "default": column.get("default"), "identity": column.get("identity"),
                })
            constraints.append({
                "schema": schema, "table": name, "type": "PRIMARY KEY",
                "name": pk["name"], "columns": pk["constrained_columns"],
            })
            for foreign_key in foreign_keys:
                constraints.append({
                    "schema": schema, "table": name, "type": "FOREIGN KEY",
                    "name": foreign_key["name"], "columns": foreign_key["constrained_columns"],
                    "referred_schema": foreign_key.get("referred_schema") or schema,
                    "referred_table": foreign_key["referred_table"],
                    "referred_columns": foreign_key["referred_columns"],
                })
            for unique in unique_keys:
                constraints.append({
                    "schema": schema, "table": name, "type": "UNIQUE",
                    "name": unique["name"], "columns": unique["column_names"],
                })
            for check in checks:
                constraints.append({
                    "schema": schema, "table": name, "type": "CHECK",
                    "name": check["name"], "expression": check["sqltext"],
                })
    return {
        "identity": identity, "schemas": list(EXPECTED_TABLES),
        "table_count": len(tables), "tables": tables, "columns": columns,
        "constraint_counts": dict(Counter(row["type"] for row in constraints)),
        "constraints": constraints,
    }


def inspect_initial_schema(engine: Engine) -> dict:
    """Read committed schema metadata; no DDL or data changes."""
    _require_postgresql(engine)
    with engine.connect() as connection:
        return _inventory(connection)


def run_initial_ddl(engine: Engine, ddl_dir: str | Path | None = None) -> dict:
    """Run both trusted baseline scripts in one engine.begin() transaction.

    A complete existing set of tables is inspected and left untouched. This
    does not compare existing definitions to revised SQL or perform migrations.
    Partial/conflicting objects cause an error before any script is executed.
    """
    scripts = _scripts(DEFAULT_DDL_DIR if ddl_dir is None else Path(ddl_dir))
    _require_postgresql(engine)
    expected = {(schema, name) for schema, names in EXPECTED_TABLES.items() for name in names}
    with engine.begin() as connection:
        relations = _relations(connection)
        existing = expected.intersection(relations)
        if existing and (existing != expected or any(relations[key] not in ("r", "p") for key in existing)):
            names = ", ".join(f"{schema}.{name}" for schema, name in sorted(existing))
            raise ConfigurationError(
                "Partial or conflicting initial schema; no DDL was executed. "
                f"Review existing objects: {names}"
            )
        applied = not existing
        if applied:
            for _, sql, _ in scripts:
                # Whole trusted file, no bindings or semicolon splitting. This
                # uses psycopg's parameter-free multi-statement execution.
                connection.exec_driver_sql(sql, execution_options={"no_parameters": True}).close()
        report = _inventory(connection)
        report.update({
            "status": "created" if applied else "already_present",
            "target": DatabaseSettings(url=engine.url).safe_url,
            "transaction_committed": True,
            "ddl_executed": applied, "data_loaded": False,
            "existing_definitions_compared_to_sql": False,
            "files": [{
                "file": filename, "sha256": digest,
                "executed": applied,
            } for filename, _, digest in scripts],
        })
    # Returning only after the context manager commits prevents a success
    # report being returned for a failed commit.
    return report
