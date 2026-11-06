import json
import sys
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from google.adk.plugins.base_plugin import BasePlugin


TOOLS = frozenset({"search_travel_policy", "prepare_travel_proposal",
                   "calculate_trip_cost", "get_travel_estimates"})
TOKEN_FIELDS = ("prompt_token_count", "candidates_token_count", "total_token_count",
                "cached_content_token_count", "thoughts_token_count", "tool_use_prompt_token_count")


class ObservabilityPlugin(BasePlugin):
    def __init__(self, name="travel_observability"):
        super().__init__(name)
        self._runs = {}

    def _emit(self, run, kind, phase, operation, outcome, **fields):
        try:
            row = {"timestamp": datetime.now(timezone.utc).isoformat(),
                   "severity": "ERROR" if phase == "failure" else "INFO",
                   "event": f"{kind}.{phase}", "invocation_id": run["id"],
                   "operation_id": operation["id"], "outcome": outcome,
                   "elapsed_ms": round((perf_counter() - operation["started"]) * 1000, 3),
                   **fields}
            sys.stdout.write(json.dumps(row, allow_nan=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    async def before_run_callback(self, *, invocation_context):
        key = invocation_context.invocation_id
        if key not in self._runs:
            run = {"id": uuid4().hex, "started": perf_counter(), "operations": {}, "failed": False}
            self._runs[key] = run
            self._emit(run, "invocation", "start", run, "started")

    def _start(self, context, kind, identity, name=None):
        run = self._runs.get(context.invocation_id)
        if run is None:
            return
        key = (kind, id(identity))
        if key not in run["operations"]:
            operation = {"id": uuid4().hex, "started": perf_counter(), "identity": identity,
                         "kind": kind, "done": False, "fields": {"tool": name} if name else {}}
            run["operations"][key] = operation
            self._emit(run, kind, "start", operation, "started", **operation["fields"])

    def _finish(self, context, kind, identity, outcome, **fields):
        run = self._runs.get(context.invocation_id)
        if run is None:
            return
        operation = run["operations"].get((kind, id(identity)))
        if operation is None or operation["done"]:
            return
        operation["done"] = True
        failed = outcome in {"error", "returned_error", "exception", "interrupted", "incomplete"}
        run["failed"] |= failed
        self._emit(run, kind, "failure" if failed else "completion", operation, outcome,
                   **operation["fields"], **fields)

    async def before_model_callback(self, *, callback_context, llm_request):
        self._start(callback_context, "model", callback_context.actions)

    async def after_model_callback(self, *, callback_context, llm_response):
        if llm_response.partial and not llm_response.error_code and not llm_response.interrupted:
            return
        usage = {}
        for field in TOKEN_FIELDS:
            value = getattr(llm_response.usage_metadata, field, None)
            if type(value) is int and value >= 0:
                usage[field] = value
        outcome = "error" if llm_response.error_code else "interrupted" if llm_response.interrupted else "ok"
        self._finish(callback_context, "model", callback_context.actions, outcome, usage=usage)

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        self._finish(callback_context, "model", callback_context.actions, "exception")

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        name = tool.name if tool.name in TOOLS else "other"
        self._start(tool_context, "tool", tool_context, name)

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        status = result.get("status") if isinstance(result, dict) else None
        error = result.get("error") if isinstance(result, dict) else None
        if (tool.name == "prepare_travel_proposal" and isinstance(error, dict)
                and error.get("code") == "proposal_attempt_already_made"):
            outcome = "blocked"
        elif tool.name == "search_travel_policy":
            outcome = status if type(status) is str and status in {"ok", "no_evidence", "error"} else "error"
        else:
            outcome = "returned_error" if status == "error" else "no_data" if status == "no_data" else "ok"
        self._finish(tool_context, "tool", tool_context, outcome)

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        self._finish(tool_context, "tool", tool_context, "exception")

    async def on_event_callback(self, *, invocation_context, event):
        run = self._runs.get(invocation_context.invocation_id)
        if run is not None and event.error_code:
            run["failed"] = True

    def _end_run(self, context, failed):
        run = self._runs.get(context.invocation_id)
        if run is None:
            return
        for operation in run["operations"].values():
            if not operation["done"]:
                self._finish(context, operation["kind"], operation["identity"], "incomplete")
        failed |= run["failed"]
        self._emit(run, "invocation", "failure" if failed else "completion", run,
                   "error" if failed else "ok")
        del self._runs[context.invocation_id]

    async def after_run_callback(self, *, invocation_context):
        self._end_run(invocation_context, False)

    async def on_run_error_callback(self, *, invocation_context, error):
        self._end_run(invocation_context, True)

    async def close(self):
        self._runs.clear()
