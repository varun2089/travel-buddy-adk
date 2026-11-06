import argparse
import asyncio
import csv
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys
from time import perf_counter
from uuid import uuid4

from travel_agent.policy import config
from travel_agent.evaluation.report import write_report


DATASET = Path(__file__).with_name("cases.json")
READ_ONLY_TOOLS = {"search_travel_policy", "get_travel_estimates", "calculate_trip_cost"}
TOKEN_FIELDS = (
    "prompt_token_count", "candidates_token_count", "total_token_count",
    "cached_content_token_count", "thoughts_token_count", "tool_use_prompt_token_count",
)
CITATION = re.compile(r"\[([^\[\]\n]+)\]")


def evidence_id(row):
    return "/".join(config.identifier(row[key]) for key in ("document_id", "version", "section_id"))


def load_cases():
    cases = json.loads(DATASET.read_text())
    seen = set()
    for case in cases:
        config.identifier(case["id"])
        if case["id"] in seen or not isinstance(case["question"], str) or not case["question"].strip():
            raise ValueError("Invalid evaluation case.")
        seen.add(case["id"])
        for item in case["expected_evidence_ids"]:
            parts = item.split("/")
            if len(parts) != 3:
                raise ValueError("Invalid expected evidence ID.")
            for part in parts:
                config.identifier(part)
        if not isinstance(case["human_review_required"], bool):
            raise ValueError("Invalid review requirement.")
        if not isinstance(case["citations_required"], bool):
            raise ValueError("Invalid citation requirement.")
        if not case["required_behaviour"] or not case["forbidden_claims_or_actions"]:
            raise ValueError("Missing evaluation rubric.")
    return cases


def capture_retrieval(response):
    status = response.get("status")
    if status not in {"ok", "no_evidence", "error"}:
        raise ValueError("Invalid retrieval response.")
    evidence = []
    if status == "ok":
        for row in response["evidence"]:
            evidence.append({"id": evidence_id(row), "heading": row["heading"],
                             "text": row["text"], "distance": row["distance"]})
    error_code = response.get("error", {}).get("code")
    if error_code not in {"invalid_query", "policy_embedding_error", "policy_retrieval_error"}:
        error_code = "policy_service_error"
    return {"status": status, "evidence": evidence,
            "error_code": error_code if status == "error" else None}


def assess(case, record):
    answer = record["final_answer"]
    record["final_answer"] = (answer.strip() or None) if isinstance(answer, str) else None
    retrieved = sorted({row["id"] for call in record["retrievals"] for row in call["evidence"]})
    expected = set(case["expected_evidence_ids"])
    matched = sorted(expected.intersection(retrieved))
    citations = sorted(set(CITATION.findall(record["final_answer"] or "")))
    invalid = sorted(set(citations).difference(retrieved))
    citations_required = case.get("citations_required", bool(expected))
    service_failed = record["execution_status"] != "completed" or any(
        call["status"] == "error" for call in record["retrievals"])
    if service_failed and record["execution_status"] == "completed":
        record["execution_status"] = "service_failure"
        record["failure_code"] = next(call["error_code"] for call in record["retrievals"] if call["status"] == "error")
    flags = []
    if record["blocked_tools"]:
        flags.append("attempted_disallowed_tool")
    if invalid:
        flags.append("citation_not_retrieved")
    if record["mode"] == "answer" and not record["final_answer"] and not service_failed:
        flags.append("missing_final_answer")
    if record["mode"] == "answer" and citations_required and not citations and not service_failed:
        flags.append("missing_required_citations")
    if expected.difference(retrieved) and not service_failed:
        flags.append("expected_evidence_missing")
    record.update({
        "retrieved_ids": retrieved,
        "evidence_check": {
            "status": "not_assessed_execution_failure" if service_failed else (
                "not_applicable" if not expected else "matched" if expected.issubset(retrieved) else "missed"),
            "matched_ids": matched, "missing_ids": sorted(expected.difference(retrieved)),
            "recall": len(matched) / len(expected) if expected and not service_failed else None,
        },
        "citation_check": {
            "status": "invalid" if invalid else "valid" if citations else "no_citations",
            "cited_ids": citations, "invalid_ids": invalid,
            "required": citations_required,
            "requirement_status": "not_applicable" if record["mode"] != "answer" else (
                "not_assessed_execution_failure" if service_failed else "not_required" if not citations_required
                else "present" if citations else "missing"),
            "claim_support": "not_assessed_requires_human_review",
        },
        "quality_flags": flags,
        "semantic_correctness": "pending_human_review" if record["mode"] == "answer" else "not_assessed",
    })
    return record


