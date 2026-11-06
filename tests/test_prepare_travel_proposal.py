import unittest
from unittest.mock import Mock, patch

from sqlalchemy.exc import SQLAlchemyError

from travel_agent.request_context import preparation_request
from travel_agent.services.proposals import prepare
from travel_agent.tools.prepare_travel_proposal import prepare_travel_proposal


INPUTS = dict(destination="Sydney", departure_date="2026-11-16",
              return_date="2026-11-18", purpose="Conference",
              proposed_budget_aud=1500, registration_fee_aud=0)


class PrepareTravelProposalTests(unittest.TestCase):
    def setUp(self):
        from travel_agent.request_context import PreparationRequest, preparation_request
        token = preparation_request.set(PreparationRequest("offline", "request-1"))
        self.addCleanup(preparation_request.reset, token)

    def test_configuration_errors_are_safe(self):
        with patch("travel_agent.db.session.create_session_factory", side_effect=ValueError("private-secret")) as factory:
            result = prepare_travel_proposal(**INPUTS)
        self.assertEqual(result["error"]["code"], "database_configuration_error")
        self.assertFalse(result["retryable"])
        self.assertNotIn("private-secret", str(result))
        factory.assert_called_once()

    def test_factory_database_error(self):
        with patch("travel_agent.db.session.create_session_factory", side_effect=SQLAlchemyError("private-secret")):
            result = prepare_travel_proposal(**INPUTS)
        self.assertEqual(result["error"]["code"], "proposal_storage_error")
        self.assertNotIn("private-secret", str(result))

    def test_results_errors_and_cleanup(self):
        for outcome in ({"status": "NEEDS_REVIEW", "proposal_id": "saved-id"},
                        {"status": "error", "error": {"code": "invalid_input"}},
                        SQLAlchemyError("private-secret"), RuntimeError("private-secret"),
                        ValueError("programming failure")):
            with self.subTest(outcome=type(outcome).__name__):
                factory = Mock()
                factory.kw = {"bind": Mock()}
                with patch("travel_agent.db.session.create_session_factory", return_value=factory), patch(
                    "travel_agent.services.proposals.prepare") as service:
                    if isinstance(outcome, Exception):
                        service.side_effect = outcome
                    else:
                        service.return_value = outcome
                    if isinstance(outcome, (RuntimeError, ValueError)):
                        with self.assertRaises(type(outcome)):
                            prepare_travel_proposal(**INPUTS)
                    else:
                        result = prepare_travel_proposal(**INPUTS)
                        if isinstance(outcome, SQLAlchemyError):
                            self.assertEqual(result["error"]["code"], "proposal_storage_error")
                            self.assertFalse(result["retryable"])
                            self.assertNotIn("private-secret", str(result))
                        else:
                            self.assertEqual(result, outcome)
                service.assert_called_once()
                factory.kw["bind"].dispose.assert_called_once()

    def test_validation_never_opens_transaction(self):
        factory = Mock()
        factory.begin.side_effect = AssertionError("Invalid input attempted database access")
        for changes in ({"purpose": " "}, {"destination": "Tokyo"},
                        {"departure_date": "bad"}, {"return_date": "2026-11-15"},
                        {"proposed_budget_aud": -1}, {"registration_fee_aud": float("nan")}):
            with self.subTest(changes=changes):
                self.assertEqual(prepare(factory, **(INPUTS | changes), request=preparation_request.get())["status"], "error")
        factory.begin.assert_not_called()
