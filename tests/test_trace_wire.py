import io
import json
import logging
import os
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from importlib.metadata import version
from unittest.mock import MagicMock, patch

import grpc
from google.auth.credentials import AnonymousCredentials
from google.cloud.trace_v2 import BatchWriteSpansRequest, TraceServiceClient
from google.cloud.trace_v2.services.trace_service.transports.rest import TraceServiceRestTransport
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExportResult
from opentelemetry.trace import Link, SpanContext, SpanKind, Status, StatusCode, TraceFlags, TraceState
from requests import Request, Response

from travel_agent.tracing import EXPORT_LOGGERS, SanitizingExporter
from test_tracing import RecordingExporter


SECRET = "WIRE_SENSITIVE_SENTINEL_82719"


class TraceWireTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "TRAVEL_TRACE_EXPORT": "false", "ADK_DISABLE_LOAD_DOTENV": "true",
            "GOOGLE_API_USE_MTLS_ENDPOINT": "never", "GOOGLE_API_USE_CLIENT_CERTIFICATE": "false",
        }))
        self.guards = [self.stack.enter_context(patch(target, side_effect=AssertionError("Network or ADC forbidden")))
                       for target in (
                           "socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
                           "socket.socket.connect_ex", "socket.socket.sendto",
                           "requests.sessions.Session.request", "grpc.secure_channel", "grpc.insecure_channel",
                           "google.auth.default",
                       )]
        self.transport = TraceServiceRestTransport(credentials=AnonymousCredentials())
        self.addCleanup(self.transport.close)
        self.client = TraceServiceClient(transport=self.transport)
        self.exporter = CloudTraceSpanExporter(project_id="offline-trace-project", client=self.client)
        self.post = self.stack.enter_context(patch.object(self.transport._session, "post"))
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        for name in EXPORT_LOGGERS:
            logger = logging.getLogger(name)
            level = logger.level
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)
            self.addCleanup(logger.setLevel, level)
            self.addCleanup(logger.removeHandler, handler)
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.errors))

    def response(self, code=200):
        response = Response()
        response.status_code = code
        response.url = "https://cloudtrace.googleapis.com/v2/projects/offline-trace-project/traces:batchWrite"
        response.headers["Content-Type"] = "application/json"
        response.request = Request("POST", response.url).prepare()
        response._content = json.dumps({"error": {"code": code, "message": SECRET}} if code != 200 else {}).encode()
        return response

    def spans(self):
        raw = RecordingExporter()
        provider = TracerProvider(resource=Resource({"secret.resource": SECRET}))
        provider.add_span_processor(SimpleSpanProcessor(raw))
        self.addCleanup(provider.shutdown)
        tracer = provider.get_tracer(SECRET, SECRET, schema_url=SECRET, attributes={"secret.scope": SECRET})
        upstream = SpanContext(0x1234, 0x5678, True, TraceFlags(1), TraceState([("secret", SECRET)]))
        context = trace.set_span_in_context(trace.NonRecordingSpan(upstream))
        with tracer.start_as_current_span(SECRET, context=context, kind=SpanKind.SERVER,
                                           start_time=1700000000123456789, end_on_exit=False) as parent:
            parent.set_attributes({"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": SECRET,
                                   "gen_ai.agent.description": SECRET, "gcp.vertex.agent.llm_request": SECRET,
                                   "gcp.vertex.agent.llm_response": SECRET})
            with tracer.start_as_current_span(SECRET, kind=SpanKind.CLIENT, start_time=1700000001123456789,
                                               links=[Link(upstream, {"secret.link": SECRET})], end_on_exit=False) as child:
                child.set_attributes({
                    "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search_travel_policy",
                    "gen_ai.tool.description": SECRET, "gcp.vertex.agent.tool_call_args": SECRET,
                    "gcp.vertex.agent.tool_response": SECRET, "policy_text": SECRET,
                    "gen_ai.usage.input_tokens": 17,
                })
                child.add_event(SECRET, {"secret.event": SECRET})
                child.record_exception(RuntimeError(SECRET))
                child.set_status(Status(StatusCode.ERROR, SECRET))
                child.end(end_time=1700000002123456789)
            parent.set_status(Status(StatusCode.OK))
            parent.end(end_time=1700000003123456789)
        return raw.spans

    def request(self, expected_calls=1):
        self.assertEqual(self.post.call_count, expected_calls)
        arguments = self.post.call_args
        self.assertEqual(arguments.args[0], self.response().url)
        body = arguments.kwargs["data"]
        self.assertNotIn(SECRET, body)
        self.assertNotIn(SECRET, repr(arguments))
        parsed = BatchWriteSpansRequest.from_json(json.dumps({
            "name": "projects/offline-trace-project", **json.loads(body)}))
        binary = BatchWriteSpansRequest.serialize(parsed)
        self.assertNotIn(SECRET.encode(), binary)
        return BatchWriteSpansRequest.pb(BatchWriteSpansRequest.deserialize(binary))

    def assert_quiet_and_offline(self):
        for guard in self.guards:
            guard.assert_not_called()
        for output in (self.output, self.errors, self.logs):
            self.assertNotIn(SECRET, output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
        self.assertEqual(os.environ["TRAVEL_TRACE_EXPORT"], "false")

    def test_real_exporter_serializes_sanitized_request(self):
        self.post.return_value = self.response()
        spans = self.spans()
        self.assertIn(SECRET, spans[0].to_json())
        self.assertEqual(SanitizingExporter(self.exporter).export(spans), SpanExportResult.SUCCESS)
        request = self.request()
        self.assert_wire_spans(spans, request)
        self.assert_quiet_and_offline()

    def assert_wire_spans(self, spans, request):
        self.assertEqual(len(request.spans), len(spans))
        expected_agent = f"opentelemetry-python {version('opentelemetry-sdk')}; google-cloud-trace-exporter {version('opentelemetry-exporter-gcp-trace')}"
        for original, wire in zip(spans, request.spans):
            trace_id = f"{original.context.trace_id:032x}"
            span_id = f"{original.context.span_id:016x}"
            self.assertEqual(wire.name, f"projects/offline-trace-project/traces/{trace_id}/spans/{span_id}")
            self.assertEqual(wire.span_id, span_id)
            self.assertEqual(wire.parent_span_id, f"{original.parent.span_id:016x}")
            self.assertEqual(wire.start_time.ToNanoseconds(), original.start_time)
            self.assertEqual(wire.end_time.ToNanoseconds(), original.end_time)
            self.assertFalse(wire.HasField("time_events"))
            self.assertFalse(wire.HasField("links"))
            self.assertFalse(wire.HasField("stack_trace"))
            self.assertTrue(wire.HasField("status"))
            self.assertFalse(wire.status.message)
            self.assertFalse(wire.status.details)
            self.assertEqual(wire.status.code, 2 if original.status.status_code == StatusCode.ERROR else 0)
            labels = wire.attributes.attribute_map
            expected = {"gen_ai.operation.name", "g.co/agent"}
            if original.status.status_code == StatusCode.ERROR:
                expected |= {"gen_ai.tool.name", "gen_ai.usage.input_tokens"}
                self.assertEqual(labels["gen_ai.usage.input_tokens"].int_value, 17)
            self.assertEqual(set(labels), expected)
            self.assertEqual(labels["g.co/agent"].string_value.value, expected_agent)
        self.assertEqual(request.spans[0].parent_span_id, request.spans[1].span_id)
        self.assertEqual(request.spans[0].span_kind, 3)
        self.assertEqual(request.spans[1].span_kind, 2)
        self.assertNotIn(SECRET, json.dumps(MessageToDict(request)))

    def test_default_grpc_export_path_serializes_protobuf_without_network(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                payloads = []
                channel = MagicMock(spec=grpc.Channel)

                def unary_unary(method, request_serializer, response_deserializer, *args, **kwargs):
                    rpc = MagicMock(spec=grpc.UnaryUnaryMultiCallable)

                    def invoke(request, *args, **kwargs):
                        payloads.append(request_serializer(request))
                        if failure:
                            raise RuntimeError(SECRET)
                        call = MagicMock(spec=grpc.Call)
                        call.trailing_metadata.return_value = ()
                        return Empty(), call

                    rpc.with_call.side_effect = invoke
                    return rpc

                channel.unary_unary.side_effect = unary_unary
                with patch("opentelemetry.exporter.cloud_trace.TraceServiceGrpcTransport.create_channel",
                           return_value=channel) as create_channel:
                    delegate = CloudTraceSpanExporter(project_id="offline-trace-project")
                spans = self.spans()
                result = SanitizingExporter(delegate).export(spans)
                self.assertEqual(result, SpanExportResult.FAILURE if failure else SpanExportResult.SUCCESS)
                create_channel.assert_called_once()
                self.assertEqual(len(payloads), 1)
                self.assertNotIn(SECRET.encode(), payloads[0])
                request = BatchWriteSpansRequest.pb(BatchWriteSpansRequest.deserialize(payloads[0]))
                self.assert_wire_spans(spans, request)
                self.assert_quiet_and_offline()

    def test_http_export_failure_does_not_print_server_error_payload(self):
        self.post.return_value = self.response(400)
        result = SanitizingExporter(self.exporter).export(self.spans())
        self.assertEqual(result, SpanExportResult.FAILURE)
        self.request()
        self.assert_quiet_and_offline()

    def test_transport_exception_does_not_print_exception_or_stack(self):
        self.post.side_effect = RuntimeError(SECRET)
        provider = TracerProvider()
        processor = BatchSpanProcessor(SanitizingExporter(self.exporter))
        provider.add_span_processor(processor)
        self.addCleanup(provider.shutdown)
        with provider.get_tracer("offline").start_as_current_span(SECRET) as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
        self.assertTrue(provider.force_flush())
        self.request()
        self.assert_quiet_and_offline()

    def test_retry_diagnostics_remove_sensitive_server_error(self):
        self.post.side_effect = [self.response(503), self.response()]
        logger = logging.getLogger("google.api_core.retry")
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, previous_level)
        with patch("google.api_core.retry.retry_unary.time.sleep"):
            result = SanitizingExporter(self.exporter).export(self.spans())
        self.assertEqual(result, SpanExportResult.SUCCESS)
        self.request(expected_calls=2)
        self.assertIn("Trace exporter diagnostic", self.logs.getvalue())
        self.assertNotIn("Traceback", self.logs.getvalue())
        self.assert_quiet_and_offline()

    def test_wrapper_failure_is_safe_and_other_logs_are_unchanged(self):
        exporter = SanitizingExporter(self.exporter)
        with patch("travel_agent.tracing.sanitize_span", side_effect=RuntimeError(SECRET)):
            self.assertEqual(exporter.export([object()]), SpanExportResult.FAILURE)
        self.post.assert_not_called()
        self.assertIn("Trace export failed", self.logs.getvalue())
        self.assertNotIn("Traceback", self.logs.getvalue())
        self.assert_quiet_and_offline()
        logging.getLogger("google.api_core.retry").warning("ordinary unrelated diagnostic")
        self.assertIn("ordinary unrelated diagnostic", self.logs.getvalue())
