# Private Cloud Run learning container

The image installs the dependencies declared in `pyproject.toml` on Python 3.12,
runs as UID 10001, and serves `travel_agent` through the custom `travel_agent.server` ADK HTTP launcher from `/app`.
It binds to `0.0.0.0`, reads `PORT` (default 8080), disables reload and limits
each invocation to five model calls. Startup never runs migrations.

Sessions require a separately prepared PostgreSQL schema and SESSION_DATABASE_URL;
see [durable sessions](sessions.md) for setup, permissions and Secret Manager configuration.
Artifacts use `memory://` and are lost on process or instance replacement. Keep
this deployment private with Cloud Run IAM authentication and restricted invokers.
This learning setup has no application-level user/session ownership enforcement
and is not ready for public or multi-user production use.

Build a Linux amd64 image for Cloud Run and check HTTP startup on port 8097:

```sh
docker build --platform linux/amd64 -t travel-buddy-learning:local .
docker run --rm -d --name travel-buddy-learning-check \
  -e PORT=8097 \
  -e SESSION_DATABASE_URL=postgresql+psycopg://startup_check:unused@127.0.0.1:5432/startup_check \
  -p 127.0.0.1:8097:8097 travel-buddy-learning:local
curl --fail http://127.0.0.1:8097/health
curl --fail http://127.0.0.1:8097/list-apps
docker exec travel-buddy-learning-check id
docker stop travel-buddy-learning-check
```

These endpoints do not invoke the agent, call models or access the application
database. Do not call `/run` or `/run_sse` for a startup-only check. The launcher validates `SESSION_DATABASE_URL` at construction, so the example supplies
a syntactically valid placeholder URL. These endpoints do not connect to its database.
No real database credentials, ADC or environment file is required. Wait for Uvicorn
to start before running curl; this check does not establish session or tool readiness.

The build context allows only packaging files and the agent source, with explicit
exclusions for environment files, credentials and local runtime data. No local
credentials or environment files should be copied or mounted into the container.

For a later private deployment, attach the `travel-buddy-runtime` service account
so Vertex clients use ADC. Supply `DATABASE_URL` and `SESSION_DATABASE_URL` securely and configure reachable
PostgreSQL storage before using sessions, proposal or policy-search tools. Database schema
preparation is a separate administrative step. This packaging does not deploy,
provision infrastructure or alter stored policies or vectors.

## Verification boundary

Recorded Cloud Run checks verified proposal saving, equivalent retry with the same
key returning the original proposal, and recovery, plus session state across
revisions. MCP estimates integration was verified locally only; cloud MCP startup
and tool execution have not been verified. Local session restart testing checked
both conversation history and state; the cloud check establishes state persistence
only. These results were not rerun or checked against cloud services during this
documentation update.

## Deployment configuration

The following command documents the required configuration for a later deployment;
this exact command was not executed during this documentation update.
Run it from the repository root. Source deployment uploads source, builds with
the Dockerfile in Cloud Build, and may create the source-deployment Artifact
Registry repository. Required APIs, build/deployer permissions, the runtime
identity, Cloud SQL instance and secret versions must already be prepared.
The runtime identity needs Cloud SQL Client, Secret Manager Secret Accessor on
both specified secrets, and the Vertex AI permissions used by the agent.
These prerequisites have not been checked against the cloud project.

