**# Architecture

## Components

| File | Responsibility |
| --- | --- |
| `travel_agent/agent.py` | Root ADK agent, instructions and tool registration, including the MCP toolset |
| `travel_agent/mcp_server.py` | FastMCP stdio server exposing synthetic `get_travel_estimates` |
| `travel_agent/server.py` | Custom ADK HTTP launcher, durable sessions, request middleware and recovery endpoint |
| `travel_agent/request_context.py` | Validated caller key and server-owned shared scope for preparation |
| `travel_agent/session_config.py` | Session URL validation and fixed `adk_sessions` search path |
| `travel_agent/callbacks.py` | Preparation guard and safe unexpected tool errors |
| `travel_agent/tools/travel_estimates.py` | Synthetic Sydney and Brisbane estimates |
| `travel_agent/tools/calculate_trip_cost.py` | Validation, itemised costs and budget comparison |
| `travel_agent/tools/prepare_travel_proposal.py` | Agent adapter and database error handling |
| `travel_agent/services/proposals.py` | Preparation and human-review rules |
| `travel_agent/db/models.py` | Immutable proposals with scoped request keys, review and audit tables |
| `travel_agent/db/repositories.py` | Database queries and inserts |
| `travel_agent/db/session.py` | Database configuration and session factory |
| `travel_agent/review_cli.py` | Human review commands |

The agent starts the MCP server with the current Python executable and `-m travel_agent.mcp_server`, using ADK `McpToolset` over stdio and exposing only `get_travel_estimates`. The server delegates to the same synthetic fixture lookup used by the calculator. It does not save proposals or call a live pricing service.

## Proposal workflow

Preparation accepts a destination, dates in `YYYY-MM-DD`, a non-empty purpose, proposed budget in AUD and optional registration fee.
Null or omitted registration means unknown; zero means known. Validation runs before the service opens a transaction.
Unknown registration leaves the complete total unconfirmed; exceeding known costs produces an over-budget result.

Valid proposals receive a UUID and `NEEDS_REVIEW` status. Inputs preserve destination spelling, purpose, dates and decimal amounts.
Results and monetary inputs use decimal strings in PostgreSQL JSONB.
The calculator shares an exact `Decimal` calculation path with its JSON-number tool interface; precision already lost by a caller cannot be recovered.

Proposal contents are immutable, enforced by a database trigger.
Corrections require a new proposal. Review state lives in a separate row. Approval, rejection and submission are never agent tools.

## Human review

Review mutations require a non-empty reviewer identifier and `--confirm`. The identifier is unauthenticated and does not establish identity or permission.

Approval requires known registration and costs within the proposed budget. It saves `SUBMITTED`, a `MOCK-` confirmation and an audit record in one transaction.
Row locking serialises concurrent decisions. Repeated approval returns the existing confirmation without another audit record.

Rejection saves `REJECTED`. Rejected proposals cannot be submitted; conflicting review transitions fail. An audit failure rolls back the entire transition.
Atomicity covers the mock PostgreSQL operation only. A future external submission API needs external submission idempotency, an outbox and reconciliation of uncertain outcomes.

## Bounded execution and errors

ADK 2.11.0 creates the runner for `adk web`. The launch command in the [quick start](../README.md) passes `--max_llm_calls 5`, which sets ADK's per-invocation `RunConfig` limit.
ADK raises `LlmCallsLimitExceededError` before a sixth model call.

Omitting the option uses `ADK_MAX_LLM_CALLS` or ADK's default of 500. Zero or negative limits permit unbounded calls.
The limit bounds the model loop, not elapsed time, database operations or tools requested in a single response.
Exhaustion is a runtime error and may leave no final assistant explanation.

The before-tool callback records a preparation attempt in temporary ADK state before executing the tool.
Further attempts in the same invocation return `proposal_attempt_already_made`, even after validation or storage failure or with changed arguments.
Other tools remain available. The agent is instructed to wait for a new explicit user request before trying preparation again.

| Response | Meaning |
| --- | --- |
| Existing validation error codes | Invalid input; preserve the tool's validation response |
| `no_data` | No synthetic estimate for the destination |
| `database_configuration_error` | Missing or invalid database configuration |
| `proposal_storage_error` | Database operation failed; saving could not be confirmed |
| `tool_execution_error` | Unexpected exception handled by ADK's error callback |
| `proposal_attempt_already_made` | Another preparation attempt in this invocation was blocked |

The new error responses contain `status`, an `error` with `code` and `message`, and `retryable: false`. Exception details and credentials are not returned.
Configuration `ValueError` is caught around factory creation; SQLAlchemy errors are handled separately. Other exceptions reach the registered ADK callback.
The adapter disposes its engine in `finally`.

## Proposal idempotency and recovery