def new_record(case, mode):
    return {"case": case, "mode": mode, "execution_status": "completed", "failure_code": None,
            "retrievals": [], "final_answer": None, "answer_capture_source": None, "latency_seconds": None,
            "blocked_tools": [], "tool_calls": [], "model_request_attempts": 0, "model_responses": 0,
            "model_versions": [], "usage": [], "usage_note": "Embedding usage is not exposed by the search tool."}


def progress(record, started, stage):
    elapsed = perf_counter() - started
    record["last_stage"] = stage
    record["last_stage_elapsed_seconds"] = elapsed
    print(f"case={config.identifier(record['case']['id'])} elapsed={elapsed:.3f}s {stage}",
          file=sys.stderr, flush=True)


def case_finished(record, started):
    return progress(record, started, "Case completed" if record["execution_status"] == "completed"
                    else "Case timed out" if record["execution_status"] == "timeout" else "Case failed")


def retrieval_case(case):
    from travel_agent.tools.search_travel_policy import search_travel_policy

    record = new_record(case, "retrieval")
    started = perf_counter()
    progress(record, started, "Case started")
    try:
        progress(record, started, "Tool started")
        record["retrievals"].append(capture_retrieval(search_travel_policy(case["question"])))
        progress(record, started, "Tool completed")
    except Exception:
        record.update(execution_status="service_failure", failure_code="retrieval_execution_error")
    record["latency_seconds"] = perf_counter() - started
    record = assess(case, record)
    case_finished(record, started)
    return record


def answer_text(response):
    if response.partial or response.interrupted or response.error_code:
        return None
    content = response.content
    if content is None or content.role not in (None, "model"):
        return None
    parts = content.parts or []
    if any(part.function_call or part.function_response or part.executable_code or part.code_execution_result
           for part in parts):
        return None
    return "".join(part.text for part in parts if part.text and not part.thought).strip() or None


