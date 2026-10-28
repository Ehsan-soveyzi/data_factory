# Database tests

`test_connection.py` covers step 2 settings and lazy engine creation offline.
It tests missing/malformed configuration, the driver and database requirements,
port validation, credential-free errors/display, encoded passwords, environment
isolation, and engine construction/disposal with network and Psycopg connections
guarded. Tests also cover real temporary `.env` files, process-environment
precedence, literal password preservation, safe file-reading errors and the
default engine factory path. The user's local `.env` is excluded from tests.
Run from the repository root:

```powershell
python -m unittest discover -s tests/database -p test_connection.py -v
```

These tests require SQLAlchemy and Psycopg but no PostgreSQL server or credentials.
They are also discovered by `python -m data_factory.testing`, whose full suite
disables outbound connections. Live DDL tests skip unless an explicitly isolated
test database is configured. Keep stage 01 offline tests independent of a running
PostgreSQL server.

`test_ddl.py` adds three offline checks and five live PostgreSQL checks: committed
table/key creation, preserved data on rerun, rollback across both files, partial
schema preservation, and whole-script literal handling. Run all database-layer
tests using the credentials from the local project `.env`:

```powershell
python tests/database/run_ddl_tests.py
```

The runner creates a uniquely named `df_ddl_test_*` database through the `postgres`
administrative database, configures `DATA_FACTORY_TEST_DATABASE_URL` only for the
test subprocess, and removes that same database after verifying its OID. It does
not force-drop connections. The configured analysis database is never used for
fixtures; schema resets occur only in the isolated test database. Creating the
temporary database requires the local role to have CREATEDB or equivalent rights.
Results are saved to `stages/stage_02/evidence/quality/ddl_tests.json`.

Later stages must add function values, monthly procedure idempotency,
analytical-query correctness, reference-violation handling, and repeated loads.
Server integration tests must use an explicitly configured test database, not an existing
analysis database.
