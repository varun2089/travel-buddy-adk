# Trace-only observability

Export remains disabled with `TRAVEL_TRACE_EXPORT=false`. The offline exporter-serialization activation blocker is resolved for Google trace exporter 1.15.0, Google Cloud Trace client 1.21.0, ADK 2.11.0 and OTel API/SDK 1.42.1. Tests inspect actual serialized gRPC protobuf and REST JSON requests with network access blocked. Real ADC, IAM, ingestion and Cloud Run delivery remain unverified. No cloud resources were changed.

## Dependencies and startup

`pyproject.toml` adds these exact dependency declarations:

```text
opentelemetry-api==1.42.1
opentelemetry-sdk==1.42.1
opentelemetry-exporter-gcp-trace>=1.9,<2
```

The exporter constraint matches ADK 2.11.0's declared GCP dependency. It is not a resolved version lock; rerun the wire tests when dependency resolution changes. The project `.venv` now has exporter 1.15.0, Google Cloud Trace client 1.21.0, Google API Core 2.34.0 and gRPC 1.85.0 installed. OTel API/SDK remain pinned at 1.42.1, and `pip check` passes. The installation command must include the project pins to prevent pip from upgrading OTel beyond ADK's supported version:

```sh
.venv/bin/python -m pip install 'opentelemetry-exporter-gcp-trace>=1.9,<2' 'opentelemetry-api==1.42.1' 'opentelemetry-sdk==1.42.1'
```

Exporter 1.15.0 emits a deprecation warning for this exporter class; this implementation retains ADK 2.11.0's verified trace-only path. A future migration requires a new privacy and wire-format verification.

The Docker command is now `python -m travel_agent.server`. It calls ADK's `get_fast_api_app` with the existing API-only configuration: host `0.0.0.0`, port from `PORT` (default 8080), no reload, `max_llm_calls=5`, PostgreSQL sessions configured by required `SESSION_DATABASE_URL` and in-memory artifacts, and `travel_agent.observability.ObservabilityPlugin`. Agent tools, callbacks and business rules are unchanged.

This uses the same `CloudTraceSpanExporter(project_id=...)` and `BatchSpanProcessor` as ADK 2.11.0's installed `--trace_to_cloud` path, with a sanitizing exporter between them. ADK has no CLI option to insert this sanitizer into that path. Both `trace_to_cloud` and `otel_to_cloud` are false in the app factory so a second, unsanitized exporter is never registered. `--otel_to_cloud` additionally enables metrics and OTel logs and is outside this implementation.

The launcher owns the process tracer provider and refuses an existing provider or nonempty `OTEL_EXPORTER_*` configuration. Do not use an auto-instrumentation launcher or additional exporters. Explicitly pass runtime settings through the platform environment; the launcher disables ADK dotenv loading.

## Environment

The Docker image defaults and launch-time enforced settings are:

```text
TRAVEL_TRACE_EXPORT=false
ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT
ADK_TELEMETRY_IGNORE_RUN_CONFIG=true
ADK_DISABLE_LOAD_DOTENV=true
```

The launcher unconditionally enforces the last four settings before building the API server. A request's `RunConfig.telemetry` cannot override them. The current API also does not forward client-supplied run configuration; direct runner tests exercise the admin lock independently.

For a later approved activation, the only additional runtime configuration is:

```text
GOOGLE_CLOUD_PROJECT=gen-lang-client-0842015305
TRAVEL_TRACE_EXPORT=true
```

Missing exporter dependencies or project configuration fail startup rather than falling back to raw export. Do not set OTLP endpoints, enable message content capture, or add a second trace flag.

## Export boundary

The installed SDK passes ended `ReadableSpan` objects to `SpanExporter.export`. `SanitizingExporter` constructs new `ReadableSpan` snapshots using the SDK constructor and hands only those snapshots to its delegate. This is a custom exporter adapter, not an assumed SDK filter. It does not mutate the original spans or use private SDK mutation hooks. The SDK describes `ReadableSpan` as an SDK-created object; the snapshot construction and batch-export interface are covered by offline tests and must be reverified when upgrading the pinned SDK.

Only these attributes survive:

- `gen_ai.operation.name`: one of `invoke_agent`, `invoke_workflow`, `execute_tool`, `generate_content`.
- `gen_ai.tool.name`: one of the four existing allowlisted application tools.
- Nonnegative integer counts in `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.reasoning.output_tokens`. Booleans and text are excluded.

Span names become fixed operation names, `invocation`, `call_llm`, or `operation`. Resource attributes are replaced with the fixed `service.name=travel-buddy`; instrumentation scope is replaced with `travel_buddy.tracing`. All other attributes, scope metadata, links and events are discarded. That removes exception messages/stack traces, prompts, answers, policy evidence, tool arguments/results, user/session/invocation identifiers, free-text descriptions and potentially sensitive resource metadata. Status descriptions are discarded; status codes survive.

Trace IDs, span IDs, parent span IDs, trace flags, remote-context flags, span kind and start/end timestamps survive. Trace state is removed from both span and parent contexts. These tracing IDs permit correlation and are not anonymized replacements for user or session IDs. Raw spans may exist in process memory before export; the filter is an export boundary, not a memory-redaction mechanism.

