# Testing

Run commands from the project root with the virtual environment active.

## Offline tests

```sh
ADK_DISABLE_LOAD_DOTENV=true RUN_LIVE_POLICY_CHECKS=0 TEST_DATABASE_URL='' SESSION_TEST_DATABASE_URL='' python -m unittest discover -s tests -v
```

Offline tests use synthetic fixtures, mocks and a fake ADK model; they make no real model calls.
Clearing `TEST_DATABASE_URL` and `SESSION_TEST_DATABASE_URL` ensures PostgreSQL tests are reported as skipped, not passed.
Live embedding checks and ADK dotenv loading are explicitly disabled.
See [trace-only observability](tracing.md) for export privacy coverage and the remaining activation checks.

Coverage includes:

- Destination matching, unsupported destinations and fixture isolation.
- Trip dates, money validation, registration, budget comparisons and decimal arithmetic.
- Validation before transaction access, adapter error responses and engine cleanup.
- ADK enforcement of five model calls before refusing a sixth.
- Safe errors reaching the model and repeated preparation blocked after success, validation failure or uncertain storage outcomes.
- Multiple preparation calls in one response, changed arguments, and another attempt in a new invocation.

## Separate PostgreSQL test database

First complete the [development setup](../README.md#setup), including loading the trusted `.env` into the shell and starting PostgreSQL.
Keep `DATABASE_URL` pointing at the development database.

Create a separate database once, using a name ending in `_test` and different from the development database:

```sh
docker compose --env-file .env exec postgres createdb -U "$POSTGRES_USER" travel_buddy_test
```

Set `TEST_DATABASE_URL` in your local shell to the SQLAlchemy URL for that database.
Use the `postgresql+psycopg` driver and the same configured host, published port and database user.
Supply credentials locally; do not paste them into documentation or logs.

Migrate only the test database with a command-local override, then run all tests:

```sh
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
python -m unittest discover -s tests -v
```

The override does not change the development `DATABASE_URL` in your shell. To run only PostgreSQL tests:

```sh
python -m unittest discover -s tests -p 'test_proposals_postgres.py' -v
```

Tests require an explicit test URL, a database name ending in `_test`, and a name different from `DATABASE_URL`.
They do not start containers, create databases or apply migrations. A configured but unavailable or unmigrated database fails rather than being skipped.

Use a dedicated test database without valuable data. Cleanup removes rows created by each test; it does not reset or drop the database.

## PostgreSQL coverage

- Preparation, exact stored amounts and persistence through new connections.
- Same key and equivalent normalized inputs return the same proposal ID, stored inputs/results and current review state, without recalculation.
- Same key and changed valid inputs return `idempotency_key_conflict`; a new key starts a new preparation with a new ID for valid inputs.
- Concurrent preparations with matching or conflicting inputs, recovery through a new connection, and rollback of proposal/request persistence when initial review insertion fails.
- Database-enforced immutability.
- Approval, confirmation and audit persistence in one transaction.
- Repeat-safe and concurrent duplicate approvals.
- Rejection, conflicting transitions and explicit reviewer confirmation.
- Approval blocked for unknown registration or exceeded budgets.
- Full review rollback when audit creation fails.

Offline idempotency tests also cover normalization, missing/malformed/duplicate headers, request-context isolation through streaming, and read-only recovery responses. The per-invocation preparation guard still blocks a second attempt in the same run; durable retry checks apply to explicit later requests. See [the retry and recovery contract](architecture.md#proposal-idempotency-and-recovery).

See [durable sessions](sessions.md) for offline session configuration coverage and the dedicated database restart test.
