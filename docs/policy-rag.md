# Travel Policy RAG

Retrieves fictional university travel policies using Gemini embeddings and PostgreSQL with pgvector.
Policies are synthetic training data, not Monash policies.

## Setup

Run from the project root. Docker Desktop must be running.

```bash
set -a
source ./.env
set +a
export ADK_DISABLE_LOAD_DOTENV=true

.venv/bin/python -m pip install -e .
docker compose --env-file .env up -d --wait postgres
.venv/bin/alembic upgrade head
```

The trusted root `.env` supplies database settings; Compose does not export them for Alembic. Do not source `travel_agent/.env`. ADC is configured separately below.

Keep credentials out of Git. Before changing an existing database image, back up its data. Do not delete its volume.

Policy embeddings explicitly use Vertex AI with Application Default Credentials (ADC),
project `gen-lang-client-0842015305` and location `us-central1`. For local ADC setup,
run `gcloud auth application-default login`. The project needs Vertex AI enabled
and the ADC identity needs permission to invoke its models. Shared backend
environment settings are unchanged; generation independently uses `global`.

Google's [embedding guide](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/embeddings/get-text-embeddings)
documents `gemini-embedding-001` in `us-central1`; its
[API reference](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/model-reference/text-embeddings-api)
supports configurable output dimensions and both retrieval task types. Checked
with installed `google-genai` 2.29.0 on 2026-10-24. The client retains 768 dimensions,
`RETRIEVAL_DOCUMENT` for ingestion, `RETRIEVAL_QUERY` for search, unit normalization,
input limits, a 10-second timeout and two retry attempts. Safe outward errors and
the diagnostic containing only exception type and HTTP status are unchanged.

This backend change preserves the model and stored vectors. Do not re-ingest
existing policies for this change. The documentation does not guarantee identical
vectors across backends; offline checks establish request compatibility, not
retrieval quality or project access. After ADC and the database connection are
available, check one existing active policy without ingestion or migrations:

```bash
.venv/bin/python -m travel_agent.evaluation.run retrieval --live --case review-draft --limit 1
```

## Ingest Policies

```bash
.venv/bin/travel-policy-ingest policies/synthetic_travel.md
```

Ingestion splits the document into sections, generates embeddings and stores them. It calls Gemini and may incur charges.

Unchanged versions are skipped. Changed content requires a new version.

## Activate Policies

```bash
.venv/bin/python -m travel_agent.policy.admin list
.venv/bin/python -m travel_agent.policy.admin activate SYN-TRAVEL v1
```

Use the document ID and version shown by `list`. Search includes all active policies, with one active version per document.

To deactivate without deleting content:

```bash
.venv/bin/python -m travel_agent.policy.admin deactivate SYN-TRAVEL
```

## Run

```bash
.venv/bin/adk web --port 8000 --max_llm_calls 5
```

Open http://localhost:8000 and ask:

> According to the synthetic travel policies, what receipts are required? Cite the relevant sections.

Search returns relevant passages with citation IDs. If evidence is missing or retrieval fails, the agent should explain the limitation rather than invent an answer.

## Tests

Offline tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

PostgreSQL tests require a separate test database and `TEST_DATABASE_URL` loaded:

```bash
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
.venv/bin/python -m unittest discover -s tests -v
```

Database tests are skipped when `TEST_DATABASE_URL` is absent. Live embedding checks are opt-in.

## Limitations

- Policies are fictional.
- Citation validity checks that IDs refer to retrieved evidence; it does not establish that evidence supports a claim. Human assessment of claim support and semantic correctness remains separate; see [evaluation](evaluation.md). Similarity scores do not prove support.
- Retrieved content must be treated as untrusted data.
- Policy search cannot approve, book or submit travel.
- Live answer quality requires separate evaluation.
