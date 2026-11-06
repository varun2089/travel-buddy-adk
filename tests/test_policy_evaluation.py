import asyncio
import csv
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

from travel_agent.agent import root_agent
from travel_agent.evaluation.run import (
    answer_case, answer_text, assess, capture_retrieval, evaluate, load_cases, main, new_record,
    progress, retrieval_case, summary,
)
from travel_agent.policy.chunking import chunk_markdown
from travel_agent.tools.search_travel_policy import search_travel_policy


ROW = {"document_id": "SYN-TRAVEL", "version": "v1", "section_id": "REVIEW-01",
       "heading": "Human review", "text": "Human review required.", "distance": 0.1}
RESPONSE = {"status": "ok", "evidence": [ROW]}
ANSWER = "A saved proposal awaits human review. [SYN-TRAVEL/v1/REVIEW-01]"


class EvaluationFakeModel(BaseLlm):
    model: str = "offline-evaluation-fake"
    calls: int = 0
    tool_name: str = "search_travel_policy"
    tool_args: dict = Field(default_factory=lambda: {"query": "Does a saved proposal need review?"})
    answer: str = ANSWER
    repeat: bool = False
    no_tool: bool = False
    explode: bool = False
    delay: float = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.explode:
            raise RuntimeError("credential-must-not-be-exported")
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.no_tool and (self.repeat or self.calls % 2 == 1):
            parts = [types.Part(function_call=types.FunctionCall(name=self.tool_name, args=self.tool_args))]
        else:
            parts = [types.Part(text=self.answer)]
        yield LlmResponse(content=types.Content(role="model", parts=parts), model_version="offline-fake-v1",
                          usage_metadata=types.GenerateContentResponseUsageMetadata(
                              prompt_token_count=5, candidates_token_count=3, total_token_count=8))