The plugin's safe JSON stdout logs are unchanged. There is no OTel logging exporter or automatic log/trace correlation added. ADK/framework/Uvicorn logs remain separate and can contain exceptions; this sanitizer makes no claim to sanitize all container logs. A handled tool error can have a successful ADK span status even while the plugin correctly records `tool.failure`.

The actual exporter logs transport exceptions with traceback information. A context-scoped logging filter replaces exporter, Trace client transport and API Core retry diagnostics with a fixed message, retaining severity and removing exception/stack details and structured extras. It applies only during this wrapper's export call; unrelated log records and the application logging plugin are unchanged. Unexpected sanitizer/delegate exceptions produce only the fixed `Trace export failed` diagnostic and return `SpanExportResult.FAILURE` rather than escaping to SDK traceback logging. Existing framework logs outside trace export are outside this filter.

## APIs, IAM and authentication

For later activation, enable `cloudtrace.googleapis.com` in the destination project and grant the attached runtime service account `roles/cloudtrace.agent`, containing `cloudtrace.traces.patch`. Use ADC from Cloud Run's service identity; no service-account key is needed. Existing Vertex AI, Cloud SQL and Secret Manager permissions remain necessary. No new Telemetry, Monitoring or Logging API/role is required for this trace-only path. API enablement and IAM changes are administrator tasks, not runtime permissions.

The documented runtime identity is `travel-buddy-runtime@gen-lang-client-0842015305.iam.gserviceaccount.com`; its actual deployment/IAM has not been inspected. Preserve `--no-allow-unauthenticated`, `--invoker-iam-check`, restricted invokers and the existing deployment settings. This launcher adds no application authentication or user/session ownership mechanism. Trace viewers need `roles/cloudtrace.user`.

Cloud Trace's Trace Explorer can show sanitized agent/model/tool waterfalls and timing. It cannot display removed payloads or descriptions. Existing container logs remain available in Logs Explorer. Richer Application Monitoring dashboards require their own App Hub/resource association prerequisites; this implementation does not configure them.

## Offline verification and remaining checks

```sh
ADK_DISABLE_LOAD_DOTENV=true RUN_LIVE_POLICY_CHECKS=0 TEST_DATABASE_URL='' SESSION_TEST_DATABASE_URL='' PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

The tests exercise real SDK batching and real ADK runs with sensitive sentinels in prompts, instructions, answers, tool arguments, policy results, exceptions and model errors. They verify every exported span is free of sentinels, preserves IDs/parents/timing/status codes, and drops events, links, trace state and status descriptions. They also test startup rejection, export disabled by default, the capture admin lock, an HTTP capture override attempt, unchanged safe logging and the existing five-call/business regressions.

The real-exporter wire tests are in `tests/test_trace_wire.py`. Run them with:

```sh
TRAVEL_TRACE_EXPORT=false ADK_DISABLE_LOAD_DOTENV=true PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p test_trace_wire.py -v
```

These tests use the actual `CloudTraceSpanExporter` and Google Trace client. REST uses explicit `AnonymousCredentials`, with only the final session `post` replaced; assertions inspect its serialized JSON body and protobuf round trip. The default exporter construction and gRPC client/interceptor path use an in-memory channel stub that invokes the actual request serializer; assertions decode the captured protobuf bytes. ADC discovery, socket connections/DNS/datagrams, HTTP session requests and real gRPC channel creation are guarded with failures, and the tests assert no guard was invoked. No credential files or dotenv files are read, and no request leaves the process.

Successful exports retain parent relationships, span IDs, nanosecond timing, span kinds, safe labels and Cloud Trace status codes (`OK=0`, error mapped to `UNKNOWN=2`). Requests have no sentinel, events, links, stack traces, status messages/details, payloads, identifiers or descriptions. The exporter adds its fixed `g.co/agent` version label; no other labels outside the expected metadata set survive. Failure tests exercise HTTP error payloads, gRPC/HTTP transport exceptions, retries with debug logging enabled, and sanitizer failures. Captured stdout, stderr and logs contain neither the sentinel nor tracebacks. The application capture-lock and five-call regressions remain part of the focused checks.

The offline serialization blocker is resolved for the versions above. Export remains disabled; this verification does not authorize activation or prove cloud access or delivery.

After a separately approved deployment, use synthetic traffic to verify private access, trace ingestion, safe logs, and delivery after idle periods and shutdown. Cloud Run delivery is not verified offline.

## Cloud Run batching

The installed SDK defaults to a 5-second batch schedule, a 2048-span queue and 512-span maximum batches. Raw completed spans can wait in memory until a batch is exported. Request-based CPU throttling can delay background exports after the response; scale-to-zero, queue overflow, process crashes, OOM and forced termination can lose spans. Tracing is best effort, not an audit trail.

The ASGI lifespan attempts a 5-second `force_flush` followed by provider shutdown on graceful exit. This does not flush per request or guarantee delivery: an in-flight exporter RPC can exceed the flush deadline, and shutdown can outlast Cloud Run's termination grace period. The SDK's batch export-timeout setting does not itself bound exporter network calls. CPU allocation and collector changes are deliberately deferred; no billing or deployment settings were changed.

References: [ADK trace setup](https://adk.dev/observability/traces/), [Cloud Trace write permissions](https://docs.cloud.google.com/trace/docs/troubleshooting), [OTel Python trace/export API](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html), [Cloud Run execution lifecycle](https://docs.cloud.google.com/run/docs/container-contract).
