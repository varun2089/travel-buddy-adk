import unittest
from unittest.mock import patch

from google.adk.agents import Agent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from travel_agent.agent import root_agent
from travel_agent.tools.travel_estimates import get_travel_estimates


INPUTS = dict(destination="Sydney", departure_date="2026-11-16",
              return_date="2026-11-18", purpose="Conference",
              proposed_budget_aud=1500, registration_fee_aud=0)


class FakeModel(BaseLlm):
    model: str = "offline-fake"
    calls: int = 0
    requests: list = Field(default_factory=list)
    batches: list = Field(default_factory=list)
    repeat: bool = False
    tool_name: str = "prepare_travel_proposal"

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        self.requests.append(llm_request.model_copy(deep=True))
        if self.repeat or self.calls <= len(self.batches):
            batch = [INPUTS] if self.repeat else self.batches[self.calls - 1]
            parts = [types.Part(function_call=types.FunctionCall(
                name=self.tool_name, args=args)) for args in batch]
        else:
            parts = [types.Part(text="Finished")]
        yield LlmResponse(content=types.Content(role="model", parts=parts))


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from travel_agent.request_context import PreparationRequest, preparation_request
        token = preparation_request.set(PreparationRequest("offline", "request-1"))
        self.addCleanup(preparation_request.reset, token)

    async def make_runner(self, model, tools=None):
        agent = Agent(name="offline_agent", model=model,
                      tools=root_agent.tools if tools is None else tools,
                      before_tool_callback=root_agent.before_tool_callback,
                      on_tool_error_callback=root_agent.on_tool_error_callback)
        runner = InMemoryRunner(agent=agent, app_name="offline_tests")
        session = await runner.session_service.create_session(
            app_name="offline_tests", user_id="test_user")
        self.addAsyncCleanup(runner.close)
        return runner, session

    async def run_agent(self, runner, session):
        return [event async for event in runner.run_async(
            user_id="test_user", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="Save proposal")]),
            run_config=RunConfig(max_llm_calls=5))]

    def responses(self, events):
        return [part.function_response.response for event in events if event.content
                for part in event.content.parts or [] if part.function_response]

    async def test_limit_stops_before_sixth_fake_model_call(self):
        model = FakeModel(repeat=True)
        runner, session = await self.make_runner(model)
        with patch("travel_agent.db.session.create_session_factory", side_effect=ValueError("private")) as factory:
            with self.assertRaises(LlmCallsLimitExceededError):
                await self.run_agent(runner, session)
        self.assertEqual(model.calls, 5)
        factory.assert_called_once()

    async def test_repeated_calls_blocked_after_success_failure_and_validation(self):
        outcomes = [{"proposal_id": "saved-id", "status": "NEEDS_REVIEW"},
                    {"status": "error", "error": {"code": "invalid_input"}}]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                model = FakeModel(batches=[[INPUTS, dict(INPUTS, purpose="Changed")], [INPUTS]])
                runner, session = await self.make_runner(model)
                with patch("travel_agent.db.session.create_session_factory") as factory, patch(
                    "travel_agent.services.proposals.prepare", return_value=outcome) as prepare:
                    events = await self.run_agent(runner, session)
                    prepare.assert_called_once()
                    responses = self.responses(events)
                    self.assertEqual(responses[0], outcome)
                    self.assertEqual([r["error"]["code"] for r in responses[1:]],
                                     ["proposal_attempt_already_made"] * 2)
                    model.calls = 0
                    await self.run_agent(runner, session)
                    self.assertEqual(prepare.call_count, 2)

    async def test_unexpected_failure_is_safe_and_not_retried(self):
        model = FakeModel(batches=[[INPUTS], [INPUTS]])
        runner, session = await self.make_runner(model)
        with patch("travel_agent.db.session.create_session_factory"), patch(
            "travel_agent.services.proposals.prepare", side_effect=RuntimeError("private-secret")) as prepare:
            events = await self.run_agent(runner, session)
        responses = self.responses(events)
        self.assertEqual(responses[0]["error"]["code"], "tool_execution_error")
        self.assertFalse(responses[0]["retryable"])
        self.assertNotIn("private-secret", str(responses))
        self.assertEqual(responses[1]["error"]["code"], "proposal_attempt_already_made")
        prepare.assert_called_once()
        seen = [p.function_response.response for request in model.requests
                for content in request.contents for p in content.parts or [] if p.function_response]
        self.assertIn(responses[0], seen)

    async def test_uncertain_storage_outcome_is_attempted_once(self):
        model = FakeModel(batches=[[INPUTS], [INPUTS]])
        runner, session = await self.make_runner(model)
        with patch("travel_agent.db.session.create_session_factory"), patch(
            "travel_agent.services.proposals.prepare", side_effect=SQLAlchemyError("private-secret")) as prepare:
            events = await self.run_agent(runner, session)
        responses = self.responses(events)
        self.assertEqual(responses[0]["error"]["code"], "proposal_storage_error")
        self.assertFalse(responses[0]["retryable"])
        self.assertEqual(responses[1]["error"]["code"], "proposal_attempt_already_made")
        self.assertNotIn("private-secret", str(responses))
        prepare.assert_called_once()

    async def test_other_tools_are_not_blocked_and_validation_reaches_model(self):
        args = {key: value for key, value in INPUTS.items() if key != "purpose"}
        args["departure_date"] = "bad"
        model = FakeModel(tool_name="calculate_trip_cost", batches=[[args], [args]])
        runner, session = await self.make_runner(model)
        events = await self.run_agent(runner, session)
        responses = self.responses(events)
        self.assertEqual([r["error"]["code"] for r in responses], ["invalid_date"] * 2)
        seen = [p.function_response.response for request in model.requests
                for content in request.contents for p in content.parts or [] if p.function_response]
        self.assertIn(responses[0], seen)

    async def test_estimate_exception_uses_safe_error_callback(self):
        model = FakeModel(tool_name="get_travel_estimates", batches=[[{"destination": None}]])
        runner, session = await self.make_runner(model, tools=[get_travel_estimates])
        with patch.object(FunctionTool, "run_async", side_effect=RuntimeError("private-secret")) as execute:
            responses = self.responses(await self.run_agent(runner, session))
        execute.assert_awaited_once()
        self.assertEqual(responses[0]["error"]["code"], "tool_execution_error")
