import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from travel_agent.evaluation.report import main, paragraph, render_report, write_report
from travel_agent.evaluation.run import assess, capture_retrieval, load_cases, new_record


class EvaluationReportTests(unittest.TestCase):
    def record(self, mode="answer", status="completed"):
        case = load_cases()[0]
        record = new_record(case, mode)
        record.update(execution_status=status, final_answer="Needs human approval. [SYN-TRAVEL/v1/REVIEW-01]")
        record["retrievals"] = [capture_retrieval({"status": "ok", "evidence": [{
            "document_id": "SYN-TRAVEL", "version": "v1", "section_id": "REVIEW-01",
            "heading": "Review", "text": "Human review is required.", "distance": 0.1}]})]
        return assess(case, record)

    def test_summary_table_case_content_and_pending_review_without_mutation(self):
        records = [self.record(status=status) for status in ("completed", "service_failure", "timeout", "call_limit")]
        for index, record in enumerate(records):
            record["case"] = dict(record["case"], id=f"case-{index}")
        original = copy.deepcopy(records)
        report = render_report(records, execution_kind="offline_test")
        self.assertIn("Completed: 1; service failures: 1; timeouts: 1; call limits: 1; needing human review: 4", report)
        self.assertIn("| Case ID | Execution outcome | Expected evidence found | Citation check | Human-review status |", report)
        for record in records:
            for text in (record["case"]["question"], record["final_answer"], record["case"]["required_behaviour"],
                         *record["case"]["forbidden_claims_or_actions"], "Human review is required."):
                self.assertIn(text, report)
        for text in ("Evidence supports answer:", "Semantic correctness:", "Reviewer notes:",
                     "Pending human review", "Valid citations do not establish claim support", "offline"):
            self.assertIn(text, report)
        self.assertIn("1/1", report)
        self.assertEqual(records, original)

    def test_missing_answers_evidence_and_flags_are_explicit(self):
        record = self.record(mode="retrieval")
        record.update(final_answer=None, retrievals=[], quality_flags=["expected_evidence_missing"])
        report = render_report([record])
        self.assertIn("No answer recorded (retrieval-only evaluation).", report)
        self.assertIn("No evidence retrieved.", report)
        self.assertIn("expected_evidence_missing", report)
        record["mode"] = "answer"
        self.assertIn("No answer recorded.", render_report([record]))

    def test_citation_failure_and_unscored_evidence_are_not_reported_as_success(self):
        record = self.record(status="timeout")
        record["citation_check"].update(status="invalid", requirement_status="not_assessed_execution_failure")
        report = render_report([record])
        self.assertIn("not_assessed_execution_failure", report)
        self.assertIn("invalid; requirement:", report)
        self.assertIn("needing human review: 1", report)
        self.assertNotIn("Recorded human review:", report)

    def test_only_recorded_csv_review_supplies_a_human_verdict(self):
        record = self.record()
        record["semantic_correctness"] = "passed"
        report = render_report([record])
        self.assertNotIn("passed", report)
        self.assertIn("needing human review: 1", report)
        reviews = {record["case"]["id"]: {"semantic_correctness": "passed",
                   "evidence_support": "supported", "reviewer_notes": "Reviewed against evidence"}}
        report = render_report([record], reviews)
        self.assertIn("Recorded human review: passed", report)
        self.assertIn("needing human review: 0", report)
        self.assertIn("Reviewed against evidence", report)

    def test_payloads_cannot_break_markdown_structure(self):
        record = self.record()
        record["case"] = dict(record["case"], id="case|<script>")
        record["final_answer"] = "```\n# Fake verdict\n<script>alert(1)</script>"
        report = render_report([record])
        self.assertIn("case\\|&lt;script&gt;", report)
        self.assertNotIn("```", report)
        self.assertNotIn("<script>", report)
        self.assertNotIn("\n# Fake verdict", report)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)

    def test_prose_quotes_and_bullets_keep_ordinary_punctuation_readable(self):
        record = self.record()
        record["case"] = dict(record["case"], question="Is this a draft?\n\nPlease explain.",
                              required_behaviour="Explain draft-status (not approval).",
                              forbidden_claims_or_actions=["Claim approval.", "Book travel."])
        record["retrievals"][0]["evidence"][0]["text"] = "A draft needs review.\n\nDo not book."
        report = render_report([record])
        for text in ("### Question\n\nIs this a draft?\n\nPlease explain.",
                     "### Actual answer\n\n" + record["final_answer"],
                     "### Expected behaviour\n\nExplain draft-status (not approval).",
                     "- Claim approval.\n- Book travel.",
                     "> A draft needs review.\n>\n> Do not book.", "Evidence ID: SYN-TRAVEL/v1/REVIEW-01"):
            self.assertIn(text, report)
        self.assertNotIn("```", report)
        self.assertNotIn("\\-", report)

    def test_untrusted_markdown_cannot_create_active_links_or_structure(self):
        for text, expected in (("[click](javascript:alert(1))", "[click\\](javascript:alert(1))"),
                               ("![tracking](https://example.com)", "\\![tracking\\](https://example.com)"),
                               ("[ref]: javascript:alert(1)", "\\[ref]: javascript:alert(1)"),
                               ("# Fake verdict", "\\# Fake verdict"),
                               ("1. Fake verdict", "1\\. Fake verdict"),
                               ("---", "\\---"),
                               ("<img src=x onerror=alert(1)>", "&lt;img src=x onerror=alert(1)&gt;")):
            with self.subTest(text=text):
                self.assertEqual(paragraph(text), expected)
        self.assertEqual(paragraph("service_failure [SYN-TRAVEL/v1/REVIEW-01]."),
                         "service_failure [SYN-TRAVEL/v1/REVIEW-01].")

    def test_existing_run_cli_preserves_saved_artifacts_and_uses_no_service_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = self.record()
            (output / "results.jsonl").write_text(json.dumps(record) + "\n")
            (output / "run.json").write_text('{"execution_kind":"offline_test"}')
            (output / "summary.json").write_text('{"unchanged":true}')
            with (output / "human-review.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["case_id", "semantic_correctness", "reviewer_notes"])
                writer.writeheader()
                writer.writerow({"case_id": record["case"]["id"], "semantic_correctness": "passed",
                                 "reviewer_notes": "Checked"})
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            code = (
                "import sys; from travel_agent.evaluation.report import main; "
                "assert not any(name in sys.modules for name in "
                "('travel_agent.agent','google.genai','travel_agent.db.policy_repository')); "
                "raise SystemExit(main())"
            )
            result = subprocess.run([sys.executable, "-c", code, directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertIn("Recorded human review: passed", (output / "report.md").read_text())
            write_report(output)
            self.assertEqual(before, {name: (output / name).read_bytes() for name in before})

    def test_cli_failure_does_not_expose_exception_details(self):
        with patch("sys.argv", ["report", "unused"]), patch(
            "travel_agent.evaluation.report.write_report", side_effect=ValueError("private-secret")), patch(
            "sys.stderr", new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 1)
        self.assertNotIn("private-secret", output.getvalue())
