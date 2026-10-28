# Stage 02 - Database Performance

Status: source preparation, connection and initial DDL are complete. The saved
notebook includes Olist/NYC loading outputs. All five initial queries run against the populated
database with saved results, correctness tests and measured baselines.
Q1/Q3/Q4/Q5 exceed 200 ms; all seven saved Q2 runs are below 200 ms. Performance
optimization, views and routines remain pending. Older setup sections below
describe their original execution snapshots, including the initial empty tables.

- [solution.ipynb](solution.ipynb): source checks in sections 2.1-2.7 and executed cleaning in 2.8-2.10 and saved NYC inspection in 2.11-2.15.
- [report.md](report.md): schema decisions, alternatives, measured evidence and limitations.
- [Olist schema proposal](../../config/olist_schema.json): column types, keys and planned data policies.
- [Inspection module](../../src/data_factory/database/source_inspection.py): reusable key, relationship, numeric, date and postal checks.
- [Notebook evidence](evidence/quality/notebook_checks.json): input hashes and results produced by the module called from this notebook.
- [Q1 SQL](../../sql/queries/01_monthly_category_sales.sql): monthly category sales, unique category orders and category contribution per order.
- [Q1 measurements](evidence/benchmarks/q1_baseline.json): seven actual execution/fetch samples, coverage, reconciliation and measurement conditions.
- [Cleaning module](../../src/data_factory/olist_cleaning.py): normalization, exact-point deduplication, quarantine, reference completion and atomic publication.
- [Cleaning evidence](evidence/quality/cleaning_report.json): actual before/after counts, changes, quality flags, output types and hashes.
- [SQL layout](../../sql/README.md): versioned database definitions and queries.
- [Database configuration example](../../config/database.example.json): source/curated paths used by cleaning and planned database schemas.

Run shared commands from the repository root, using the installation instructions
in the [project guide](../../README.md). Run offline preparation with:

```powershell
python -m data_factory clean-olist
```

The existing `run --stage ...` commands remain available for stage 01. No database bootstrap command is available yet.
The root `requirements.txt` contains the stage 01 pins and stage 02 dependency
additions. The installation status of the new dependencies is recorded below.

## Database setup - step 1: dependencies