Secret `travel-buddy-database-url` version `1` must contain a SQLAlchemy
`postgresql+psycopg` URL for `travel_app` and database `travel_buddy`, using
the Unix socket host `/cloudsql/gen-lang-client-0842015305:us-east1:travel-buddy-db-dev`.
Secret `travel-buddy-session-database-url` version `1` must contain the separately
prepared session runtime role's `postgresql+psycopg` URL, using the same Cloud SQL
socket form (or a separately configured reachable session database). See
[session administration](sessions.md#administrative-preparation) for schema preparation
and restricted grants. Runtime must not receive `SESSION_SETUP_DATABASE_URL`.

The local proxy address `127.0.0.1:5433` is not a Cloud Run database endpoint.
The Cloud SQL attachment alone does not rewrite the URL or prepare the schema.
This socket configuration assumes the instance is reachable through the
Cloud SQL integration; a private-IP-only instance needs separate VPC routing.
See [Cloud SQL connections](https://docs.cloud.google.com/sql/docs/postgres/connect-run).

```sh
gcloud run deploy travel-buddy \
  --source=. \
  --project=gen-lang-client-0842015305 \
  --region=us-east1 \
  --service-account=travel-buddy-runtime@gen-lang-client-0842015305.iam.gserviceaccount.com \
  --set-cloudsql-instances=gen-lang-client-0842015305:us-east1:travel-buddy-db-dev \
  --set-secrets=DATABASE_URL=travel-buddy-database-url:1,SESSION_DATABASE_URL=travel-buddy-session-database-url:1 \
  --no-allow-unauthenticated \
  --invoker-iam-check \
  --min=0 \
  --max=1 \
  --concurrency=1 \
  --cpu=1 \
  --memory=1Gi \
  --cpu-throttling \
  --timeout=180s
```

Google Cloud SDK 590.0.1's installed `gcloud run deploy --help` was checked
locally for these flags. `--min` and `--max` apply across the service's revisions.
`--cpu-throttling` selects
[request-based billing](https://docs.cloud.google.com/run/docs/configuring/billing-settings).
IAM authentication is required; grant invocation only to intended learners.
Existing project/service IAM bindings still need review for broad invoker access.

The standalone `.gcloudignore` allows only Docker build inputs and excludes
credentials, environment files, virtual environments, Git history, backups,
evaluation results and local ADK data. It deliberately does not include
`.gitignore`. `.dockerignore` independently filters the container build context.
Local upload enumeration was checked without displaying sensitive paths or
reading excluded file contents. Repeat that check after changing build inputs.

The programmatic ADK launcher retains `max_llm_calls=5`, PostgreSQL sessions and
in-memory artifacts. Both secret bindings above are required; session tables must be prepared separately
before session access. Artifacts are discarded on restart. The local opt-in restart
test verifies conversation history and state; the recorded cloud check verified
state across revisions. Database proposals and review records persist separately.
Proposal preparation has durable scoped-key deduplication and read-only recovery
through the custom HTTP launcher; see [idempotency and recovery](architecture.md#proposal-idempotency-and-recovery).
The per-run guard alone does not provide durable replay protection, and preparation
deduplication does not protect future external submissions. Cloud Run can temporarily exceed its maximum during replacement;
the limit is not a session-affinity or strict spending guarantee.
This authenticated API still lacks application-level user/session ownership checks.

## Application event logs

The Docker launcher registers `travel_agent.observability.ObservabilityPlugin`
through ADK 2.11.0's `extra_plugins` class loader. For local use, append
`--extra_plugins travel_agent.observability.ObservabilityPlugin` to `adk web`
or `adk api_server`. A programmatic runner can register an instance through
`google.adk.apps.App(plugins=[ObservabilityPlugin()], ...)`. Registration is
per runner; importing the agent alone does not enable logging. The evaluation
harness is unchanged and does not register this plugin.

Each application event is one JSON stdout line with a timestamp, severity,
fixed event/outcome, generated invocation and operation IDs, and elapsed
milliseconds from a monotonic clock. Original invocation, session, user and
function-call identifiers are never emitted. Tool names are allowlisted; model
names are omitted. No prompts, answers, evidence, arguments, result bodies,
credentials or exception details are emitted. Output-write failures are ignored
so logging cannot replace business results or errors.

Model and tool starts describe observed callback attempts. Terminals distinguish
`ok`, `blocked` preparation, `returned_error`, `exception`, `no_data`, and
retrieval `ok`/`no_evidence`/`error`. Model response errors and interruptions
are failures. A tool exception followed by the existing safe error callback
produces one failure, without a second completion event. Invocation failure
means an observed error or unfinished operation, even when the runner returns
normally after handling a tool error. It does not establish whether a proposal
was saved. Existing guards, response callbacks and retry behaviour are unchanged.

Available numeric token counts appear in model terminal `usage`; missing fields
are omitted. Partial response chunks are not summed. These are provider-reported
counts, not exact cost estimates; embedding usage is not exposed here.

Cloud Run HTTP request logs describe transport status and latency. The plugin's
stdout events describe agent operations and are collected as container logs.
An HTTP success does not establish successful agent work. These events provide
logging, not distributed tracing. The launcher
supports [sanitized trace export](tracing.md), disabled by default. No HTTP trace header is
captured by the plugin, so request-log nesting is not automatic.

Run-error notifications close unfinished operations as `incomplete`, but this
phase does not cover all pre-run failures, cancellation, client disconnects,
process termination or OOM. ADK may treat early generator closure as completion;
that is not proof the client received a full answer. Unfinished operations make
an observed run terminal a failure, but missing terminals remain possible.
Cancellation that bypasses run terminals can retain in-memory bookkeeping until
runner close. Streaming usage arriving after an operation's terminal is not
captured. The callback pairing relies on ADK 2.11.0's shared model event-actions
object and per-call tool context; reverify these when upgrading ADK.
Framework, SDK and Uvicorn logs are separate and may still contain their own
payloads or tracebacks; this plugin does not sanitize them or claim complete
log privacy. The offline tests verify only the plugin's output.
