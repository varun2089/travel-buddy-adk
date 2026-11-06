import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from travel_agent.request_context import PreparationRequest, PreparationRequestMiddleware, preparation_request
from travel_agent.services.proposals import normalize_inputs, prepare, replay
from travel_agent.tools.prepare_travel_proposal import prepare_travel_proposal


INPUTS = dict(destination="Sydney", departure_date="2026-11-16", return_date="2026-11-18",
              purpose="Conference", proposed_budget_aud=1500, registration_fee_aud=0)
REQUEST = PreparationRequest("offline", "key-1")


class IdempotencyTests(unittest.TestCase):
    def test_normalization_and_conflicts(self):
        normalized = normalize_inputs(INPUTS)
        equivalent = INPUTS | dict(destination=" sYdNeY ", purpose=" Conference ",
                                   proposed_budget_aud=Decimal("1500.000"), registration_fee_aud=-0.0)
        self.assertEqual(normalized, normalize_inputs(equivalent))
        proposal = SimpleNamespace(id="saved", inputs=INPUTS, results={}, normalized_inputs=normalized)
        review = SimpleNamespace(status="SUBMITTED", confirmation="MOCK-saved")
        self.assertEqual(replay((proposal, review), normalized)["status"], "SUBMITTED")
        for field, value in dict(destination="Brisbane", purpose="Other", departure_date="2026-11-15",
                                 return_date="2026-11-19", proposed_budget_aud=1600,
                                 registration_fee_aud=None).items():
            self.assertEqual(replay((proposal, review), normalize_inputs(INPUTS | {field: value}))
                             ["error"]["code"], "idempotency_key_conflict")

    def test_replay_does_not_recalculate(self):
        proposal = SimpleNamespace(id="saved", inputs=INPUTS, results={}, normalized_inputs=normalize_inputs(INPUTS))
        review = SimpleNamespace(status="REJECTED", confirmation=None)
        with patch("travel_agent.services.proposals.find_proposal_request", return_value=(proposal, review)), patch(
            "travel_agent.services.proposals.calculate_trip_cost_exact", side_effect=AssertionError):
            result = prepare(MagicMock(), **INPUTS, request=REQUEST)
        self.assertEqual(result["status"], "REJECTED")

    def test_unique_race_recovers_only_named_constraint(self):
        from sqlalchemy.exc import IntegrityError
        proposal = SimpleNamespace(id="winner", inputs=INPUTS, results={}, normalized_inputs=normalize_inputs(INPUTS))
        review = SimpleNamespace(status="NEEDS_REVIEW", confirmation=None)
        for constraint in ("proposals_request_unique", "other_constraint"):
            original = Exception("private-secret")
            original.sqlstate = "23505"
            original.diag = SimpleNamespace(constraint_name=constraint)
            error = IntegrityError("insert", {}, original)
            factory = MagicMock()
            with patch("travel_agent.services.proposals.find_proposal_request", side_effect=[None, (proposal, review)]), patch(
                "travel_agent.services.proposals.add_proposal", side_effect=error):
                if constraint == "proposals_request_unique":
                    self.assertEqual(prepare(factory, **INPUTS, request=REQUEST)["proposal_id"], "winner")
                    self.assertEqual(factory.begin.call_count, 3)
                else:
                    with self.assertRaises(IntegrityError):
                        prepare(factory, **INPUTS, request=REQUEST)

    def test_missing_context_never_opens_database(self):
        token = preparation_request.set(None)
        try:
            with patch("travel_agent.db.session.create_session_factory") as factory:
                self.assertEqual(prepare_travel_proposal(**INPUTS)["error"]["code"], "missing_idempotency_key")
                factory.assert_not_called()
        finally:
            preparation_request.reset(token)

    def test_tool_schema_has_no_key_or_scope(self):
        from google.adk.tools import FunctionTool
        declaration = FunctionTool(prepare_travel_proposal)._get_declaration()
        self.assertEqual(set(declaration.parameters_json_schema["properties"]), set(INPUTS))


class RequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_lifetime_isolation_and_cleanup(self):
        seen = []
        async def app(scope, receive, send):
            async def worker():
                await asyncio.sleep(0)
                seen.append((scope["path"], preparation_request.get()))
            await asyncio.create_task(worker())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await asyncio.sleep(0)
            self.assertEqual(preparation_request.get().key, scope["headers"][0][1].decode())
            await send({"type": "http.response.body", "body": b"done"})
        middleware = PreparationRequestMiddleware(app, "configured")
        async def invoke(path, key):
            async def noop(*args):
                pass
            await middleware({"type": "http", "path": path, "headers": [(b"idempotency-key", key)]}, noop, noop)
            self.assertIsNone(preparation_request.get())
        await asyncio.gather(invoke("/run", b"first"), invoke("/run_sse", b"second"))
        self.assertEqual({context.key for _, context in seen}, {"first", "second"})
        self.assertEqual({context.scope for _, context in seen}, {"configured"})

    async def test_rejection_and_exception_cleanup(self):
        async def app(*args):
            raise asyncio.CancelledError
        async def noop(*args):
            pass
        middleware = PreparationRequestMiddleware(app, "configured")
        for headers in ([(b"idempotency-key", b"bad key")], [(b"idempotency-key", b"a")] * 2):
            messages = []
            async def send(message):
                messages.append(message)
            await middleware({"type": "http", "headers": headers}, noop, send)
            self.assertEqual(messages[0]["status"], 400)
        with self.assertRaises(asyncio.CancelledError):
            await middleware({"type": "http", "headers": [(b"idempotency-key", b"valid")]}, noop, noop)
        self.assertIsNone(preparation_request.get())


class RecoveryTests(unittest.TestCase):
    def test_recovery_endpoint_is_read_only_and_safe(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from travel_agent.server import create_app
        with patch("travel_agent.server.session_database_url", return_value="unused"), patch(
            "travel_agent.server.configure_tracing"), patch(
            "google.adk.cli.fast_api.get_fast_api_app", return_value=FastAPI()), patch(
            "travel_agent.db.session.create_session_factory") as factory, patch(
            "travel_agent.services.proposals.recover") as recover_mock:
            app = create_app()
            with TestClient(app) as client:
                path = "/proposal-preparations/recovery"
                self.assertEqual(client.get(path).status_code, 400)
                factory.assert_not_called()
                for value, status in ((None, 404), ({"proposal_id": "saved", "status": "SUBMITTED"}, 200)):
                    recover_mock.return_value = value
                    response = client.get(path, headers={"Idempotency-Key": "recovery"})
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                recover_mock.side_effect = ValueError("private-secret")
                response = client.get(path, headers={"Idempotency-Key": "recovery"})
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private-secret", response.text)
                self.assertEqual(client.post(path).status_code, 405)