async def answer_case(case, timeout_seconds):
    from google.adk.agents.invocation_context import LlmCallsLimitExceededError
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from travel_agent.agent import root_agent

    record = new_record(case, "answer")
    started = perf_counter()
    progress(record, started, "Case started")
    model_answer = None

    def before_tool(tool, args, tool_context):
        progress(record, started, "Tool started")
        record["tool_calls"].append(tool.name)
        if tool.name not in READ_ONLY_TOOLS:
            record["blocked_tools"].append(tool.name)
            return {"status": "error", "error": {"code": "evaluation_read_only",
                    "message": "This evaluation permits read-only tools only."}, "retryable": False}
        return None

    def after_tool(tool, args, tool_context, tool_response):
        if tool.name == "search_travel_policy":
            record["retrievals"].append(capture_retrieval(tool_response))
        elif tool.name in READ_ONLY_TOOLS and tool_response.get("status") == "error":
            record.update(execution_status="service_failure", failure_code="read_only_tool_error")
        progress(record, started, "Tool completed")
        return None

    def before_model(callback_context, llm_request):
        record["model_request_attempts"] += 1
        progress(record, started, "Model request started")
        return None

    def after_model(callback_context, llm_response):
        nonlocal model_answer
        if not llm_response.partial:
            record["model_responses"] += 1
            model_answer = answer_text(llm_response)
            progress(record, started, "Model request completed")
        if llm_response.error_code:
            record.update(execution_status="service_failure", failure_code="model_response_error")
        if llm_response.model_version and llm_response.model_version not in record["model_versions"]:
            record["model_versions"].append(llm_response.model_version)
        if llm_response.usage_metadata is not None:
            record["usage"].append({field: getattr(llm_response.usage_metadata, field)
                                    for field in TOKEN_FIELDS
                                    if getattr(llm_response.usage_metadata, field, None) is not None})
        return None

    agent = root_agent.clone(update={
        "before_tool_callback": [before_tool, *root_agent.canonical_before_tool_callbacks],
        "after_tool_callback": [after_tool, *root_agent.canonical_after_tool_callbacks],
        "before_model_callback": [before_model, *root_agent.canonical_before_model_callbacks],
        "after_model_callback": [after_model, *root_agent.canonical_after_model_callbacks],
    })
    runner = None

    async def consume():
        nonlocal runner
        runner = InMemoryRunner(agent=agent, app_name="travel_policy_evaluation")
        session = await runner.session_service.create_session(
            app_name="travel_policy_evaluation", user_id="evaluation")
        record["session_id"] = session.id
        async for event in runner.run_async(
            user_id="evaluation", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=case["question"])]),
            run_config=RunConfig(max_llm_calls=5),
        ):
            if event.error_code:
                record.update(execution_status="service_failure", failure_code="agent_event_error")
            if event.is_final_response() and event.content and event.author == agent.name:
                text = answer_text(event)
                if text:
                    record["final_answer"] = text
                    record["answer_capture_source"] = "final_event"

    try:
        await asyncio.wait_for(consume(), timeout=timeout_seconds)
        if record["execution_status"] == "completed" and record["final_answer"] is None and model_answer:
            record["final_answer"] = model_answer
            record["answer_capture_source"] = "model_response"
    except LlmCallsLimitExceededError:
        record.update(execution_status="call_limit", failure_code="five_model_call_limit")
    except TimeoutError:
        record["timeout_stage"] = record["last_stage"]
        record.update(execution_status="timeout", failure_code="case_timeout")
    except Exception:
        record.update(execution_status="service_failure", failure_code="agent_execution_error")
    finally:
        if runner is not None:
            try:
                await runner.close()
            except Exception:
                record.update(execution_status="service_failure", failure_code="runner_cleanup_error")
        record["latency_seconds"] = perf_counter() - started
    record = assess(case, record)
    case_finished(record, started)
    return record


def configuration(mode):
    details = {"mode": mode, "embedding_model": config.MODEL, "dimensions": config.DIMENSIONS,
               "top_k": config.TOP_K, "max_distance": config.MAX_DISTANCE,
               "active_policy_selection": "PostgreSQL registry; observed pairs recorded per case",
               "packages": {name: version(name) for name in ("google-adk", "google-genai")},
               "max_llm_calls": 5 if mode == "answer" else 0}
    if mode == "answer":
        from travel_agent.agent import root_agent

        details.update(model=root_agent.model if isinstance(root_agent.model, str) else root_agent.model.model,
                       instruction_sha256=sha256(root_agent.instruction.encode()).hexdigest(),
                       read_only_tool_allowlist=sorted(READ_ONLY_TOOLS))
        generation = root_agent.generate_content_config
        details["generation"] = {
            field: getattr(generation, field) for field in (
                "temperature", "top_p", "top_k", "max_output_tokens", "seed")
            if generation is not None and getattr(generation, field, None) is not None}
    return details


def summary(records):
    eligible = [row for row in records if row["evidence_check"]["recall"] is not None]
    return {"cases": len(records),
            "execution_failures": sum(row["execution_status"] != "completed" for row in records),
            "execution_status_counts": {status: sum(row["execution_status"] == status for row in records)
                                        for status in ("completed", "service_failure", "call_limit", "timeout")},
            "evidence_scored_cases": len(eligible),
            "all_expected_evidence_rate": sum(row["evidence_check"]["status"] == "matched"
                                               for row in eligible) / len(eligible) if eligible else None,
            "mean_expected_evidence_recall": sum(row["evidence_check"]["recall"] for row in eligible)
                                              / len(eligible) if eligible else None,
            "quality_flagged_cases": sum(bool(row["quality_flags"]) for row in records),
            "missing_final_answer_cases": sum("missing_final_answer" in row["quality_flags"] for row in records),
            "missing_required_citation_cases": sum("missing_required_citations" in row["quality_flags"] for row in records),
            "invalid_citation_cases": sum(row["citation_check"]["status"] == "invalid" for row in records),
            "valid_citation_cases": sum(row["citation_check"]["status"] == "valid" for row in records),
            "no_citation_cases": sum(row["citation_check"]["status"] == "no_citations" for row in records),
            "semantic_pass_rate": None,
            "semantic_note": "No automated semantic verdict. Citation validity does not establish claim support."}


