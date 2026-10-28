# Stage 02 - Database Performance

Status: Olist inspection/cleaning, saved NYC inspection, the real PostgreSQL
connection, and initial DDL execution are complete. The two schemas contain 12
empty tables. PostgreSQL loading, analytical queries, performance measurements,
and tests for later data operations remain pending.

- [solution.ipynb](solution.ipynb): source checks in sections 2.1-2.7 and executed cleaning in 2.8-2.10 and saved NYC inspection in 2.11-2.15.
- [report.md](report.md): schema decisions, alternatives, measured evidence and limitations.
- [Olist schema proposal](../../config/olist_schema.json): column types, keys and planned data policies.
- [Inspection module](../../src/data_factory/database/source_inspection.py): reusable key, relationship, numeric, date and postal checks.
- [Notebook evidence](evidence/quality/notebook_checks.json): input hashes and results produced by the module called from this notebook.
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
