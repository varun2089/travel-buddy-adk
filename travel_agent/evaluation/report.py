import argparse
import csv
from html import escape
import json
from pathlib import Path
import re


PENDING = {"", "pending", "pending human review", "pending_human_review", "not_assessed",
           "not assessed", "not_applicable", "not applicable"}


def cell(value):
    return paragraph(" ".join(str(value).split())).replace("|", "\\|")


def paragraph(value):
    parts = []
    for part in re.split(r"\n\s*\n", str(value).replace("\r", "\n")):
        text = escape(" ".join(part.split()), quote=False)
        text = re.sub(r"([\\`*])", r"\\\1", text)
        text = re.sub(r"(?<!\w)_|_(?!\w)", r"\\_", text)
        text = re.sub(r"\](?=\s*[\[(])", r"\\]", text)
        text = text.replace("![", "\\![")
        text = re.sub(r"^(#{1,6})(?=\s|$)|^([-+])(?=\s|$)|^(-)(?=-{2,}$)|^(~{3,})|^(\[)(?=.*\]:)",
                      lambda match: "\\" + match.group(), text)
        text = re.sub(r"^(\d+)([.)])(?=\s|$)", r"\1\\\2", text)
        parts.append(text)
    return "\n\n".join(parts)


def bullets(values):
    return "\n".join("- " + paragraph(" ".join(str(value).split())) for value in values)


def evidence_quote(value):
    return "\n".join("> " + line if line else ">" for line in paragraph(value).splitlines())


def review_status(record, review):
    verdict = review.get("semantic_correctness", "").strip()
    if verdict.lower() not in PENDING:
        return "Recorded human review: " + verdict
    return "Pending human review" if record["case"].get("human_review_required", True) else "Not required; correctness unassessed"


def render_report(records, reviews=None, execution_kind=None):
    reviews = reviews or {}
    counts = {status: sum(row["execution_status"] == status for row in records)
              for status in ("completed", "service_failure", "timeout", "call_limit")}
    pending = sum(review_status(row, reviews.get(row["case"]["id"], {})) == "Pending human review"
                  for row in records)
    lines = ["# Evaluation report", "",
             "Execution completion, citation validity and human-assessed correctness are separate. "
             "Valid citations do not establish claim support. No automated semantic pass is assigned.", "",
             f"Completed: {counts['completed']}; service failures: {counts['service_failure']}; "
             f"timeouts: {counts['timeout']}; call limits: {counts['call_limit']}; needing human review: {pending}.", ""]
    if execution_kind:
        lines.extend(["Execution kind: " + cell(execution_kind) + ". Offline runs provide harness validation only.", ""])
    lines.extend(["| Case ID | Execution outcome | Expected evidence found | Citation check | Human-review status |",
                  "| --- | --- | --- | --- | --- |"])
    for record in records:
        case = record["case"]
        evidence = record["evidence_check"]
        found = f"{len(evidence['matched_ids'])}/{len(case['expected_evidence_ids'])} ({evidence['status']})"
        citation = record["citation_check"]
        citation_text = f"{citation['status']}; requirement: {citation['requirement_status']}"
        values = [case["id"], record["execution_status"], found, citation_text,
                  review_status(record, reviews.get(case["id"], {}))]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    for record in records:
        case = record["case"]
        review = reviews.get(case["id"], {})
        lines.extend(["", "## " + cell(case["id"]), "",
                      "Execution outcome: " + cell(record["execution_status"]), "",
                      "### Question", "", paragraph(case["question"]), "",
                      "### Actual answer", "", paragraph(record["final_answer"]) if record.get("final_answer")
                      else "No answer recorded" + (" (retrieval-only evaluation)." if record["mode"] == "retrieval" else "."), "",
                      "### Expected behaviour", "", paragraph(case["required_behaviour"]), "",
                      "### Forbidden claims/actions", "", bullets(case["forbidden_claims_or_actions"]), "",
                      "### Retrieved evidence", ""])
        evidence = [item for retrieval in record["retrievals"] for item in retrieval["evidence"]]
        for item in evidence:
            lines.extend(["Evidence ID: " + cell(item["id"]), "", evidence_quote(item["text"]), ""])
        if not evidence:
            lines.extend(["No evidence retrieved.", ""])
        lines.extend(["### Automated flags", "", bullets(record["quality_flags"])
                      if record["quality_flags"] else "None recorded.", "",
                      "### Human review", "", cell(review_status(record, review)), ""])
        for label, field in (("Evidence supports answer", "evidence_support"),
                             ("Semantic correctness", "semantic_correctness"), ("Reviewer notes", "reviewer_notes")):
            value = review.get(field, "").strip()
            lines.extend([label + ":", "", paragraph(value) if value else "Pending human review.", ""])
    return "\n".join(lines) + "\n"


def write_report(output_dir):
    output_dir = Path(output_dir)
    records = [json.loads(line) for line in (output_dir / "results.jsonl").read_text().splitlines() if line.strip()]
    reviews = {}
    review_path = output_dir / "human-review.csv"
    if review_path.exists():
        with review_path.open(newline="") as stream:
            reviews = {row["case_id"]: row for row in csv.DictReader(stream)}
    metadata_path = output_dir / "run.json"
    kind = json.loads(metadata_path.read_text()).get("execution_kind") if metadata_path.exists() else None
    report_path = output_dir / "report.md"
    report_path.write_text(render_report(records, reviews, kind))
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Render saved evaluation results without model or database calls.")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    try:
        write_report(args.output_dir)
    except Exception:
        parser.exit(1, "Report generation failed; check saved evaluation files.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
