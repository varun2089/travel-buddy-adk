import os
import re
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.responses import JSONResponse


@dataclass(frozen=True)
class PreparationRequest:
    scope: str
    key: str


preparation_request = ContextVar("preparation_request", default=None)


def shared_scope():
    value = os.environ.get("PROPOSAL_REQUEST_SCOPE", "travel-buddy:prepare:v1")
    if not re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value):
        raise ValueError("Invalid PROPOSAL_REQUEST_SCOPE configuration.")
    return value


class PreparationRequestMiddleware:
    def __init__(self, app, request_scope):
        self.app = app
        self.request_scope = request_scope

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        values = [value for name, value in scope.get("headers", [])
                  if name.lower() == b"idempotency-key"]
        context = None
        if values:
            if len(values) != 1 or not re.fullmatch(rb"[A-Za-z0-9._:-]{1,128}", values[0]):
                response = JSONResponse({"error": "invalid_idempotency_key"}, status_code=400)
                return await response(scope, receive, send)
            context = PreparationRequest(self.request_scope, values[0].decode("ascii"))
        token = preparation_request.set(context)
        try:
            await self.app(scope, receive, send)
        finally:
            preparation_request.reset(token)
