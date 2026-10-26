# Database tests

Reserved for stage 02 integration tests and small, controlled fixtures. No test
cases are implemented yet. Keep the existing stage 01 offline suite independent
of PostgreSQL; put database connection setup inside the future database test runner.

Cover at least function values, monthly procedure idempotency, atomic rollback,
analytical-query correctness, reference-violation handling, and repeated loads.
Database tests must use an explicitly configured test database, not an existing
analysis database. Document the test command when the runner is implemented.