async def evaluate(mode, cases, output_dir, timeout_seconds=120, execution_kind="offline_test"):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    metadata = {"execution_kind": execution_kind, "started_at": datetime.now(timezone.utc).isoformat(),
                "dataset_sha256": sha256(DATASET.read_bytes()).hexdigest(),
                "configuration": configuration(mode), "timeout_seconds": timeout_seconds,
                "quality_evidence": "unreviewed live outputs" if execution_kind == "live_opt_in"
                                    else "offline harness validation only; no real-model quality evidence"}
    (output_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
    records = []
    fields = ["case_id", "question", "execution_status", "expected_ids", "retrieved_ids", "retrieved_evidence", "final_answer",
              "required_behaviour", "forbidden_claims_or_actions", "human_review_required",
              "citation_validity", "citation_requirement", "quality_flags", "evidence_support", "semantic_correctness", "reviewer_notes"]
    with (output_dir / "results.jsonl").open("w") as results, (output_dir / "human-review.csv").open("w", newline="") as review:
        writer = csv.DictWriter(review, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            record = retrieval_case(case) if mode == "retrieval" else await answer_case(case, timeout_seconds)
            record["execution_kind"] = execution_kind
            record["configuration"] = metadata["configuration"]
            results.write(json.dumps(record, allow_nan=False) + "\n")
            results.flush()
            writer.writerow({"case_id": case["id"], "question": case["question"],
                             "execution_status": record["execution_status"],
                             "expected_ids": "; ".join(case["expected_evidence_ids"]),
                             "retrieved_ids": "; ".join(record["retrieved_ids"]),
                             "retrieved_evidence": json.dumps([row for call in record["retrievals"] for row in call["evidence"]]),
                             "final_answer": record["final_answer"],
                             "required_behaviour": case["required_behaviour"],
                             "forbidden_claims_or_actions": "; ".join(case["forbidden_claims_or_actions"]),
                             "human_review_required": case["human_review_required"],
                             "citation_validity": record["citation_check"]["status"],
                             "citation_requirement": record["citation_check"]["requirement_status"],
                             "quality_flags": "; ".join(record["quality_flags"]),
                             "evidence_support": "pending human review", "semantic_correctness": "pending human review",
                             "reviewer_notes": ""})
            review.flush()
            records.append(record)
    report = summary(records)
    (output_dir / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    write_report(output_dir)
    return report


def main():
    parser = argparse.ArgumentParser(description="Opt-in live policy evaluation; may incur model charges.")
    parser.add_argument("mode", choices=("retrieval", "answer"))
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.limit < 1 or not 0 < args.timeout_seconds <= 600:
        parser.error("Use a positive limit and a timeout in (0, 600].")
    cases = load_cases()
    if args.case_ids:
        unknown = set(args.case_ids).difference(case["id"] for case in cases)
        if unknown:
            parser.error("Unknown evaluation case ID.")
        cases = [case for case in cases if case["id"] in args.case_ids]
    cases = cases[:args.limit]
    output_dir = args.output_dir or Path("evaluations/results") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.mode + "-" + uuid4().hex[:8])
    try:
        report = asyncio.run(evaluate(args.mode, cases, output_dir, args.timeout_seconds,
                                      execution_kind="live_opt_in"))
    except FileExistsError:
        parser.error("Output directory already exists; choose a new directory.")
    except Exception:
        print(json.dumps({"status": "error", "message": "Evaluation setup or export failed; check local configuration."}))
        return 1
    print(json.dumps({"output_dir": str(output_dir), **report}, indent=2))
    return 1 if report["execution_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
