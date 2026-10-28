# Versioned SQL

This directory holds versioned stage 02 SQL. The initial DDL files have been
executed in the local `ds_learning` database. Q1 merchandise sales is implemented
in `queries/01_monthly_category_sales.sql`; its static EXPLAIN companion has the
same query with an instrumentation prefix. Q2 is implemented in
`queries/02_monthly_state_delivery.sql` with a single SQL file and a compiled
SQLAlchemy EXPLAIN wrapper. Q3 uses `queries/03_delivery_review_satisfaction.sql`
with the same wrapper. Q4-Q5 and dependent views/routines remain pending.

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

## Q1: monthly category sales

Notebook section 4.1 runs Q1 with bound `start_date` (inclusive), `end_date`
(exclusive) and `order_status` (`delivered`). It sums item prices in BRL without
freight. Orders are first aggregated by category, so several items from one
category count as one order. The average is that category's sales per order
containing the category, rather than the entire multi-category basket value.
Missing categories remain a separate NULL-key group; missing translations fall
back to the original category. Category order counts must not be summed to obtain
global distinct orders.

`data_factory.database.sales.run_sales_baseline` reuses the notebook Engine in a
repeatable-read, read-only transaction. It performs two warmups and seven timed
execute/fetch runs, then separately collects EXPLAIN ANALYZE. It creates no
database objects and does not change persistent server configuration. Results,
exact money values, SQL hash, existing indexes and measurement conditions are
saved under `stages/stage_02/evidence/`. The baseline currently exceeds 200 ms;
query-budget acceptance remains pending optimization.

## Q2: monthly delivery timeliness by customer state

`queries/02_monthly_state_delivery.sql` counts delivered orders by purchase month
and customer state. Delivery is late only when its calendar date is strictly
after the estimated DATE; same-day evening delivery is on time. Eligible orders
require the source eligibility flag, both dates and actual delivery at/after
purchase. Unknown/invalid dates remain in coverage counts but not in the rate
denominator. A zero denominator returns NULL. Aggregate rates must be recomputed
from summed late/eligible counts, not averaged from group percentages.

`data_factory.database.query_benchmark.Explain` compiles EXPLAIN around the same
SQLAlchemy statement while preserving bind parameters; Q2 has no duplicate plan
SQL file. `delivery_analysis.run_delivery_baseline` verifies every month/state
group against an independent Python classification of order rows fetched in
5,000-row batches. It performs the same two warmups/seven measurements as Q1,
without executing Q1 or modifying database data/settings persistently.

All seven Q2 warm samples were below 200 ms (median 146.25 ms, maximum 158.59 ms).
This describes the saved local measurement, not cold-cache or concurrent load.
The original Q1 SQL and measurement remain unchanged.

## Q3: review satisfaction versus delivery delay

`queries/03_delivery_review_satisfaction.sql` keeps the Q2 delivery eligibility
and calendar-date definitions. It groups early, same-day, 1-3, 4-7, 8-14 and 15+
days late. Each order contributes at most one review: latest answer timestamp,
then creation timestamp, then review ID descending. Missing reviews remain in
coverage counts; mean score and low/high percentages use reviewed orders only.
Low scores are 1-2, high scores 4-5 and score 3 is neutral. An all-unreviewed
group has NULL score statistics. Pre-delivery responses are retained and counted
separately; the output describes association and does not establish causality.

`satisfaction_analysis.run_satisfaction_baseline` reuses the common read-only
benchmark and single-statement EXPLAIN wrapper. Its independent Python control
streams all source reviews in batches of 5,000, picks the latest per order and
recomputes every bucket and statistic outside timing. The six result rows are
saved in section 4.3 with exact numeric values and a two-panel chart. Q3's warm
baseline has median 414.49 ms and maximum 435.07 ms, above the 200 ms target;
optimization remains pending. Existing Q1/Q2 SQL and evidence are unchanged.
