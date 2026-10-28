# Stage 02 - Database Performance

Status: Olist inspection, cleaning and a logical schema proposal are complete,
with saved notebook results and audited curated CSVs. All 330,000 saved NYC cases have also been inspected. PostgreSQL loading,
analytical queries, performance measurements, and database integration tests remain pending.

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
These checks do not connect to PostgreSQL. Connection helpers,
server checks, DDL execution and data loading belong to later approved steps.

## Shared implementation locations

| Location | Responsibility |
|---|---|
| `src/data_factory/sources/` | Olist CSV source adapter |
| `src/data_factory/olist_cleaning.py` | Local preparation and audited publication before database loading |
| `src/data_factory/sinks/` | Database destination integrated with the factory |
| `src/data_factory/database/` | Source inspection; future SQLAlchemy and benchmarking helpers |
| `sql/` | DDL, queries, indexes, views, functions and procedures |
| `tests/database/` | Controlled fixtures and database integration tests |
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
