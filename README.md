# Travel Buddy

A travel assistant using Google ADK, a local MCP estimates server and PostgreSQL. Prices and policies are synthetic; submission is simulated.

## Setup

Run from the project root with Python 3.12 or newer:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Create a trusted, shell-compatible root `.env` with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT` and `DATABASE_URL`. Keep credentials out of Git. `DATABASE_URL` must use `postgresql+psycopg` and match the local database credentials and published port, with host `127.0.0.1`.

```sh
set -a
source ./.env
set +a
export ADK_DISABLE_LOAD_DOTENV=true
docker compose --env-file .env up -d --wait postgres
alembic upgrade head
```

Repeat the shell-loading step in each terminal. Compose's `--env-file` does not export variables for Alembic or Python; application database code does not load dotenv files. Migrations prepare proposal/policy storage, including proposal request keys in migration 0004.

## Vertex AI credentials

Vertex AI uses Application Default Credentials (ADC), separately from database settings:

```sh
gcloud auth application-default login
```

The configured project is `gen-lang-client-0842015305`; generation uses `global`, policy embeddings use `us-central1`. The project needs Vertex AI enabled and your ADC identity needs model access. Cloud Run uses its attached service account. See [policy retrieval](docs/policy-rag.md).

## Policy and estimates demo

```sh
adk web --port 8000 --max_llm_calls 5
```

Open <http://localhost:8000>, select `travel_agent`, and ask for Sydney estimates or synthetic policy evidence. Policy search requires ingested, active policies. ADK launches the estimates MCP subprocess automatically. This demo has no proposal request-key middleware.

## Save and review proposals

Use the [custom HTTP server workflow](docs/architecture.md#custom-http-server-workflow). It requires `SESSION_DATABASE_URL` for separately prepared durable ADK sessions, alongside `DATABASE_URL` for proposals/policies; see [session setup](docs/sessions.md).

Supply a caller-created `Idempotency-Key` on `/run` or `/run_sse`. Persist the key before sending: equivalent retries return the original proposal ID, changed inputs conflict, and new preparations need new keys. After an uncertain save, use `GET /proposal-preparations/recovery` with the same header before explicitly retrying. The shared key scope does not enforce user ownership.

Review the returned UUID with the CLI:

```sh
travel-review show PROPOSAL_ID
travel-review approve PROPOSAL_ID --reviewer reviewer@example.test --confirm
# Alternatively, reject a proposal still awaiting review:
travel-review reject PROPOSAL_ID --reviewer reviewer@example.test --confirm
```

Reviewer identifiers are unauthenticated audit labels. No real booking or NuTrip submission occurs.

## Further documentation

- [Architecture, idempotency and recovery](docs/architecture.md)
- [Private Cloud Run learning deployment](docs/cloud-run-learning.md)
- [Durable sessions](docs/sessions.md)
- [Policy RAG](docs/policy-rag.md) and [evaluation](docs/evaluation.md)
- [Testing](docs/testing.md) and [sanitized tracing](docs/tracing.md)
