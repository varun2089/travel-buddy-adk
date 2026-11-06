# Durable conversation sessions

The server requires `SESSION_DATABASE_URL` using `postgresql+psycopg`. It uses ADK 2.11.0 DatabaseSessionService and fixes every session connection's search path to `adk_sessions`. Missing or invalid configuration stops startup with a generic error; URLs are never included in validation errors. URL `options`, `service` and `servicefile` overrides are rejected. Database connectivity is checked on session access, not server construction.

Session storage contains user messages, assistant messages, event data including tool inputs/results, and persistent state. Sanitized tracing and application logs do not sanitize this database. Restrict database access and define appropriate retention, deletion and backup policies before storing real conversations.

Persistence does not enforce ownership. ADK's app/user/session identifiers are lookup keys supplied by callers, not proof of identity. Keep Cloud Run IAM authentication and restricted invokers. This remains unsuitable for public multi-user access without application authorization.

Artifacts remain in memory. Existing proposal/policy tables, their `DATABASE_URL`, migrations, logging, sanitized tracing and five-model-call invocation limit remain unchanged. Session persistence is separate from [proposal preparation deduplication](architecture.md#proposal-idempotency-and-recovery), and does not protect future external submissions from replay.

## Administrative preparation

These are repeatable administrative setup instructions; server startup does not perform them. Install the project's updated dependencies in the intended environment; `SQLAlchemy[asyncio]` supplies greenlet while the existing psycopg binary dependency supplies the async PostgreSQL driver.

Use the existing database with a separate schema and session roles. A separate database is optional. Do not add these ADK tables to application Alembic metadata.

1. As an administrator, execute the role/schema creation statements in `scripts/session_permissions.sql` through the schema USAGE grant. The file is deliberately ordered around a preparation step: do not execute the later table grants until tables exist. Replace the database name for a dedicated test database. Assign the runtime password privately through your database administration tooling; do not put passwords in SQL files or command history.
2. Provide `SESSION_SETUP_DATABASE_URL` privately in the process environment, using a dedicated setup login capable of assuming `travel_session_owner`. Set that login's database-specific default role to `travel_session_owner`, so created objects belong to the owner role. The setup login needs CONNECT and membership in the owner role; runtime must not have that membership. Never give setup credentials to the server.
3. Run explicitly:

```sh
ADK_DISABLE_LOAD_DOTENV=true python -m travel_agent.session_setup
```

The installed entry point is also `travel-session-setup`. It reads only `SESSION_SETUP_DATABASE_URL`, calls public `prepare_tables()`, and closes the engine. It never creates the schema or falls back to runtime credentials. Setup errors omit database exception details and suppress dependency logging during preparation. Inspect failures privately and check database state before retrying.

4. Execute the remaining table grants. Review inherited PUBLIC privileges, particularly CREATE on public, database CREATE and TEMP; a fixed search path is not a substitute for database privilege controls. Remove unwanted PUBLIC rights only after checking other applications. Runtime needs no table ownership, schema CREATE, role membership in the owner, or metadata writes. The current schema uses string identifiers, so sequence grants are unnecessary.
5. Set `SESSION_DATABASE_URL` privately with the runtime login. Local form: `postgresql+psycopg://USER:ENCODED_PASSWORD@127.0.0.1:PORT/travel_buddy`. Percent-encode credentials. Start with `ADK_DISABLE_LOAD_DOTENV=true python -m travel_agent.server`; the launcher reads process variables only. For local use, explicitly export settings from the trusted root `.env` as in the [README](../README.md#setup), then use the [local app-factory command](architecture.md#custom-http-server-workflow); the module launcher defaults to `/app` inside the container.

Fresh preparation creates sessions, events, app_states, user_states, adk_internal_metadata and declared indexes, recording schema version 1 with JSON event serialization. ADK checks tables/indexes on first session access in each process. Pre-created objects allow this check with the restricted runtime role; a missing object must fail rather than granting runtime DDL permissions.

`create_all()` does not upgrade existing columns. Keep ADK pinned and prepare/review schema changes with setup credentials before runtime rollout. The included legacy v0-to-v1 migration copies from a source to a different destination; it does not support in-place migration. Back up and rehearse upgrades separately. Existing proposal/policy migrations are independent.

## Cloud configuration

Create a separate Secret Manager secret for the runtime session URL, and grant the existing runtime service account Secret Accessor only on that secret. Pin a specific secret version in Cloud Run configuration, for example `SESSION_DATABASE_URL=travel-buddy-session-database-url:1`, alongside the existing DATABASE_URL binding. Do not replace or expose the proposal/policy secret. No Secret Manager client code is needed; Cloud Run injects the environment variable.

The session secret uses `postgresql+psycopg://USER:ENCODED_PASSWORD@/travel_buddy?host=/cloudsql/PROJECT:REGION:INSTANCE`. Retain the existing Cloud SQL attachment, Cloud SQL Client access, network routing and private IAM invoker settings. Setup credentials belong only in a separate administrative environment and secret, never in the runtime service. No setup is run by server startup or deployment.

## Restart integration test

The local PostgreSQL restart test verified both synthetic conversation history (user and assistant messages) and persistent session state across separate processes. The Cloud Run check verified session state across revisions; it did not establish cloud conversation-history persistence. These recorded results were not rerun during this documentation update. Use only a dedicated disposable database whose name ends in `_test` and differs from DATABASE_URL, SESSION_DATABASE_URL and SESSION_SETUP_DATABASE_URL database names. Provision its schema and tables separately as above, then grant its runtime role the same restricted rights. Do not point this test at development or cloud application data.

Set `SESSION_TEST_DATABASE_URL` privately to that dedicated test runtime URL and run:

```sh
ADK_DISABLE_LOAD_DOTENV=true python -m unittest discover -s tests -p 'test_sessions_restart_postgres.py' -v
```

With no test URL it skips. With a URL it must succeed, not silently skip on failure. Process A persists synthetic user/assistant events and state using ADK, then exits. A separate process constructs the actual server and retrieves that history through the session HTTP API using TestClient. Exact message and state assertions establish storage survival across process replacement without model calls or cloud access. Cleanup deletes the uniquely identified session; synthetic app/user state rows remain in the disposable database. It does not create databases or run setup/migrations.
