import logging
import os
from contextvars import ContextVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, Status

from .observability import TOOLS


CONTENT_CONTROLS = {
    "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
    "ADK_TELEMETRY_IGNORE_RUN_CONFIG": "true",
    "ADK_DISABLE_LOAD_DOTENV": "true",
}
TOKEN_ATTRIBUTES = frozenset({
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.usage.reasoning.output_tokens",
})
OPERATIONS = frozenset({"invoke_agent", "invoke_workflow", "execute_tool", "generate_content"})
RESOURCE = Resource({"service.name": "travel-buddy"})
SCOPE = InstrumentationScope("travel_buddy.tracing")
EXPORT_ACTIVE = ContextVar("travel_trace_export_active", default=False)
EXPORT_LOGGERS = (
    "opentelemetry.exporter.cloud_trace",
    "google.api_core.retry",
    "google.cloud.trace_v2.services.trace_service.transports.grpc",
    "google.cloud.trace_v2.services.trace_service.transports.rest",
)


def safe_context(context):
    if context is None:
        return None
    return SpanContext(context.trace_id, context.span_id, context.is_remote, context.trace_flags)


def sanitize_span(span):
    source = span.attributes or {}
    attributes = {key: value for key, value in source.items()
                  if key in TOKEN_ATTRIBUTES and type(value) is int and value >= 0}
    operation = source.get("gen_ai.operation.name")
    if type(operation) is str and operation in OPERATIONS:
        attributes["gen_ai.operation.name"] = operation
    else:
        operation = None
    tool = source.get("gen_ai.tool.name")
    if type(tool) is str and tool in TOOLS:
        attributes["gen_ai.tool.name"] = tool
    name = "operation"
    if span.name in {"invocation", "call_llm"}:
        name = span.name
    elif operation in OPERATIONS:
        name = operation
    return ReadableSpan(
        name=name,
        context=safe_context(span.context),
        parent=safe_context(span.parent),
        resource=RESOURCE,
        attributes=attributes,
        events=(),
        links=(),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=SCOPE,
    )


class ExportDiagnosticFilter(logging.Filter):
    def filter(self, record):
        if not EXPORT_ACTIVE.get():
            return True
        return logging.LogRecord(record.name, record.levelno, __file__, 0,
                                 "Trace exporter diagnostic", (), None)


EXPORT_DIAGNOSTIC_FILTER = ExportDiagnosticFilter()


class SanitizingExporter(SpanExporter):
    def __init__(self, delegate):
        self.delegate = delegate
        for name in EXPORT_LOGGERS:
            logging.getLogger(name).addFilter(EXPORT_DIAGNOSTIC_FILTER)

    def export(self, spans):
        token = EXPORT_ACTIVE.set(True)
        try:
            sanitized = tuple(sanitize_span(span) for span in spans)
            return self.delegate.export(sanitized)
        except Exception:
            logging.getLogger(__name__).error("Trace export failed")
            return SpanExportResult.FAILURE
        finally:
            EXPORT_ACTIVE.reset(token)

    def shutdown(self):
        self.delegate.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)


def configure_tracing():
    os.environ.update(CONTENT_CONTROLS)
    if any(value for key, value in os.environ.items()
           if key.startswith("OTEL_EXPORTER_") and value):
        raise RuntimeError("External OTel exporter configuration is not supported")
    if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        raise RuntimeError("Tracing must be configured before any other provider")
    provider = TracerProvider(resource=RESOURCE)
    enabled = os.environ.get("TRAVEL_TRACE_EXPORT", "false").lower()
    if enabled not in {"true", "false"}:
        raise RuntimeError("TRAVEL_TRACE_EXPORT must be true or false")
    if enabled == "true":
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for trace export")
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        provider.add_span_processor(BatchSpanProcessor(SanitizingExporter(
            CloudTraceSpanExporter(project_id=project))))
    trace.set_tracer_provider(provider)
    return provider