Preparation requires a caller-created `Idempotency-Key` header: 1–128 ASCII letters, digits, dots, underscores, colons or hyphens. Persist it before sending the request and reuse it for retries of the same operation. New operations and corrections need new keys. Missing headers permit ordinary chat but block preparation. Duplicate or malformed headers return HTTP 400.

Pure ASGI middleware carries the header through server-owned ContextVar state for the complete HTTP/SSE lifecycle. The tool reads that state directly; model arguments, session state, user IDs and request metadata cannot select the key or scope. `PROPOSAL_REQUEST_SCOPE` configures the shared namespace, defaulting to `travel-buddy:prepare:v1`. Keep it stable across revisions and restarts. All allowed invokers share this namespace; it does not establish ownership.

Migration 0004 adds a unique scoped key and versioned normalized inputs to immutable proposals. Existing rows remain without keys. Normalization strips destination/purpose edges, case-folds destination, preserves purpose case/internal whitespace, validates ISO dates and represents finite nonnegative money as exact decimal strings without insignificant zeros. Unknown registration remains distinct from zero. Original inputs remain stored separately.

Matching retries return stored inputs/results and current review state without recalculating estimates. Changed valid normalized inputs return `idempotency_key_conflict`. Proposal, key and initial review commit together. Concurrent insert losers roll back and read the winner in a fresh transaction; only the named request uniqueness violation takes this path. Approval rules, review audit and the per-invocation guard remain unchanged.

After an uncertain save, explicitly call `GET /proposal-preparations/recovery` with the same header. It returns the current proposal (200), no committed match (404), or a safe storage error (503), with `Cache-Control: no-store`. It neither invokes a model nor writes data. A 404 may race an in-flight commit: poll or explicitly retry the original operation with its original key and inputs. Never infer rollback from a lost response or invent a replacement key. Automatic model retries remain prohibited. A model that changes inputs on a retry receives a conflict; recovery avoids regenerating arguments.

This protects proposal preparation only, not all effects of replaying an ADK run. The shared scope and recovery endpoint are suitable only for the private trusted learning group. Caller user/session IDs are unauthenticated. Multi-user use requires a verified principal/tenant, ownership enforcement and identity-scoped keys. Do not put keys in URLs, logs, prompts or traces. Apply migration 0004 separately before database testing or enabling preparation; startup does not apply it.

## Custom HTTP server workflow

From the repository root, activate the virtual environment and load the root `.env` as in the [quick start](../README.md#setup). Set `SESSION_DATABASE_URL` to the restricted runtime URL after completing [session schema preparation](sessions.md#administrative-preparation). It is separate from the proposal/policy `DATABASE_URL`; Alembic does not prepare session tables.

The module launcher defaults to `/app` for the Docker image. For local use, point the app factory at the repository root explicitly:

```sh
ADK_DISABLE_LOAD_DOTENV=true python -c 'import os, uvicorn; from travel_agent.server import create_app; uvicorn.run(create_app(agents_dir=os.getcwd()), host="127.0.0.1", port=8080)'
```

This serves the ADK API without the web UI, with five model calls per invocation, PostgreSQL sessions, in-memory artifacts, safe event logging and optional sanitized tracing. Create a session, then send a save request (Vertex AI ADC is required for the model call):

```sh
curl --fail-with-body -X POST \
  http://127.0.0.1:8080/apps/travel_agent/users/learner/sessions/demo-1 \
  -H 'Content-Type: application/json' -d '{}'

# Example key only: create and persist a unique key for each new preparation.
curl --fail-with-body http://127.0.0.1:8080/run \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-preparation-1' \
  -d '{"appName":"travel_agent","userId":"learner","sessionId":"demo-1","newMessage":{"role":"user","parts":[{"text":"Save a Sydney travel proposal for 2026-11-16 to 2026-11-18, purpose Attend conference, proposed budget AUD 1500 and registration AUD 200, for human review."}]}}'
```

Read the tool result for the proposal ID; HTTP success alone does not confirm a save. If the response is lost or saving is uncertain, recover without a model call:

```sh
curl --fail-with-body http://127.0.0.1:8080/proposal-preparations/recovery \
  -H 'Idempotency-Key: demo-preparation-1'
```

Use the same persisted key on recovery and any explicit retry. A fresh key starts a new preparation and can duplicate a previous uncertain save. Review a recovered or returned proposal through [the CLI](../README.md#save-and-review-proposals).

## Runtime and production gaps

The application database code and Alembic use the process environment; neither loads `.env` files.
Follow the quick start's explicit shell-loading step. Imports do not connect to the database or apply migrations.

Compose runs PostgreSQL 17 with persistent storage and a port bound to `127.0.0.1`.
Changing environment variables does not change credentials in an already initialised volume.

Production work includes user and reviewer authentication, access authorisation, real submission integration and replay protection for external effects, secrets management, deployment security, monitoring, backups and recovery.**
