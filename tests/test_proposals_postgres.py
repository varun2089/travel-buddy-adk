import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4
from travel_agent.request_context import PreparationRequest


TEST_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "TEST_DATABASE_URL is not configured; PostgreSQL integration tests skipped")
class ProposalPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy.engine import make_url
        from travel_agent.db.session import create_session_factory

        url = make_url(TEST_URL)
        if url.drivername != "postgresql+psycopg" or not url.database or not url.database.endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL must use PostgreSQL and a database ending in _test.")
        development_url = os.environ.get("DATABASE_URL")
        if development_url and make_url(development_url).database == url.database:
            raise RuntimeError("The test database must differ from DATABASE_URL.")
        cls.factory = create_session_factory(TEST_URL)

    @classmethod
    def tearDownClass(cls):
        cls.factory.kw["bind"].dispose()

    def setUp(self):
        self.proposal_ids = []

    def tearDown(self):
        from sqlalchemy import delete
        from travel_agent.db.models import AuditRecord, Proposal, Review

        with self.factory.begin() as session:
            for model in (AuditRecord, Review, Proposal):
                column = model.id if model is Proposal else model.proposal_id
                session.execute(delete(model).where(column.in_(self.proposal_ids)))

    def prepare(self, **changes):
        from travel_agent.services.proposals import prepare

        values = dict(destination="  sYdNeY ", departure_date="2026-11-16",
                      return_date="2026-11-18", purpose="Attend conference",
                      proposed_budget_aud=Decimal("1500.30"), registration_fee_aud=Decimal("200.10"))
        values.update(changes)
        request = values.pop("request", PreparationRequest("postgres-tests", str(uuid4())))
        result = prepare(self.factory, **values, request=request)
        if "proposal_id" in result:
            self.proposal_ids.append(UUID(result["proposal_id"]))
        return result

    def review(self, proposal, action="approve", **changes):
        from travel_agent.services.proposals import review_proposal

        values = dict(reviewer="reviewer@example.test", confirmed=True)
        values.update(changes)
        return review_proposal(self.factory, proposal["proposal_id"], action, **values)

    def test_preparation_persistence_and_corrections(self):
        from travel_agent.db.session import create_session_factory
        from travel_agent.services.proposals import show

        proposal = self.prepare()
        self.assertEqual(proposal["status"], "NEEDS_REVIEW")
        self.assertEqual(proposal["inputs"]["destination"], "  sYdNeY ")
        self.assertEqual(proposal["inputs"]["proposed_budget_aud"], "1500.30")
        self.assertEqual(proposal["results"]["complete_total_aud"], "1360.10")
        self.assertEqual(proposal["results"]["source"], "synthetic_training_data")
        other = create_session_factory(TEST_URL)
        try:
            self.assertEqual(show(other, proposal["proposal_id"]), proposal)
        finally:
            other.kw["bind"].dispose()
        self.assertNotEqual(self.prepare()["proposal_id"], proposal["proposal_id"])

    def test_approval_is_atomic_and_repeatable(self):
        from sqlalchemy import select
        from travel_agent.db.models import AuditRecord
        from travel_agent.services.proposals import show

        proposal = self.prepare()
        approved = self.review(proposal)
        self.assertEqual(approved["status"], "SUBMITTED")
        self.assertTrue(approved["confirmation"].startswith("MOCK-"))
        self.assertEqual(self.review(proposal), approved)
        self.assertEqual(show(self.factory, proposal["proposal_id"]), approved)
        with self.factory.begin() as session:
            audit = session.scalars(select(AuditRecord).where(
                AuditRecord.proposal_id == UUID(proposal["proposal_id"]))).all()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0].confirmation, approved["confirmation"])
            self.assertEqual(audit[0].reviewer, "reviewer@example.test")
        with self.assertRaises(ValueError):
            self.review(proposal, "reject")

    def test_rejection_and_confirmation(self):
        proposal = self.prepare()
        for changes in ({"reviewer": " "}, {"confirmed": False}):
            with self.assertRaises(ValueError):
                self.review(proposal, **changes)
        self.assertEqual(self.review(proposal, "reject")["status"], "REJECTED")
        for action in ("approve", "reject"):
            with self.assertRaises(ValueError):
                self.review(proposal, action)

    def test_incomplete_and_over_budget(self):
        from travel_agent.services.proposals import show

        for changes in ({"registration_fee_aud": None}, {"proposed_budget_aud": 1000}):
            proposal = self.prepare(**changes)
            with self.assertRaises(ValueError):
                self.review(proposal)
            self.assertEqual(show(self.factory, proposal["proposal_id"])["status"], "NEEDS_REVIEW")
            self.assertEqual(self.review(proposal, "reject")["status"], "REJECTED")

    def test_concurrent_duplicate_approvals(self):
        from sqlalchemy import func, select
        from travel_agent.db.models import AuditRecord

        proposal = self.prepare()
        barrier = Barrier(2)

        def approve():
            barrier.wait(timeout=10)
            return self.review(proposal)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results[0]["confirmation"], results[1]["confirmation"])
        with self.factory.begin() as session:
            count = session.scalar(select(func.count()).select_from(AuditRecord).where(
                AuditRecord.proposal_id == UUID(proposal["proposal_id"])))
            self.assertEqual(count, 1)

    def test_proposal_update_is_blocked(self):
        from sqlalchemy import update
        from sqlalchemy.exc import DBAPIError
        from travel_agent.db.models import Proposal

        proposal = self.prepare()
        with self.assertRaises(DBAPIError):
            with self.factory.begin() as session:
                session.execute(update(Proposal).where(Proposal.id == UUID(proposal["proposal_id"])).values(inputs={}))

    def test_audit_failure_rolls_back_approval(self):
        from unittest.mock import patch
        from sqlalchemy import func, select
        from travel_agent.db.models import AuditRecord
        from travel_agent.services.proposals import show

        proposal = self.prepare()
        with patch("travel_agent.services.proposals.add_audit", side_effect=RuntimeError("Audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.review(proposal)
        saved = show(self.factory, proposal["proposal_id"])
        self.assertEqual(saved["status"], "NEEDS_REVIEW")
        self.assertIsNone(saved["confirmation"])
        with self.factory.begin() as session:
            count = session.scalar(select(func.count()).select_from(AuditRecord).where(
                AuditRecord.proposal_id == UUID(proposal["proposal_id"])))
            self.assertEqual(count, 0)

    def test_idempotency_replay_conflict_and_review(self):
        from travel_agent.services.proposals import recover
        request = PreparationRequest("postgres-tests", str(uuid4()))
        first = self.prepare(request=request)
        self.assertEqual(self.prepare(request=request, destination="Sydney", proposed_budget_aud=Decimal("1500.300")), first)
        self.assertEqual(self.prepare(request=request, purpose="Changed")["error"]["code"], "idempotency_key_conflict")
        reviewed = self.review(first)
        self.assertEqual(self.prepare(request=request), reviewed)
        from travel_agent.db.session import create_session_factory
        other = create_session_factory(TEST_URL)
        try:
            self.assertEqual(recover(other, request), reviewed)
            self.assertIsNone(recover(other, PreparationRequest("other-scope", request.key)))
        finally:
            other.kw["bind"].dispose()

    def test_concurrent_preparation_same_and_conflicting_inputs(self):
        from sqlalchemy import func, select
        from travel_agent.db.models import Proposal, Review
        for conflicting in (False, True):
            request = PreparationRequest("postgres-tests", str(uuid4()))
            barrier = Barrier(2)
            def attempt(index):
                barrier.wait(timeout=10)
                return self.prepare(request=request, purpose="Changed" if conflicting and index else "Attend conference")
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, range(2)))
            if conflicting:
                self.assertEqual(sum(r.get("error", {}).get("code") == "idempotency_key_conflict" for r in results), 1)
            else:
                self.assertEqual(results[0]["proposal_id"], results[1]["proposal_id"])
            with self.factory.begin() as session:
                count = session.scalar(select(func.count()).select_from(Proposal).join(Review).where(
                    Proposal.request_scope == request.scope, Proposal.request_key == request.key))
                self.assertEqual(count, 1)

    def test_failed_review_insert_rolls_back_request(self):
        from unittest.mock import patch
        from sqlalchemy.orm import Session
        from travel_agent.db.models import Review
        from travel_agent.services.proposals import recover
        request = PreparationRequest("postgres-tests", str(uuid4()))
        original = Session.add
        def fail_review(session, instance, *args, **kwargs):
            if isinstance(instance, Review):
                raise RuntimeError("simulated failure")
            return original(session, instance, *args, **kwargs)
        with patch.object(Session, "add", fail_review), self.assertRaises(RuntimeError):
            self.prepare(request=request)
        self.assertIsNone(recover(self.factory, request))
        self.assertIn("proposal_id", self.prepare(request=request))