`pyproject.toml` declares SQLAlchemy 2.x and Psycopg 3 with the `binary` extra.
The [Psycopg binary installation](https://www.psycopg.org/psycopg3/docs/basic/install.html#binary-installation)
bundles the client libraries and avoids a local compiler/libpq setup.
`requirements.txt` pins the selected versions and the added transitive dependencies;
the previous stage 01 pins are preserved.

| Package | Selected version | Local Python 3.14 verification |
|---|---|---|
| SQLAlchemy | 2.0.49 | Installed from an existing cached Windows wheel; import passed |
| greenlet | 3.5.1 | Installed from an existing cached Windows wheel; import passed |
| typing_extensions | 4.16.0 | Already installed; import passed |
| psycopg with binary extra | 3.3.6 | Installed; import passed |
| psycopg-binary | 3.3.6 | Installed; import passed; `binary` implementation active |

**Step 1 is complete:** all five added packages match the selected versions and
import successfully in the project's existing virtual environment. SQLAlchemy's
Psycopg dialect resolves the installed driver, and Psycopg reports `binary`.
The editable project installation was refreshed offline and `pip check` passed.
The missing build backend was restored using the already-pinned
`setuptools==84.0.0` from local wheel cache. Earlier PyPI download failures were
resolved by the user's driver installation; no new network download was needed
for these verification checks.
See the [actual environment checks](evidence/quality/dependency_setup.json).
Verification covers the added database dependencies and build backend; the
entire historical stage 01 requirements lock was not reinstalled or revalidated.

To reproduce setup, use the intended virtual environment and run these commands
from the repository root (a fresh environment needs package download access):

```powershell
python -m pip install -r requirements.txt
python -m pip install --no-build-isolation -e .
python -m pip check
python -c "import sqlalchemy, psycopg; from psycopg import pq; print('SQLAlchemy:', sqlalchemy.__version__); print('Psycopg:', psycopg.__version__); print('Psycopg implementation:', pq.__impl__)"
```

Use `../venv/Scripts/python.exe` in place of `python` for the existing parent-folder
Windows environment if it is not activated. The final check prints both
selected versions and the Psycopg implementation `binary`.
These dependency checks do not connect to PostgreSQL. Connection helpers and the
real local server check are described below; DDL execution and data loading remain
later steps.

## Database setup - step 2: settings and engine

[connection.py](../../src/data_factory/database/connection.py) provides three APIs:

| API | Responsibility |
|---|---|
| `load_database_settings()` | Read and validate `DATABASE_URL` from the environment or the project's `.env` |
| `DatabaseSettings` | Hold the parsed URL; expose a credential-free `safe_url` |
| `create_database_engine(settings=None)` | Build a reusable SQLAlchemy Core Engine; read the environment if settings are omitted |

The selected driver prefix is `postgresql+psycopg://`. A database name is required,
and an explicit port must be numeric and between 1 and 65535. Username/password
may be omitted for libpq authentication. Host may be omitted for local libpq
defaults. Invalid configuration raises the shared `ConfigurationError` without
including the connection string or chaining a parser exception containing it.

The module reads the repository-root `.env` regardless of the working directory.
`python-dotenv==1.1.1` is installed and pinned for this purpose. Existing process
environment variables override file values. Loading does not change `os.environ`
and variable interpolation is disabled to preserve literal password characters.
An explicit `environ={...}` bypasses file loading for isolated callers; an optional
`env_file=...` selects a different file when using the real process environment.

Local settings are in the Git-ignored `.env`, with host `localhost`, port `5432`,
user `postgres` and database `ds_learning`, as selected by the user after checking
the listening port and available databases. The password stays only in `.env`.
`.env.example` contains placeholders and can be copied for a fresh checkout.
For a process-only override in PowerShell:

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@localhost:5432/ds_learning'
```

Percent-encode special characters in username/password in a URL string, such as
`@` as `%40` and `/` as `%2F`. The module retains connection options such as
`?sslmode=require`. For a programmatically assembled URL, use SQLAlchemy
`URL.create()` with `DatabaseSettings(url=...)` and an unescaped password instead.

```python
from data_factory.database.connection import load_database_settings, create_database_engine

settings = load_database_settings()
print(settings.safe_url)  # No username, password or query parameters.
engine = create_database_engine(settings)
try:
    print(engine.dialect.name, engine.dialect.driver)  # postgresql psycopg
finally:
    engine.dispose()
```

The engine is lazy: construction and disposal do not open a server connection.
Reuse the same engine for work targeting the same database and dispose it when
finished. SQL echo is off and SQL parameter values are hidden. The connection
timeout is 10 seconds per libpq connection target, and `pool_pre_ping=True` checks
connection health when a connection is checked out. It does not prove the server
is reachable at engine construction time. See [SQLAlchemy engine configuration](https://docs.sqlalchemy.org/en/20/core/engines.html)
and [pool health checks](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic).

The notebook now builds its engine from the real local settings and runs a read-only
query that verifies `SELECT 1`, the database, user and server version, without embedding
credentials in its source or outputs. Notebook section **1.1** now places this setup
immediately after project initialization, before source/schema work and SQL execution.
The shared `engine` stays available to all later SQL cells; each operation uses a
scoped Connection (`engine.connect()` for reads or `engine.begin()` for writes).
Pool disposal is the final notebook cell. Definitions remain in versioned `.sql`
files and will be executed through SQLAlchemy; writing the definitions does not
require an open database connection. The [step 2 evidence](evidence/quality/connection_setup.json)
records the configuration tests and notebook execution scope. A separate
[read-only connection check](evidence/quality/local_connection_check.json) records
the actual server connection attempt with these settings. No DDL or data load is
performed. Run the configuration tests
from the repository root:

```powershell
python -m unittest discover -s tests/database -p test_connection.py -v
```

**Step 2 scope:** the settings and engine APIs are ready and use `.env` by default.
The user's requested real target has been checked with a read-only connection
attempt; consult the separate connection evidence for its actual status.
Initial DDL execution is described below; data loading remains pending.

## Database setup - initial DDL execution

Notebook section **3.1** calls the shared
[DDL module](../../src/data_factory/database/ddl.py), after section 2 source/schema
inspection and before section 3.2 loading. It reuses the Engine from section 1.1.

```python
from data_factory.database.ddl import run_initial_ddl, inspect_initial_schema

ddl_report = run_initial_ddl(engine, PROJECT_ROOT / "sql/ddl")
committed_schema = inspect_initial_schema(engine)
```

Both initial `.sql` files run in order inside a single transaction. The execution
was committed in `ds_learning`; a separate post-commit Connection verified:

| Item | Measured result |
|---|---:|
| `olist` tables | 9 |
| `nyc311` tables | 3 |
| Primary keys | 12 |
| Foreign keys | 9 |
| CHECK constraints | 75 |
| UNIQUE constraints | 1 |
| Total data rows | 0 |

NOT NULL is recorded through column nullability rather than the constraint-count
table. Column types/defaults/identity details, all keys/checks, raw file hashes and
the execution scope are in [ddl_execution.json](evidence/quality/ddl_execution.json).
The notebook retains source inspection and cleaning results; only setup,
connection, DDL, committed-schema inspection and cleanup were executed in a
fresh IPython process. A full notebook/Jupyter-kernel run was not performed.

On rerun, a complete existing table set returns `already_present` and is not
recreated. Revised SQL is not automatically applied to existing tables, and the
runner does not claim to compare existing definitions with the files. A partial
or conflicting table set raises a configuration error without executing DDL.
No existing objects are dropped and no data is loaded by the application runner.

The [test evidence](evidence/quality/ddl_tests.json) records 25 passing database-layer
tests: 20 offline and 5 live PostgreSQL tests covering commit visibility, retained
data on rerun, rollback of both files, partial-object preservation and literal
semicolon/percent handling. The live fixtures used a unique temporary database
which was removed after the run. Reproduce with the configured local credentials:

```powershell
python tests/database/run_ddl_tests.py
```

This test runner needs permission to create a temporary database on the configured
server. It uses `postgres` for administrative commands and never uses `ds_learning`
for test fixture resets. Data loading is the next separately authorized stage.

## Shared implementation locations

| Location | Responsibility |
|---|---|
| `src/data_factory/sources/` | Olist CSV source adapter |
| `src/data_factory/olist_cleaning.py` | Local preparation and audited publication before database loading |
| `src/data_factory/sinks/` | Database destination integrated with the factory |
| `src/data_factory/database/` | Source inspection, SQLAlchemy settings/engine and initial DDL; future benchmarking helpers |
| `sql/` | DDL, queries, indexes, views, functions and procedures |
| `tests/database/` | Offline settings and live isolated PostgreSQL DDL tests |
| `data/stage_02/raw/` | Local Olist source CSVs; excluded from Git |
| `data/stage_02/curated/` | Local generated datasets; excluded from Git |
| `evidence/` | Measured plans, timings, and load-quality summaries |

Use one PostgreSQL database with separate `olist` and `nyc311` schemas. Extend
the shared factory to load both sources; preserve the existing validation,
quarantine, and manifest behavior. Do not copy the stage 01 package here.

## Evidence to collect during implementation

1. Schema decisions and rejected-row counts, including foreign-key violations.
2. Five analytical queries with repeatable timing and before/after execution plans.
3. Justification, storage size and write overhead for each index, plus one removed index.
4. An analysis brought within budget using a change beyond adding indexes.
5. Two views and a measured comparison of a live view with a snapshot table.
6. A parameterized, atomic, idempotent monthly snapshot procedure and a function.
7. SQLAlchemy connection cleanup, bound parameters, chunked reads and explicit write types.
8. At least six meaningful data-layer tests and eight meaningful new commits.

Target query budget: 200 ms. Notebook runtime limit: 10 minutes, excluding initial
data loading. Store actual results in the notebook and reference them in the report.

## Prepared output

Start with `data/stage_02/curated/olist/latest.json`. Its paths identify the
content-addressed output directory, deterministic cleaning report and per-execution
manifest. Ten CSV tables are published, including the expanded `product_categories`
reference and generated `postal_areas`. Source data is not overwritten.

`deduplicated.jsonl` links each discarded point to its retained source record;
`quarantine.jsonl` retains rejected raw records and reasons; `generated.jsonl`
records category provenance. `source_row` means the one-based CSV data-record
ordinal excluding the header. The report includes explicit SQL types for CSV
columns and the rule that an empty CSV field represents NULL.

Identical inputs, contract and code reuse the same data directory. Execution
manifests have separate IDs and actual timestamps. Database loading remains pending.

## Saved NYC source

The notebook resolves `data/curated_data/delivery/latest.json` and verifies its
manifest instead of hardcoding a run hash. The JSONL reader preserves text IDs
and previews 5,000 rows; the inspection streams all saved cases. Reusable logic
is in [nyc_inspection.py](../../src/data_factory/database/nyc_inspection.py), with
a [logical schema proposal](../../config/nyc_schema.json) and
[measured evidence](evidence/quality/nyc_source_checks.json). Three tables are
proposed: cases, population areas, and ZIP aliases. This saved acquisition is
partial; the full selected 18-month scope is not available. No database writes
or network acquisition are performed by this inspection.

## Q1: monthly category sales baseline

Notebook section 4.1 defines completed merchandise sales as item prices from
`delivered` orders, grouped by purchase month and original category key. It
excludes freight and retains NULL categories. Its average is the category's
contribution per distinct order containing the category. Multi-category order
counts are not additive. Default dates cover 2016 inclusive through 2019
exclusive; missing month/category combinations are not invented as zero rows.

The real result has 1,273 groups, 96,478 eligible orders and 110,197 items;
sales reconcile to BRL 13,221,498.11. The saved warm-cache baseline is above
200 ms and is explicitly not query-budget acceptance. No new indexes or views
were created. Only setup/connection and the new Q1 cells were executed in a
fresh IPython process; loading and other previous cells were preserved, and the
pool was disposed. A full notebook/Jupyter-kernel run was not performed.

Seven Q1 checks passed, including six live fixture tests in a temporary database
that was removed. [Test evidence](evidence/quality/q1_tests.json) records scope
and output. To run the fixture tests, point `DATA_FACTORY_TEST_DATABASE_URL` at a
dedicated `df_ddl_test_*` PostgreSQL database and run:

```powershell
python -m unittest discover -s tests/database -p test_sales.py -v
```

Without that variable, only the SQL-file consistency test runs and the six live
tests are skipped. Never point fixture settings at the analysis database.

## Q2: delivery timeliness by purchase month and customer state

Section 4.2 uses [one SQL file](../../sql/queries/02_monthly_state_delivery.sql).
Late delivery means the actual calendar date is after the estimated DATE.
Only delivered orders with valid dates and the eligibility flag enter the rate
denominator; unknowns stay in coverage counts, and zero denominators yield NULL.
The month is the purchase cohort, matching Q1. The order-to-customer join preserves
one row per order; no item/payment/review joins are needed.

Actual results: 556 month/state groups, 96,478 delivered orders, 96,470 eligible,
8 missing actual delivery dates, 6,534 late and 89,936 on time. Overall late
delivery is 6.77%, computed from totals rather than averaging group percentages.
All groups match an independent streamed Python control. The notebook includes
monthly/state summaries and a trend chart for the five highest-volume states;
sparse extreme cohorts are labelled with their counts.

Seven warm execute/full-fetch samples measured 124.95-158.59 ms (median 146.25 ms),
all below the 200 ms budget in this local run. The plan still contains a disk
sort; its presence alone does not imply budget failure. EXPLAIN is compiled from
the same SQLAlchemy statement, preserving binds, with no second Q2 SQL file.
Only setup/connection and Q2 cells were executed in fresh IPython; previous
notebook cells/outputs and Q1 evidence were preserved, and the Engine pool was
disposed. A complete notebook/Jupyter-kernel run was not performed.

Run the nine Q2 tests in an automatically created/verified temporary database:

```powershell
python tests/database/run_ddl_tests.py --pattern test_delivery_analysis.py
```

This needs permission to create a temporary database, as does the existing full
database runner. Eight tests run on PostgreSQL and one checks EXPLAIN compilation
and safe binds. At Q2 delivery, the full database suite passed 41 tests: 5 live DDL,
14 live analytical and 22 offline checks. The runner now reports live analytical
tests separately and Q1/Q2 fixture schemas are isolated by rollback. All temporary
test databases were removed. Evidence is in [Q2 tests](evidence/quality/q2_tests.json)
and the [full database suite](evidence/quality/ddl_tests.json).

## Q3: review satisfaction versus delivery delay

Section 4.3 uses [one SQL file](../../sql/queries/03_delivery_review_satisfaction.sql),
the same SQLAlchemy EXPLAIN wrapper and a dark SQL display. It preserves Q2's
eligibility and date rules. Delay groups are early, on the promised date, 1-3,
4-7, 8-14 and 15+ days late. One latest answered review per order is selected,
with creation timestamp and review ID as deterministic tie-breakers. Orders
without reviews remain in coverage. Review score statistics use only reviewed
orders; low means 1-2, high means 4-5. The analysis is descriptive and counts
selected responses recorded before delivery separately.

Actual coverage: 96,470 eligible orders, 95,824 with a selected review and 646
without. Among eligible orders, 525 have multiple reviews; 529 additional source
review rows are not selected, but remain in the source. There are 4,653 selected
responses recorded before delivery. All six groups and their statistics match
an independent streamed Python calculation. Mean scores are 4.29 (early), 4.03
(same date), 3.29 (1-3 days), 2.10 (4-7), 1.67 (8-14) and 1.72 (15+).

Seven warm execute/fetch samples measured 407.48-435.07 ms, median 414.49 ms;
the Q3 baseline does not meet 200 ms yet. The plan and conditions are saved for
the optimization stage. No index, view, snapshot or persistent setting changed.
Only setup/connection and Q3 cells were executed in fresh IPython; earlier
notebook cells, outputs and Q1/Q2 evidence were preserved, and the Engine pool
was disposed. A complete notebook/Jupyter-kernel run was not performed.

Run the ten PostgreSQL fixture tests in an automatically created/verified
temporary database:

```powershell
python tests/database/run_ddl_tests.py --pattern test_satisfaction_analysis.py
```

At Q3 delivery, the full database suite passed 51 tests: 5 live DDL, 24 live analytical and
22 offline checks. All temporary test databases were removed. See the
[Q3 tests](evidence/quality/q3_tests.json) and [full suite](evidence/quality/ddl_tests.json).

## Q4: top 20 sellers with preparation and review proxies

Section 4.4 uses [one SQL file](../../sql/queries/04_top_seller_performance.sql)
and the same dark SQL display, read-only benchmark and EXPLAIN wrapper. It ranks
completed merchandise sales by seller's own item-price share, excluding freight.
Revenue ties use seller ID ascending. The default `top_n=20` is bound, as are
the purchase-date interval and delivered status.

One seller/order pair contributes once to averages, regardless of item/review
counts. Preparation is fractional days from approval to carrier, only with its
validity flag, known timestamps and a nonnegative interval; zero stays valid.
Latest reviews follow Q3. Missing or invalid metric values retain revenue and
are reported in coverage counts. Order-level timestamps and review scores are
proxies for sellers on multi-seller orders, not individual seller measurements.

Actual coverage: 2,970 sellers, 110,197 items and 96,478 distinct delivered
orders. Across all sellers, item shares reconcile to BRL 13,221,498.11. There
are 1,275 multi-seller orders and 97,819 seller/order assignments. Top-20 revenue
is BRL 2,813,946.82 (21.28% of the total). The first seller,
`4869f7a5dfa277a7dca6462dcf3b52b2` in Guariba/SP, has BRL 226,987.93 from
1,124 orders, mean preparation 2.38 days and mean selected score 4.15.

The independent streamed Python control verifies all ranks, revenue and averages.
Seven warm runs measured 801.11-1,324.07 ms, median 1,033.66 ms; Q4 does not meet
200 ms yet. The actual plan is saved for optimization. No new index, view,
snapshot or persistent setting changed. Only setup/connection and Q4 cells were
executed in fresh IPython; earlier notebook cells, outputs and Q1-Q3 evidence
were preserved, and the Engine pool was disposed. A complete notebook/Jupyter
kernel run was not performed.

Run the eleven PostgreSQL fixture tests in an automatically created and verified
temporary database:

```powershell
python tests/database/run_ddl_tests.py --pattern test_seller_analysis.py
```

At Q4 delivery, the full database suite passed 62 tests: 5 live DDL, 35 live analytical and
22 offline. All temporary test databases were removed. See the
[Q4 tests](evidence/quality/q4_tests.json) and [full suite](evidence/quality/ddl_tests.json).

## Q5: payment methods, installment plans and order values

Section 4.5 uses [one SQL file](../../sql/queries/05_payment_methods_installments.sql)
with two clearly separated result levels. Exclusive `order_profile` rows cover
every order exactly once, including mixed-method/plan and no-payment groups.
Recorded `payment_plan` rows describe actual payment methods and installments;
one order can appear in several plans. Order counts and full merchandise values
must not be summed across plan groups or across the two levels. Repeated
same-plan payment rows count once per order when averaging basket values.

Merchandise value is item price without freight. Payment values are counted once
per row, never multiplied by installment count. Missing payment amounts and
missing basket values stay NULL rather than imputed zeros; observed zero values
remain valid. Unknown/partly unknown installments are retained, not replaced by
one. Comparison of one installment with two or more uses only single-method
credit-card orders with homogeneous, known installment counts, and weights
group means by valued-order counts. It is descriptive rather than causal.

Actual coverage: 96,478 orders and 100,756 payment rows, with 31 exclusive profile
groups and 27 plan groups. Merchandise total is BRL 13,221,498.11; recorded payment
total is BRL 15,422,461.77. There are 2,875 orders with multiple payment rows,
2,182 with mixed methods, one without payment and two with unknown installments.
All orders have item values in this snapshot. Pure credit-card orders are
74.75% (72,122); boleto 19.89% (19,191). Within homogeneous known credit-card
plans, one installment has mean basket BRL 82.27 (23,161 orders), versus 171.23
for two or more (48,815 orders). Rare plan groups are highlighted in the chart.

All 58 rows match an independent Python reconstruction of separately streamed
orders/items/payments. Seven warm runs measured 1,138.45-1,209.05 ms, median
1,190.08 ms; Q5 does not meet the 200 ms budget yet. No new index, view, snapshot
or persistent setting changed. Only setup/connection and Q5 cells were executed
in fresh IPython; previous cells/outputs and Q1-Q4 evidence were preserved and
the Engine pool was disposed. A full notebook/Jupyter-kernel run was not performed.

Run the thirteen PostgreSQL fixture tests in an automatically created and
verified temporary database:

```powershell
python tests/database/run_ddl_tests.py --pattern test_payment_analysis.py
```

The full database suite now passes 75 tests: 5 live DDL, 48 live analytical and
22 offline. All temporary test databases were removed. See the
[Q5 tests](evidence/quality/q5_tests.json) and [full suite](evidence/quality/ddl_tests.json).
