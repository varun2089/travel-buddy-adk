import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import ModuleType
from unittest.mock import MagicMock, patch

from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.telemetry import ContentCapturingMode, TelemetryConfig
from google.genai import types
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags, TraceState

from travel_agent.agent import root_agent
from travel_agent.observability import ObservabilityPlugin
from travel_agent.server import create_app
from travel_agent.tracing import CONTENT_CONTROLS, SanitizingExporter, configure_tracing
from test_agent_runtime import FakeModel, INPUTS


SECRET = "SENSITIVE_SENTINEL_8934"


class RecordingExporter(SpanExporter):
    def __init__(self):
        self.spans = []
        self.closed = False

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.closed = True

    def force_flush(self, timeout_millis=30000):
        return True


class SanitizerTests(unittest.TestCase):
    def test_real_sdk_batch_export_preserves_structure_and_removes_all_text_channels(self):
        raw = RecordingExporter()
        safe = RecordingExporter()
        provider = TracerProvider(resource=Resource({"secret": SECRET}))
        provider.add_span_processor(SimpleSpanProcessor(raw))
        provider.add_span_processor(BatchSpanProcessor(SanitizingExporter(safe)))
        self.addCleanup(provider.shutdown)
        tracer = provider.get_tracer(SECRET, SECRET, schema_url=SECRET,
                                     attributes={"secret": SECRET})
        external = SpanContext(123, 456, True, TraceFlags(1), TraceState([("secret", SECRET)]))
        context = trace.set_span_in_context(trace.NonRecordingSpan(external))
        with tracer.start_as_current_span(SECRET, context=context, links=[Link(external, {"secret": SECRET})]) as parent:
            parent.set_attribute("gen_ai.agent.description", SECRET)
            parent.set_attribute("gen_ai.conversation.id", SECRET)
            with tracer.start_as_current_span(SECRET) as child:
                child.set_attributes({
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "search_travel_policy",
                    "gen_ai.tool.description": SECRET,
                    "gcp.vertex.agent.tool_call_args": SECRET,
                    "gcp.vertex.agent.tool_response": SECRET,
                    "gen_ai.usage.input_tokens": 12,
                    "gen_ai.usage.output_tokens": SECRET,
                    "gen_ai.usage.reasoning.output_tokens": True,
                })
                child.add_event(SECRET, {"secret": SECRET})
                child.record_exception(RuntimeError(SECRET))
                child.set_status(Status(StatusCode.ERROR, SECRET))
        self.assertTrue(provider.force_flush())
        self.assertEqual(len(safe.spans), 2)
        for original, sanitized in zip(raw.spans, safe.spans):
            self.assertEqual(original.context.trace_id, sanitized.context.trace_id)
            self.assertEqual(original.context.span_id, sanitized.context.span_id)
            self.assertEqual(original.parent.span_id, sanitized.parent.span_id)
            self.assertEqual(original.start_time, sanitized.start_time)
            self.assertEqual(original.end_time, sanitized.end_time)
            self.assertEqual(original.kind, sanitized.kind)
            self.assertEqual(original.status.status_code, sanitized.status.status_code)
            self.assertFalse(sanitized.events)
            self.assertFalse(sanitized.links)
            self.assertFalse(sanitized.context.trace_state)
            self.assertFalse(sanitized.parent.trace_state)
            self.assertIsNone(sanitized.status.description)
            self.assertEqual(sanitized.resource.attributes, {"service.name": "travel-buddy"})
            self.assertEqual(sanitized.instrumentation_scope, InstrumentationScope("travel_buddy.tracing"))
            self.assertNotIn(SECRET, sanitized.to_json())
        self.assertIn(SECRET, raw.spans[0].to_json())
        self.assertEqual(safe.spans[0].name, "execute_tool")
        self.assertEqual(safe.spans[0].attributes, {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "search_travel_policy",
            "gen_ai.usage.input_tokens": 12,
        })

    def test_sanitization_failure_never_reaches_delegate(self):
        delegate = RecordingExporter()
        exporter = SanitizingExporter(delegate)
        with patch("travel_agent.tracing.sanitize_span", side_effect=ValueError("invalid span")):
            self.assertEqual(exporter.export([object()]), SpanExportResult.FAILURE)
        self.assertFalse(delegate.spans)
        self.assertTrue(exporter.force_flush(10))
        exporter.shutdown()
        self.assertTrue(delegate.closed)

    def test_startup_defaults_off_and_locks_capture(self):
        with patch.dict(os.environ, {"ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "true"}, clear=True), patch(
            "travel_agent.tracing.trace.get_tracer_provider", return_value=trace.ProxyTracerProvider()), patch(
            "travel_agent.tracing.trace.set_tracer_provider") as install:
            provider = configure_tracing()
            self.addCleanup(provider.shutdown)
            install.assert_called_once_with(provider)
            self.assertFalse(provider._active_span_processor._span_processors)
            for key, value in CONTENT_CONTROLS.items():
                self.assertEqual(os.environ[key], value)
            config = RunConfig(telemetry=TelemetryConfig(
                capture_message_content=ContentCapturingMode.SPAN_AND_EVENT))
            self.assertFalse(config.telemetry.should_add_content_to_legacy_spans)
            self.assertFalse(config.telemetry.should_add_content_to_experimental_spans)
            self.assertFalse(config.telemetry.should_add_content_to_logs)

    def test_cloud_exporter_is_only_registered_behind_sanitizer(self):
        module = ModuleType("opentelemetry.exporter.cloud_trace")
        delegate = RecordingExporter()
        module.CloudTraceSpanExporter = MagicMock(return_value=delegate)
        with patch.dict(os.environ, {"TRAVEL_TRACE_EXPORT": "true", "GOOGLE_CLOUD_PROJECT": "offline"}, clear=True), patch.dict(
            "sys.modules", {module.__name__: module}), patch(
            "travel_agent.tracing.trace.get_tracer_provider", return_value=trace.ProxyTracerProvider()), patch(
            "travel_agent.tracing.trace.set_tracer_provider"):
            provider = configure_tracing()
            self.addCleanup(provider.shutdown)
            with provider.get_tracer("offline").start_as_current_span(SECRET):
                pass
            self.assertTrue(provider.force_flush())
        module.CloudTraceSpanExporter.assert_called_once_with(project_id="offline")
        self.assertEqual(len(delegate.spans), 1)
        self.assertNotIn(SECRET, delegate.spans[0].to_json())

    def test_unsafe_startup_configuration_fails_closed(self):
        for environment in ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://unused"},
                            {"TRAVEL_TRACE_EXPORT": "true"}, {"TRAVEL_TRACE_EXPORT": "invalid"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), patch(
                "travel_agent.tracing.trace.get_tracer_provider", return_value=trace.ProxyTracerProvider()), patch(
                "travel_agent.tracing.trace.set_tracer_provider") as install:
                with self.assertRaises(RuntimeError):
                    configure_tracing()
                install.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch(
            "travel_agent.tracing.trace.get_tracer_provider", return_value=object()):
            with self.assertRaises(RuntimeError):
                configure_tracing()

    def test_launcher_preserves_server_configuration_and_flushes(self):
        provider = MagicMock()
        with patch("travel_agent.server.configure_tracing", return_value=provider), patch(
            "google.adk.cli.fast_api.get_fast_api_app") as factory, patch.dict(os.environ, {"PORT": "8088", "SESSION_DATABASE_URL": "postgresql+psycopg://runtime:placeholder@localhost/travel_buddy"}):
            app = create_app()
        self.assertIs(app, factory.return_value)
        arguments = factory.call_args.kwargs
        self.assertEqual(arguments["max_llm_calls"], 5)
        self.assertTrue(arguments["session_service_uri"].startswith("postgresql+psycopg://"))
        self.assertEqual(arguments["artifact_service_uri"], "memory://")
        self.assertEqual(arguments["extra_plugins"], ["travel_agent.observability.ObservabilityPlugin"])
        for key in ("web", "reload_agents", "trace_to_cloud", "otel_to_cloud"):
            self.assertFalse(arguments[key])
        self.assertEqual(arguments["port"], 8088)
        import asyncio

        async def close():
            async with arguments["lifespan"](None):
                pass

        asyncio.run(close())
        provider.force_flush.assert_called_once_with(timeout_millis=5000)
        provider.shutdown.assert_called_once()


class RunnerTracingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = TracerProvider(resource=Resource({"service.name": SECRET}))
        cls.safe = RecordingExporter()
        cls.raw = RecordingExporter()
        cls.provider.add_span_processor(SimpleSpanProcessor(cls.raw))
        cls.provider.add_span_processor(BatchSpanProcessor(SanitizingExporter(cls.safe)))
        trace.set_tracer_provider(cls.provider)

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()

    def setUp(self):
        from travel_agent.request_context import PreparationRequest, preparation_request
        token = preparation_request.set(PreparationRequest("offline", "request-1"))
        self.addCleanup(preparation_request.reset, token)
        self.provider.force_flush()
        self.safe.spans.clear()
        self.raw.spans.clear()
        self.environment = patch.dict(os.environ, CONTENT_CONTROLS)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.output = io.StringIO()
        self.capture = redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    async def run_model(self, model, tools=None):
        agent = root_agent.clone(update={"model": model, "description": SECRET,
                                         "instruction": SECRET, "tools": tools or root_agent.tools})
        runner = InMemoryRunner(app=App(name="offline", root_agent=agent, plugins=[ObservabilityPlugin()]))
        self.addAsyncCleanup(runner.close)
        session = await runner.session_service.create_session(app_name="offline", user_id=SECRET, session_id=SECRET)
        events = [event async for event in runner.run_async(
            user_id=SECRET, session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=SECRET)]),
            run_config=RunConfig(max_llm_calls=5, telemetry=TelemetryConfig(
                capture_message_content=ContentCapturingMode.SPAN_AND_EVENT)))]
        return events

    def assert_safe(self):
        self.assertTrue(self.provider.force_flush())
        self.assertTrue(self.safe.spans)
        for span in self.safe.spans:
            self.assertNotIn(SECRET, span.to_json())
            self.assertFalse(span.events)
            self.assertIsNone(span.status.description)
        self.assertNotIn(SECRET, self.output.getvalue())
        raw = {span.context.span_id: span for span in self.raw.spans}
        for span in self.safe.spans:
            original = raw[span.context.span_id]
            self.assertEqual(span.start_time, original.start_time)
            self.assertEqual(span.end_time, original.end_time)
            self.assertEqual(span.parent.span_id if span.parent else None,
                             original.parent.span_id if original.parent else None)

    async def test_successful_answer_policy_arguments_and_results(self):
        def search_travel_policy(query: str):
            return {"status": "ok", "policy_text": SECRET, "answer": SECRET}

        class AnswerModel(FakeModel):
            async def generate_content_async(self, llm_request, stream=False):
                if not self.calls:
                    self.calls += 1
                    yield LlmResponse(content=types.Content(role="model", parts=[types.Part(
                        function_call=types.FunctionCall(name="search_travel_policy", args={"query": SECRET}))]))
                else:
                    yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=SECRET)]))

        events = await self.run_model(AnswerModel(), [search_travel_policy])
        self.assertTrue(any(event.content and any(part.text == SECRET for part in event.content.parts or [])
                            for event in events))
        self.assert_safe()
        operations = {span.attributes.get("gen_ai.operation.name") for span in self.safe.spans}
        self.assertIn("invoke_agent", operations)
        self.assertIn("execute_tool", operations)
        self.assertIn("call_llm", {span.name for span in self.safe.spans})
        for span in self.raw.spans:
            self.assertNotIn(SECRET, span.attributes.get("gcp.vertex.agent.llm_request", ""))
            self.assertNotIn(SECRET, span.attributes.get("gcp.vertex.agent.tool_response", ""))

    async def test_tool_exception_preserves_safe_business_error(self):
        with patch("travel_agent.db.session.create_session_factory", side_effect=RuntimeError(SECRET)) as factory:
            events = await self.run_model(FakeModel(batches=[[dict(INPUTS, purpose=SECRET)]]))
        factory.assert_called_once()
        responses = [part.function_response.response for event in events if event.content
                     for part in event.content.parts or [] if part.function_response]
        self.assertEqual(responses[0]["error"]["code"], "tool_execution_error")
        self.assert_safe()
        self.assertIn('"event": "tool.failure"', self.output.getvalue())

    async def test_unhandled_tool_exception_events_are_removed(self):
        def search_travel_policy(query: str):
            raise RuntimeError(SECRET)

        agent = root_agent.clone(update={"model": FakeModel(
            tool_name="search_travel_policy", batches=[[{"query": SECRET}]]),
            "tools": [search_travel_policy], "on_tool_error_callback": None})
        runner = InMemoryRunner(agent=agent, app_name="offline")
        self.addAsyncCleanup(runner.close)
        session = await runner.session_service.create_session(app_name="offline", user_id=SECRET)
        with self.assertRaises(RuntimeError):
            async for event in runner.run_async(user_id=SECRET, session_id=session.id,
                                                new_message=types.Content(parts=[types.Part(text=SECRET)])):
                pass
        self.assert_safe()
        self.assertTrue(any(span.events for span in self.raw.spans))
        self.assertTrue(any(span.status.status_code == StatusCode.ERROR for span in self.safe.spans))

    def test_http_request_cannot_enable_content_capture(self):
        loader = MagicMock()
        loader.list_agents.return_value = ["offline"]
        loader.load_agent.return_value = root_agent.clone(update={"model": FakeModel(), "instruction": SECRET})
        with tempfile.TemporaryDirectory() as directory, patch(
            "travel_agent.server.configure_tracing", return_value=MagicMock()), patch(
            "google.adk.cli.fast_api.AgentLoader", return_value=loader), patch(
            "dotenv.load_dotenv", side_effect=AssertionError("dotenv must not be read")):
            from google.adk.sessions import InMemorySessionService
            with patch("travel_agent.server.session_database_url", return_value="postgresql+psycopg://runtime:placeholder@localhost/test"), patch(
                "google.adk.cli.fast_api.create_session_service_from_options", return_value=InMemorySessionService()):
                app = create_app(directory)
            with TestClient(app) as client:
                session = client.post(f"/apps/offline/users/{SECRET}/sessions", json={})
                self.assertEqual(session.status_code, 200, session.text)
                response = client.post("/run", json={
                    "app_name": "offline", "user_id": SECRET, "session_id": session.json()["id"],
                    "new_message": {"role": "user", "parts": [{"text": SECRET}]},
                    "run_config": {"max_llm_calls": 999, "telemetry": {
                        "capture_message_content": "SPAN_AND_EVENT"}},
                    "telemetry": {"capture_message_content": "SPAN_AND_EVENT"},
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("Finished", response.text)
        self.assert_safe()
        for span in self.raw.spans:
            self.assertNotIn(SECRET, span.attributes.get("gcp.vertex.agent.llm_request", ""))

    async def test_model_exception_is_preserved_without_exporting_error_text(self):
        class FailingModel(FakeModel):
            async def generate_content_async(self, llm_request, stream=False):
                raise failure
                yield

        failure = RuntimeError(SECRET)
        with self.assertRaises(RuntimeError) as caught:
            await self.run_model(FailingModel())
        self.assertIs(caught.exception, failure)
        self.assert_safe()
        self.assertTrue(any(span.events for span in self.raw.spans))

    async def test_model_error_response_is_not_exported(self):
        class ErrorModel(FakeModel):
            async def generate_content_async(self, llm_request, stream=False):
                yield LlmResponse(error_code=SECRET, error_message=SECRET)

        events = await self.run_model(ErrorModel())
        self.assertTrue(any(event.error_code == SECRET for event in events))
        self.assert_safe()
