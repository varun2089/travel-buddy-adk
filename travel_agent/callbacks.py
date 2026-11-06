def tool_error(code, message):
    return {
        "status": "error",
        "error": {"code": code, "message": message},
        "retryable": False,
    }


def guard_proposal_preparation(tool, args, tool_context):
    if tool.name != "prepare_travel_proposal":
        return None
    key = "temp:proposal_preparation_invocation"
    if tool_context.state.get(key) == tool_context.invocation_id:
        return tool_error(
            "proposal_attempt_already_made",
            "Proposal preparation was already attempted in this run. Wait for a new explicit user request.",
        )
    tool_context.state[key] = tool_context.invocation_id
    return None


def handle_tool_error(tool, args, tool_context, error):
    return tool_error(
        "tool_execution_error",
        "The tool could not complete. Do not retry automatically or assume a proposal was saved.",
    )
