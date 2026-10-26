# Stage 02 - Database Performance

Status: scaffold only. PostgreSQL loading, analytical queries, performance
measurements, and database tests have not been implemented or executed.

- [solution.ipynb](solution.ipynb): Persian notebook outline and project setup.
- [report.md](report.md): decision-report template; fill with measured evidence.
- [SQL layout](../../sql/README.md): versioned database definitions and queries.
- [Database configuration example](../../config/database.example.json): planned
  paths and schemas; not consumed by the current CLI.

Run shared commands from the repository root, using the installation instructions
in the [project guide](../../README.md). The current `data-factory` CLI still
provides stage 01 commands only. No database bootstrap command is available yet.
The root `requirements.txt` currently locks stage 01 dependencies. SQLAlchemy and
the PostgreSQL driver must be added and locked when database work is implemented.

## Shared implementation locations

| Location | Responsibility |
|---|---|
| `src/data_factory/sources/` | Olist CSV source adapter |
| `src/data_factory/sinks/` | Database destination integrated with the factory |
| `src/data_factory/database/` | SQLAlchemy connections, SQL execution, benchmarking |
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
