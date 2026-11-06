import os
import unittest
from uuid import uuid4

TEST_URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "TEST_DATABASE_URL is not configured; PostgreSQL integration tests skipped")
class PolicyPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy.engine import make_url
        from travel_agent.db.policy_repository import create_policy_session_factory

        url = make_url(TEST_URL)
        if url.drivername != "postgresql+psycopg" or not url.database or not url.database.endswith("_test"):
            raise RuntimeError("Use a separate PostgreSQL database ending in _test.")
        development = os.environ.get("DATABASE_URL")
        if development and make_url(development).database == url.database:
            raise RuntimeError("The test database must differ from DATABASE_URL.")
        cls.factory = create_policy_session_factory(TEST_URL)
        from sqlalchemy import delete, select
        from travel_agent.db.policy_models import ActivePolicy

        with cls.factory.begin() as session:
            cls.original_active = [dict(document_id=row.document_id, version=row.version,
                                        section_id=row.section_id)
                                   for row in session.scalars(select(ActivePolicy))]
            session.execute(delete(ActivePolicy))

    @classmethod
    def tearDownClass(cls):
        from sqlalchemy import insert
        from travel_agent.db.policy_models import ActivePolicy

        try:
            if cls.original_active:
                with cls.factory.begin() as session:
                    session.execute(insert(ActivePolicy), cls.original_active)
        finally:
            cls.factory.kw["bind"].dispose()

    def setUp(self):
        from travel_agent.policy.chunking import chunk_markdown
        from travel_agent.policy.config import LABEL

        self.document_id = "TEST-" + uuid4().hex
        self.document_ids = [self.document_id]
        self.sections = chunk_markdown(
            f"# {LABEL}\nDocument-ID: {self.document_id}\nVersion: v1\n"
            "\n## A | Receipts\nKeep receipts.\n\n## B | Review\nHuman review required.\n")
        self.addCleanup(self.cleanup_rows)

    def cleanup_rows(self):
        from sqlalchemy import delete
        from travel_agent.db.policy_models import ActivePolicy, PolicySection

        with self.factory.begin() as session:
            session.execute(delete(ActivePolicy).where(ActivePolicy.document_id.in_(self.document_ids)))
            session.execute(delete(PolicySection).where(PolicySection.document_id.in_(self.document_ids)))

    def vector(self, text, task):
        return ([1.0, 0.0] if text.startswith("Receipts") else [0.0, 1.0]) + [0.0] * 766

    def test_persistence_idempotency_conflict_and_exact_search(self):
        from dataclasses import replace
        from unittest.mock import Mock
        from travel_agent.db.policy_repository import (
            VersionConflict, activate_policy, create_policy_session_factory, ingest_sections, nearest_sections,
        )

        self.assertEqual(ingest_sections(self.factory, self.sections, "fixture.md", self.vector)["inserted"], 2)
        embedder = Mock(side_effect=AssertionError("Unchanged ingestion must not embed"))
        self.assertEqual(ingest_sections(self.factory, self.sections, "fixture.md", embedder)["unchanged"], 2)
        embedder.assert_not_called()
        with self.assertRaises(VersionConflict):
            ingest_sections(self.factory, [replace(self.sections[0], content_hash="changed")], "fixture.md", embedder)
        activate_policy(self.factory, self.document_id, "v1")
        other = create_policy_session_factory(TEST_URL)
        try:
            rows = nearest_sections(other, self.vector("Receipts", "RETRIEVAL_QUERY"))
            self.assertEqual([row["section_id"] for row in rows], ["A", "B"])
            self.assertAlmostEqual(rows[0]["distance"], 0)
            self.assertAlmostEqual(rows[1]["distance"], 1)
            self.assertEqual({(row["document_id"], row["version"]) for row in rows},
                             {(self.document_id, "v1")})
        finally:
            other.kw["bind"].dispose()

    def test_failed_embedding_rolls_back_document(self):
        from unittest.mock import Mock
        from travel_agent.db.policy_repository import ingest_sections, list_policies

        embedder = Mock(side_effect=[self.vector("Receipts", ""), RuntimeError("Unavailable")])
        with self.assertRaises(RuntimeError):
            ingest_sections(self.factory, self.sections, "fixture.md", embedder)
        self.assertFalse(any(row["document_id"] == self.document_id for row in list_policies(self.factory)))

    def test_global_ranking_replacement_deactivation_and_preservation(self):
        from dataclasses import replace
        from decimal import Decimal
        from sqlalchemy import delete, select
        from travel_agent.db.models import AuditRecord, Proposal, Review
        from travel_agent.db.policy_models import PolicySection
        from travel_agent.db.policy_repository import (
            activate_policy, deactivate_policy, has_active_policies,
            ingest_sections, list_policies, nearest_sections,
        )
        from travel_agent.services.proposals import prepare, show
        from uuid import UUID
        from travel_agent.request_context import PreparationRequest

        second_id = "TEST-" + uuid4().hex
        self.document_ids.append(second_id)
        proposal = prepare(self.factory, request=PreparationRequest("policy-tests", str(uuid4())), destination="Sydney", departure_date="2026-11-16",
                           return_date="2026-11-18", purpose="Conference",
                           proposed_budget_aud=Decimal("1500"), registration_fee_aud=Decimal("200"))
        proposal_id = UUID(proposal["proposal_id"])

        def cleanup_proposal():
            with self.factory.begin() as session:
                for model in (AuditRecord, Review, Proposal):
                    column = model.id if model is Proposal else model.proposal_id
                    session.execute(delete(model).where(column == proposal_id))

        self.addCleanup(cleanup_proposal)
        query = [1.0] + [0.0] * 767
        second = [replace(section, document_id=second_id) for section in self.sections]
        replacement = [replace(section, version="v2") for section in self.sections]
        unused = [replace(section, version="v99") for section in self.sections]
        for sections, vector in ((self.sections, [0.8, 0.6]), (second, [1.0, 0.0]),
                                 (replacement, [0.6, 0.8]), (unused, [1.0, 0.0])):
            ingest_sections(self.factory, sections, "fixture.md",
                            lambda text, task, vector=vector: vector + [0.0] * 766)
        self.assertFalse(has_active_policies(self.factory))
        self.assertEqual(nearest_sections(self.factory, query), [])
        with self.factory.begin() as session:
            before = [(row.document_id, row.version, row.section_id, row.text, row.content_hash,
                       row.embedding_model, row.embedding.tolist(), row.created_at)
                      for row in session.scalars(select(PolicySection).where(
                          PolicySection.document_id.in_(self.document_ids)).order_by(
                              PolicySection.document_id, PolicySection.version, PolicySection.section_id))]
        activate_policy(self.factory, self.document_id, "v1")
        activate_policy(self.factory, second_id, "v1")
        rows = nearest_sections(self.factory, query)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["document_id"] for row in rows], [second_id, second_id, self.document_id])
        self.assertEqual([row["distance"] for row in rows], sorted(row["distance"] for row in rows))
        self.assertTrue(all(row["version"] == "v1" for row in rows))
        activate_policy(self.factory, self.document_id, "v2")
        active = {(row["document_id"], row["version"]) for row in list_policies(self.factory) if row["active"]}
        self.assertEqual(active, {(self.document_id, "v2"), (second_id, "v1")})
        with self.assertRaises(ValueError):
            activate_policy(self.factory, self.document_id, "missing")
        self.assertEqual({(row["document_id"], row["version"]) for row in list_policies(self.factory)
                          if row["active"]}, active)
        rows = nearest_sections(self.factory, query)
        self.assertEqual({(row["document_id"], row["version"]) for row in rows}, active)
        deactivate_policy(self.factory, second_id)
        rows = nearest_sections(self.factory, query)
        self.assertEqual({(row["document_id"], row["version"]) for row in rows}, {(self.document_id, "v2")})
        deactivate_policy(self.factory, self.document_id)
        deactivate_policy(self.factory, self.document_id)
        self.assertFalse(has_active_policies(self.factory))
        self.assertEqual(nearest_sections(self.factory, query), [])
        with self.factory.begin() as session:
            after = [(row.document_id, row.version, row.section_id, row.text, row.content_hash,
                      row.embedding_model, row.embedding.tolist(), row.created_at)
                     for row in session.scalars(select(PolicySection).where(
                         PolicySection.document_id.in_(self.document_ids)).order_by(
                             PolicySection.document_id, PolicySection.version, PolicySection.section_id))]
        self.assertEqual(after, before)
        self.assertEqual(show(self.factory, proposal["proposal_id"]), proposal)

    def test_database_rejects_missing_version_and_duplicate_active_document(self):
        from sqlalchemy import insert
        from sqlalchemy.exc import IntegrityError
        from travel_agent.db.policy_models import ActivePolicy
        from travel_agent.db.policy_repository import activate_policy, ingest_sections

        with self.assertRaises(IntegrityError):
            with self.factory.begin() as session:
                session.execute(insert(ActivePolicy).values(
                    document_id=self.document_id, version="missing", section_id="A"))
        ingest_sections(self.factory, self.sections, "fixture.md", self.vector)
        with self.assertRaises(ValueError):
            activate_policy(self.factory, self.document_id, "missing")
        activate_policy(self.factory, self.document_id, "v1")
        with self.assertRaises(IntegrityError):
            with self.factory.begin() as session:
                session.execute(insert(ActivePolicy).values(
                    document_id=self.document_id, version="v1", section_id="B"))
