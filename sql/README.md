# Versioned SQL

This directory holds versioned stage 02 SQL. The initial DDL files have been
executed in the local `ds_learning` database. Analytical queries and dependent
views/routines remain pending.

| Folder | Intended contents |
|---|---|
| `ddl/` | Ordered schema, table, key and constraint definitions |
| `queries/` | Five analytical queries; optional sixth query |
| `indexes/` | Index creation/removal scripts tied to measured plan evidence |
| `views/` | Simple and multi-table views |
| `functions/` | Functions returning meaningful values for use in SELECT |
| `procedures/` | Atomic, idempotent monthly snapshot refresh procedures |
| `benchmarks/` | Controlled SQL for insert-cost and performance comparisons |

The initial execution order is `ddl/001_olist.sql`, then `ddl/002_nyc311.sql`.
Notebook section **3.1**, after source/schema inspection and before loading, calls
`data_factory.database.ddl.run_initial_ddl(engine, PROJECT_ROOT / "sql/ddl")` using
the shared SQLAlchemy Engine created in section 1.1. Both files run inside one
`engine.begin()` transaction; a failure rolls back changes from both files.
Whole files are passed to `exec_driver_sql` without parameters or semicolon
splitting. No data is inserted by this setup.

The runner checks target names first. A complete existing set is inspected and
reported as `already_present` with no DDL rerun. Partial/conflicting objects stop
execution before either file runs. Existing table definitions are not compared
to revised SQL or migrated; this is initial setup, not a migration tool.
Existing tables are never dropped or rebuilt by the application runner.

`inspect_initial_schema(engine)` reads actual columns, PK/FK/UNIQUE/CHECK
constraints and row counts through a new Connection after commit. Detailed
results and raw SQL file hashes are recorded in
[ddl_execution.json](../stages/stage_02/evidence/quality/ddl_execution.json).
Initial results: 9 Olist tables, 3 NYC tables, 12 primary keys, 9 foreign keys,
75 checks, 1 unique constraint and zero data rows.

Keep dependencies explicit. Load data before applying dependent views and
routines. Experimental indexes and benchmarks must not run automatically with
baseline setup.

Keep definitions in `.sql` files. Execute notebook database interactions through
SQLAlchemy and pass values as bound parameters. Never put credentials in SQL.
