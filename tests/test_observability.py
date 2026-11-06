import asyncio
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google.adk.cli.api_server import ApiServer
from google.adk.runners import InMemoryRunner
from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from travel_agent.agent import root_agent
from travel_agent.observability import ObservabilityPlugin
from test_agent_runtime import FakeModel, INPUTS


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from travel_agent.request_context import PreparationRequest, preparation_request
        token = preparation_request.set(PreparationRequest("offline", "request-1"))
        self.addCleanup(preparation_request.reset, token)
        self.plugin = ObservabilityPlugin()
        self.output = io.StringIO()
        self.capture = redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def rows(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    async def run_model(self, model, limit=5):
        agent = root_agent.clone(update={"model": model})
        runner = InMemoryRunner(app=App(name="offline", root_agent=agent, plugins=[self.plugin]))
        self.addAsyncCleanup(runner.close)
        session = await runner.session_service.create_session(app_name="offline", user_id="private-person")
        return [event async for event in runner.run_async(
            user_id="private-person", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="private-prompt")]),
            run_config=RunConfig(max_llm_calls=limit))]

    async def test_real_runner_preserves_guard_and_safe_error_callback(self):
        model = FakeModel(batches=[[INPUTS, INPUTS], [INPUTS]])
        with patch("travel_agent.db.session.create_session_factory", side_effect=RuntimeError("private-secret")) as factory:
            events = await self.run_model(model)
        factory.assert_called_once()
        responses = [part.function_response.response for event in events if event.content
                     for part in event.content.parts or [] if part.function_response]
        self.assertEqual(responses[0]["error"]["code"], "tool_execution_error")
        self.assertEqual([row["outcome"] for row in self.rows() if row["event"].startswith("tool.")
                          and row["event"] != "tool.start"], ["exception", "blocked", "blocked"])
        self.assertEqual(responses[1]["error"]["code"], "proposal_attempt_already_made")
        self.assertEqual(self.rows()[-1]["event"], "invocation.failure")
        self.assert_private()

    def assert_private(self):
        logged = self.output.getvalue()
        for value in ("private", "Sydney", "Conference", "Finished", "1500", "evidence-text", "unknown-name"):
            self.assertNotIn(value, logged)
        for row in self.rows():
            self.assertRegex(row["invocation_id"], r"^[a-f0-9]{32}$")
            self.assertRegex(row["operation_id"], r"^[a-f0-9]{32}$")
            self.assertGreaterEqual(row["elapsed_ms"], 0)

    async def test_success_and_registration(self):
        server = object.__new__(ApiServer)
        server.extra_plugins = ["travel_agent.observability.ObservabilityPlugin"]
        loaded = server._instantiate_extra_plugins()
        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0], ObservabilityPlugin)
        await self.run_model(FakeModel())
        self.assertEqual([row["event"] for row in self.rows()],
                         ["invocation.start", "model.start", "model.completion", "invocation.completion"])
        self.assert_private()

    async def test_retrieval_and_returned_errors_concurrently(self):
        async def operation(index, status, name):
            run = SimpleNamespace(invocation_id=f"private-run-{index}")
            context = SimpleNamespace(invocation_id=run.invocation_id)
            tool = SimpleNamespace(name=name)
            await self.plugin.before_run_callback(invocation_context=run)
            await self.plugin.before_tool_callback(tool=tool, tool_args={"private": "arguments"}, tool_context=context)
            await asyncio.sleep(0)
            result = {"status": status, "evidence": "evidence-text", "error": {"message": "private-secret"}}
            await self.plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=context, result=result)
            await self.plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=context, result=result)
            await self.plugin.after_run_callback(invocation_context=run)
            await self.plugin.after_run_callback(invocation_context=run)
        await asyncio.gather(*(operation(i, status, name) for i, (status, name) in enumerate([
            ("ok", "search_travel_policy"), ("no_evidence", "search_travel_policy"),
            ("error", "search_travel_policy"), ("error", "calculate_trip_cost"),
            ("success", "unknown-name")])))
        terminal = [row for row in self.rows() if row["event"] in {"tool.completion", "tool.failure"}]
        self.assertEqual([row["outcome"] for row in terminal], ["ok", "no_evidence", "error", "returned_error", "ok"])
        self.assertEqual(len({row["invocation_id"] for row in terminal}), 5)
        self.assertEqual(len(self.rows()), 20)
        self.assert_private()

    async def test_parallel_tools_and_models_same_invocation_tokens_and_duplicates(self):
        run = SimpleNamespace(invocation_id="private-run")
        await self.plugin.before_run_callback(invocation_context=run)
        contexts = [SimpleNamespace(invocation_id=run.invocation_id, actions=object()) for _ in range(2)]
        tool = SimpleNamespace(name="calculate_trip_cost")
        for context in contexts:
            await self.plugin.before_model_callback(callback_context=context, llm_request=None)
            await self.plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=context)
        for context in reversed(contexts):
            await self.plugin.after_model_callback(callback_context=context, llm_response=LlmResponse(partial=True))
            response = LlmResponse(usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=7, candidates_token_count=2, total_token_count=9))
            await self.plugin.after_model_callback(callback_context=context, llm_response=response)
            await self.plugin.after_model_callback(callback_context=context, llm_response=response)
            await self.plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=context, result={"status": "success"})
        await self.plugin.after_run_callback(invocation_context=run)
        rows = self.rows()
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({row["operation_id"] for row in rows}), 5)
        self.assertEqual([row["usage"]["total_token_count"] for row in rows if "usage" in row], [9, 9])
        self.assertFalse(self.plugin._runs)

    async def test_run_failure_closes_unfinished_operations_without_masking_exception(self):
        model = FakeModel(repeat=True)
        with patch("travel_agent.db.session.create_session_factory", side_effect=ValueError("private-secret")):
            from google.adk.agents.invocation_context import LlmCallsLimitExceededError
            with self.assertRaises(LlmCallsLimitExceededError):
                await self.run_model(model, limit=1)
        self.assertEqual(self.rows()[-1]["event"], "invocation.failure")
        self.assertFalse(self.plugin._runs)
        self.assert_private()

    async def test_model_exception_response_error_and_output_failure(self):
        for outcome in ("exception", "error"):
            run = SimpleNamespace(invocation_id="private-run")
            context = SimpleNamespace(invocation_id=run.invocation_id, actions=object())
            await self.plugin.before_run_callback(invocation_context=run)
            await self.plugin.before_model_callback(callback_context=context, llm_request=None)
            if outcome == "exception":
                self.assertIsNone(await self.plugin.on_model_error_callback(
                    callback_context=context, llm_request=None, error=RuntimeError("private-secret")))
            else:
                await self.plugin.after_model_callback(callback_context=context, llm_response=LlmResponse(error_code="private-secret"))
            await self.plugin.on_run_error_callback(invocation_context=run, error=RuntimeError("private-secret"))
            await self.plugin.after_run_callback(invocation_context=run)
        self.assertEqual([r["outcome"] for r in self.rows() if r["event"] == "model.failure"], ["exception", "error"])
        with patch("sys.stdout.write", side_effect=OSError("private-secret")):
            self.assertIsNone(await self.plugin.before_run_callback(invocation_context=run))
            self.assertIsNone(await self.plugin.after_run_callback(invocation_context=run))
        self.assert_private()

    async def test_real_model_exception_is_preserved(self):
        class FailingModel(FakeModel):
            async def generate_content_async(self, llm_request, stream=False):
                raise failure
                yield

        failure = RuntimeError("offline-model-failure")
        with self.assertRaises(RuntimeError) as caught:
            await self.run_model(FailingModel())
        self.assertIs(caught.exception, failure)
        self.assertEqual([row["event"] for row in self.rows()],
                         ["invocation.start", "model.start", "model.failure", "invocation.failure"])
        self.assertNotIn("offline-model-failure", self.output.getvalue())

    async def test_run_error_seals_pending_operations_once(self):
        run = SimpleNamespace(invocation_id="private-run")
        context = SimpleNamespace(invocation_id=run.invocation_id, actions=object())
        await self.plugin.before_run_callback(invocation_context=run)
        await self.plugin.before_model_callback(callback_context=context, llm_request=None)
        await self.plugin.before_tool_callback(tool=SimpleNamespace(name="prepare_travel_proposal"),
                                               tool_args={}, tool_context=context)
        await self.plugin.on_run_error_callback(invocation_context=run, error=RuntimeError("private-secret"))
        await self.plugin.on_run_error_callback(invocation_context=run, error=RuntimeError("private-secret"))
        self.assertEqual([row["outcome"] for row in self.rows()],
                         ["started", "started", "started", "incomplete", "incomplete", "error"])
        self.assertFalse(self.plugin._runs)
        self.assert_private()

    async def test_real_retrieval_returned_outcomes(self):
        for status in ("ok", "no_evidence", "error"):
            self.output.seek(0)
            self.output.truncate()
            with patch("google.adk.tools.function_tool.FunctionTool.run_async",
                       return_value={"status": status, "evidence": "evidence-text"}):
                await self.run_model(FakeModel(tool_name="search_travel_policy", batches=[[{"query": "private-query"}]]))
            terminals = [row for row in self.rows() if row["event"] in {"tool.completion", "tool.failure"}]
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0]["outcome"], status)
            self.assert_private()