class EvaluationOfflineTests(unittest.TestCase):
    def setUp(self):
        self.case = load_cases()[0]

    def test_progress_is_flushed_to_stderr_with_only_case_stage_and_elapsed(self):
        record = new_record(dict(self.case, question="private-question"), "answer")
        with patch("travel_agent.evaluation.run.perf_counter", return_value=12.5), patch(
            "travel_agent.evaluation.run.print") as output:
            progress(record, 10, "Case started")
        output.assert_called_once_with(f"case={self.case['id']} elapsed=2.500s Case started",
                                       file=sys.stderr, flush=True)
        self.assertEqual(record["last_stage_elapsed_seconds"], 2.5)

    def test_retrieval_progress_reports_success_and_failure_without_payloads(self):
        for response, terminal in ((RESPONSE, "Case completed"),
                                   (RuntimeError("private-credential"), "Case failed")):
            with patch("travel_agent.tools.search_travel_policy.search_travel_policy",
                       side_effect=[response]), patch("sys.stderr", new_callable=io.StringIO) as output:
                record = retrieval_case(self.case)
            stages = [line.split("s ", 1)[1] for line in output.getvalue().splitlines()]
            expected = ["Case started", "Tool started"]
            if terminal == "Case completed":
                expected.append("Tool completed")
            self.assertEqual(stages, [*expected, terminal])
            self.assertEqual(record["last_stage"], terminal)
            self.assertNotIn("private-credential", output.getvalue())

    def test_dataset_is_grounded_in_existing_sections(self):
        cases = load_cases()
        sections = chunk_markdown(Path("policies/synthetic_travel.md").read_text())
        ids = {f"{row.document_id}/{row.version}/{row.section_id}" for row in sections}
        self.assertTrue(10 <= len(cases) <= 15)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        for case in cases:
            self.assertTrue(set(case["expected_evidence_ids"]).issubset(ids))
            self.assertTrue(case["required_behaviour"])
            self.assertTrue(case["forbidden_claims_or_actions"])
            self.assertTrue(case["human_review_required"])
            self.assertTrue(case["citations_required"])
            self.assertLessEqual(len(case["question"].encode()), 1800)

    def test_real_search_entrypoint_is_called_but_mocked_offline(self):
        with patch("travel_agent.tools.search_travel_policy.search_travel_policy", return_value=RESPONSE) as search:
            record = retrieval_case(self.case)
        search.assert_called_once_with(self.case["question"])
        self.assertEqual(record["evidence_check"]["recall"], 1)
        self.assertGreaterEqual(record["latency_seconds"], 0)
        self.assertEqual(record["semantic_correctness"], "not_assessed")

    def test_wrong_document_or_version_is_not_a_hit(self):
        for updates in ({"version": "v2"}, {"document_id": "OTHER"}):
            with patch("travel_agent.tools.search_travel_policy.search_travel_policy", return_value={
                "status": "ok", "evidence": [dict(ROW, **updates)]}):
                record = retrieval_case(self.case)
            self.assertEqual(record["evidence_check"]["status"], "missed")
            self.assertEqual(record["evidence_check"]["recall"], 0)

    def test_partial_recall_and_service_failures_have_separate_denominators(self):
        multi = dict(self.case, expected_evidence_ids=["SYN-TRAVEL/v1/REVIEW-01", "SYN-TRAVEL/v1/SCOPE-01"])
        with patch("travel_agent.tools.search_travel_policy.search_travel_policy", return_value=RESPONSE):
            partial = retrieval_case(multi)
        with patch("travel_agent.tools.search_travel_policy.search_travel_policy", return_value={
            "status": "error", "error": {"message": "credential-must-not-be-exported"}}):
            failed = retrieval_case(self.case)
        report = summary([partial, failed])
        self.assertEqual(partial["evidence_check"]["recall"], 0.5)
        self.assertEqual(failed["execution_status"], "service_failure")
        self.assertIsNone(failed["evidence_check"]["recall"])
        self.assertEqual(failed["quality_flags"], [])
        self.assertNotIn("credential-must-not-be-exported", json.dumps(failed))
        self.assertEqual(report["execution_failures"], 1)
        self.assertEqual(report["evidence_scored_cases"], 1)
        self.assertEqual(report["mean_expected_evidence_recall"], 0.5)
        self.assertIsNone(report["semantic_pass_rate"])

    def test_no_evidence_is_a_quality_miss_not_a_service_failure(self):
        with patch("travel_agent.tools.search_travel_policy.search_travel_policy",
                   return_value={"status": "no_evidence", "evidence": []}):
            record = retrieval_case(self.case)
        self.assertEqual(record["execution_status"], "completed")
        self.assertEqual(record["evidence_check"]["status"], "missed")

    def test_citation_validity_does_not_score_claim_support(self):
        for answer, status in (("Travel is approved. [SYN-TRAVEL/v1/REVIEW-01]", "valid"),
                               ("[SYN-TRAVEL/v2/REVIEW-01]", "invalid"),
                               ("[REVIEW-01]", "invalid"),
                               ("[SYN-TRAVEL/v1]", "invalid"),
                               ("[OTHER/v1/REVIEW-01]", "invalid"), ("No citations", "no_citations")):
            with self.subTest(answer=answer):
                record = new_record(self.case, "answer")
                record.update(retrievals=[capture_retrieval(RESPONSE)], final_answer=answer)
                result = assess(self.case, record)
                self.assertEqual(result["citation_check"]["status"], status)
                self.assertEqual(result["semantic_correctness"], "pending_human_review")
                self.assertEqual(result["citation_check"]["claim_support"], "not_assessed_requires_human_review")

    def test_missing_final_answer_is_flagged(self):
        for answer in (None, "", " \n\t "):
            with self.subTest(answer=answer):
                record = new_record(self.case, "answer")
                record.update(retrievals=[capture_retrieval(RESPONSE)], final_answer=answer)
                result = assess(self.case, record)
                self.assertIsNone(result["final_answer"])
                self.assertEqual(result["execution_status"], "completed")
                self.assertEqual(result["evidence_check"]["status"], "matched")
                self.assertEqual(result["quality_flags"], ["missing_final_answer", "missing_required_citations"])

    def test_required_missing_citations_are_flagged_independently_of_evidence_hits(self):
        record = new_record(self.case, "answer")
        record.update(retrievals=[capture_retrieval(RESPONSE)], final_answer="A saved proposal awaits human review.")
        result = assess(self.case, record)
        self.assertEqual(result["quality_flags"], ["missing_required_citations"])
        self.assertEqual(result["citation_check"]["requirement_status"], "missing")
        self.assertEqual(summary([result])["missing_required_citation_cases"], 1)
        self.assertEqual(summary([result])["execution_failures"], 0)
        self.assertEqual(result["semantic_correctness"], "pending_human_review")

    def test_optional_citations_and_retrieval_only_cases_are_not_flagged(self):
        case = dict(self.case, citations_required=False)
        record = new_record(case, "answer")
        record.update(retrievals=[capture_retrieval(RESPONSE)], final_answer="A saved proposal awaits human review.")
        result = assess(case, record)
        self.assertEqual(result["quality_flags"], [])
        self.assertEqual(result["citation_check"]["requirement_status"], "not_required")
        record = new_record(self.case, "retrieval")
        record["retrievals"] = [capture_retrieval(RESPONSE)]
        result = assess(self.case, record)
        self.assertEqual(result["quality_flags"], [])
        self.assertEqual(result["citation_check"]["requirement_status"], "not_applicable")

    def test_missing_answers_on_execution_failures_are_not_quality_failures(self):
        for status in ("service_failure", "timeout", "call_limit"):
            record = new_record(self.case, "answer")
            record.update(execution_status=status, retrievals=[capture_retrieval(RESPONSE)])
            result = assess(self.case, record)
            self.assertEqual(result["quality_flags"], [])
            self.assertEqual(result["citation_check"]["requirement_status"], "not_assessed_execution_failure")
            self.assertEqual(summary([result])["missing_final_answer_cases"], 0)
            self.assertEqual(summary([result])["execution_failures"], 1)

    def test_answer_text_rejects_thoughts_tools_partial_and_interrupted_responses(self):
        content = types.Content(role="model", parts=[types.Part(text=ANSWER)])
        self.assertEqual(answer_text(LlmResponse(content=content)), ANSWER)
        for response in (
            LlmResponse(content=content, partial=True),
            LlmResponse(content=content, interrupted=True),
            LlmResponse(content=content, error_code="ERROR"),
            LlmResponse(content=types.Content(role="user", parts=[types.Part(text=ANSWER)])),
            LlmResponse(content=types.Content(role="model", parts=[types.Part(text=ANSWER, thought=True)])),
            LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Searching"),
                        types.Part(function_call=types.FunctionCall(name="search_travel_policy", args={}))])),
            LlmResponse(content=types.Content(role="tool", parts=[types.Part(
                function_response=types.FunctionResponse(name="search_travel_policy", response=RESPONSE))])),
            LlmResponse(content=types.Content(role="model", parts=[types.Part(text=" \n ")])),
            LlmResponse(),
        ):
            with self.subTest(response=response):
                self.assertIsNone(answer_text(response))

    def test_cli_requires_explicit_live_opt_in(self):
        with patch("sys.argv", ["evaluate", "retrieval"]), patch("sys.stderr", new_callable=io.StringIO), patch(
            "travel_agent.evaluation.run.evaluate") as run:
            with self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, 2)
            run.assert_not_called()

    def test_cli_rejects_invalid_selection_before_running(self):
        for arguments in (["--limit", "0"], ["--timeout-seconds", "0"], ["--case", "unknown"]):
            with patch("sys.argv", ["evaluate", "answer", "--live", *arguments]), patch(
                "sys.stderr", new_callable=io.StringIO), patch("travel_agent.evaluation.run.evaluate") as run:
                with self.assertRaises(SystemExit):
                    main()
                run.assert_not_called()

    def test_cli_selects_small_live_run_only_after_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.argv", [
            "evaluate", "answer", "--live", "--case", "receipts", "--limit", "1",
            "--output-dir", str(Path(directory) / "run")]), patch("builtins.print"), patch(
            "travel_agent.evaluation.run.evaluate", new_callable=AsyncMock,
            return_value={"execution_failures": 0}) as run:
            self.assertEqual(main(), 0)
            self.assertEqual(run.call_args.args[0], "answer")
            self.assertEqual([case["id"] for case in run.call_args.args[1]], ["receipts"])
            self.assertEqual(run.call_args.kwargs["execution_kind"], "live_opt_in")


class EvaluationRuntimeOfflineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case = load_cases()[0]

    def agent(self, model, tools=None):
        updates = {"model": model}
        if tools is not None:
            updates["tools"] = tools
        return patch("travel_agent.agent.root_agent", root_agent.clone(update=updates))

    async def test_answer_progress_order_and_payload_redaction(self):
        model = EvaluationFakeModel(tool_name="calculate_trip_cost", tool_args={"private-argument": "secret"})
        with self.agent(model), patch("sys.stderr", new_callable=io.StringIO) as output:
            record = await answer_case(self.case, 10)
        stages = [line.split("s ", 1)[1] for line in output.getvalue().splitlines()
                  if line.startswith("case=")]
        self.assertEqual(stages, ["Case started", "Model request started", "Model request completed",
                                  "Tool started", "Tool completed", "Model request started",
                                  "Model request completed", "Case completed"])
        for value in ("private-argument", "secret", ANSWER, ROW["text"], self.case["question"]):
            self.assertNotIn(value, "\n".join(line for line in output.getvalue().splitlines()
                                            if line.startswith("case=")))
        self.assertEqual(record["model_request_attempts"], 2)

    async def test_timeout_progress_retains_inflight_model_stage(self):
        with self.agent(EvaluationFakeModel(delay=1), tools=[]), patch(
            "sys.stderr", new_callable=io.StringIO) as output:
            record = await answer_case(self.case, 0.01)
        stages = [line.split("s ", 1)[1] for line in output.getvalue().splitlines()
                  if line.startswith("case=")]
        self.assertEqual(stages, ["Case started", "Model request started", "Case timed out"])
        self.assertEqual(record["timeout_stage"], "Model request started")
        self.assertEqual(record["model_request_attempts"], 1)
        self.assertEqual(record["model_responses"], 0)

    async def test_timeout_progress_retains_inflight_tool_stage(self):
        async def delayed_tool(**kwargs):
            await asyncio.sleep(1)

        with self.agent(EvaluationFakeModel(), tools=[search_travel_policy]), patch(
            "google.adk.tools.function_tool.FunctionTool.run_async", side_effect=delayed_tool), patch(
            "sys.stderr", new_callable=io.StringIO) as output:
            record = await answer_case(self.case, 0.05)
        stages = [line.split("s ", 1)[1] for line in output.getvalue().splitlines()
                  if line.startswith("case=")]
        self.assertEqual(stages, ["Case started", "Model request started", "Model request completed",
                                  "Tool started", "Case timed out"])
        self.assertEqual(record["timeout_stage"], "Tool started")
        self.assertEqual(record["tool_calls"], ["search_travel_policy"])
        self.assertEqual(record["retrievals"], [])

    async def test_real_adk_runner_fake_model_fresh_sessions_usage_and_retrieval(self):
        model = EvaluationFakeModel()
        with self.agent(model), patch("travel_agent.tools.search_travel_policy.embed_text", return_value=[1.0]), patch(
            "travel_agent.db.policy_repository.create_policy_session_factory"), patch(
            "travel_agent.db.policy_repository.has_active_policies", return_value=True), patch(
            "travel_agent.db.policy_repository.nearest_sections", return_value=[ROW]):
            first = await answer_case(self.case, 10)
            second = await answer_case(self.case, 10)
        self.assertNotEqual(first["session_id"], second["session_id"])
        for record in (first, second):
            self.assertEqual(record["execution_status"], "completed")
            self.assertEqual(record["final_answer"], ANSWER)
            self.assertEqual(record["answer_capture_source"], "final_event")
            self.assertEqual(record["retrieved_ids"], ["SYN-TRAVEL/v1/REVIEW-01"])
            self.assertEqual(record["model_versions"], ["offline-fake-v1"])
            self.assertEqual(record["model_responses"], 2)
            self.assertEqual(record["usage"][0]["total_token_count"], 8)
            self.assertEqual(record["citation_check"]["status"], "valid")
            self.assertEqual(record["semantic_correctness"], "pending_human_review")

    async def test_complete_model_answer_is_captured_when_final_event_is_not_forwarded(self):
        original_run = InMemoryRunner.run_async

        async def without_final_event(runner, **kwargs):
            async for event in original_run(runner, **kwargs):
                if not event.is_final_response():
                    yield event

        with self.agent(EvaluationFakeModel(no_tool=True)), patch.object(
            InMemoryRunner, "run_async", without_final_event):
            record = await answer_case(self.case, 10)
        self.assertEqual(record["execution_status"], "completed")
        self.assertEqual(record["final_answer"], ANSWER)
        self.assertEqual(record["answer_capture_source"], "model_response")
        self.assertNotIn("missing_final_answer", record["quality_flags"])

    async def test_empty_model_answer_is_exported_as_quality_failure_not_service_failure(self):
        with tempfile.TemporaryDirectory() as directory, self.agent(EvaluationFakeModel(no_tool=True, answer=" \n ")):
            output = Path(directory) / "run"
            report = await evaluate("answer", [self.case], output)
            record = json.loads((output / "results.jsonl").read_text())
            with (output / "human-review.csv").open(newline="") as stream:
                review = next(csv.DictReader(stream))
        self.assertIsNone(record["final_answer"])
        self.assertEqual(report["execution_failures"], 0)
        self.assertEqual(report["missing_final_answer_cases"], 1)
        self.assertEqual(report["missing_required_citation_cases"], 1)
        self.assertIn("missing_final_answer", review["quality_flags"])
        self.assertEqual(review["citation_requirement"], "missing")

    async def test_proposal_preparation_is_blocked_before_any_database_write(self):
        model = EvaluationFakeModel(tool_name="prepare_travel_proposal", tool_args={
            "destination": "Sydney", "departure_date": "2026-11-16", "return_date": "2026-11-18",
            "purpose": "Conference", "proposed_budget_aud": 1500, "registration_fee_aud": 0})
        with self.agent(model), patch("travel_agent.db.session.create_session_factory") as factory, patch(
            "travel_agent.services.proposals.prepare") as prepare:
            record = await answer_case(self.case, 10)
        self.assertEqual(record["blocked_tools"], ["prepare_travel_proposal"])
        self.assertIn("attempted_disallowed_tool", record["quality_flags"])
        self.assertEqual(record["execution_status"], "completed")
        factory.assert_not_called()
        prepare.assert_not_called()

    async def test_call_limit_stops_at_five_fake_model_calls(self):
        model = EvaluationFakeModel(repeat=True, tool_name="prepare_travel_proposal", tool_args={})
        with self.agent(model):
            record = await answer_case(self.case, 10)
        self.assertEqual(model.calls, 5)
        self.assertEqual(record["execution_status"], "call_limit")
        self.assertIsNone(record["evidence_check"]["recall"])

    async def test_model_failure_and_timeout_are_not_quality_misses(self):
        for model, timeout, status in ((EvaluationFakeModel(explode=True), 10, "service_failure"),
                                       (EvaluationFakeModel(delay=1), 0.01, "timeout")):
            with self.agent(model):
                record = await answer_case(self.case, timeout)
            self.assertEqual(record["execution_status"], status)
            self.assertNotIn("credential-must-not-be-exported", json.dumps(record))
            self.assertNotIn("expected_evidence_missing", record["quality_flags"])

    async def test_export_answers_evidence_config_and_pending_review_offline_label(self):
        model = EvaluationFakeModel(no_tool=True, answer="Travel is approved. [SYN-TRAVEL/v1/REVIEW-01]")
        with tempfile.TemporaryDirectory() as directory, self.agent(model):
            output = Path(directory) / "run"
            report = await evaluate("answer", [self.case], output)
            run = json.loads((output / "run.json").read_text())
            result = json.loads((output / "results.jsonl").read_text())
            markdown = (output / "report.md").read_text()
            self.assertIn("offline", markdown)
            self.assertIn(model.answer, markdown)
            self.assertIn("Pending human review", markdown)
            with (output / "human-review.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(run["execution_kind"], "offline_test")
            self.assertIn("no real-model quality evidence", run["quality_evidence"])
            self.assertEqual(run["configuration"]["max_llm_calls"], 5)
            self.assertEqual(result["configuration"]["model"], "offline-evaluation-fake")
            self.assertEqual(result["citation_check"]["status"], "invalid")
            self.assertEqual(rows[0]["final_answer"], model.answer)
            self.assertEqual(rows[0]["semantic_correctness"], "pending human review")
            self.assertIsNone(report["semantic_pass_rate"])
            with self.assertRaises(FileExistsError):
                await evaluate("answer", [self.case], output)

    async def test_export_retrieval_misses_and_failures_without_fake_quality_claims(self):
        cases = load_cases()[:2]
        with tempfile.TemporaryDirectory() as directory, patch(
            "travel_agent.tools.search_travel_policy.search_travel_policy", side_effect=[
                {"status": "no_evidence", "evidence": []}, RuntimeError("credential-must-not-be-exported")]):
            output = Path(directory) / "run"
            report = await evaluate("retrieval", cases, output)
            self.assertEqual(report["execution_failures"], 1)
            self.assertEqual(report["evidence_scored_cases"], 1)
            self.assertEqual(report["all_expected_evidence_rate"], 0)
            for artifact in output.iterdir():
                self.assertNotIn("credential-must-not-be-exported", artifact.read_text())
