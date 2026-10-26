# Versioned SQL

This directory reserves locations for stage 02 SQL. No database objects or
analytical queries have been implemented yet.

| Folder | Intended contents |
|---|---|
| `ddl/` | Ordered schema, table, key and constraint definitions |
| `queries/` | Five analytical queries; optional sixth query |
| `indexes/` | Index creation/removal scripts tied to measured plan evidence |
| `views/` | Simple and multi-table views |
| `functions/` | Functions returning meaningful values for use in SELECT |
| `procedures/` | Atomic, idempotent monthly snapshot refresh procedures |
| `benchmarks/` | Controlled SQL for insert-cost and performance comparisons |

Use ordered filenames where execution order matters, such as `001_schemas.sql`.
Keep dependencies explicit. A future bootstrap runner must create base objects
and load data before applying dependent views and routines. Experimental indexes
and benchmarks must not run automatically with baseline setup.

Keep definitions in `.sql` files. Execute notebook database interactions through
SQLAlchemy and pass values as bound parameters. Never put credentials in SQL.
