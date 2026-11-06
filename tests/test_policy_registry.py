import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql

from travel_agent.db.policy_repository import (
    activate_policy, deactivate_policy, has_active_policies, list_policies, nearest_sections,
)
from travel_agent.policy.config import DIMENSIONS, TOP_K


class RegistryOfflineTests(unittest.TestCase):
    def setUp(self):
        self.factory = MagicMock()
        self.session = self.factory.begin.return_value.__enter__.return_value

    def sql(self, statement):
        return str(statement.compile(dialect=postgresql.dialect(),
                                     compile_kwargs={"literal_binds": True}))

    def test_activation_missing_version_does_not_write(self):
        self.session.scalar.return_value = None
        with self.assertRaisesRegex(ValueError, "not been ingested"):
            activate_policy(self.factory, "DOC", "v1")
        self.session.execute.assert_not_called()

    def test_activation_uses_atomic_upsert_and_existing_section(self):
        self.session.scalar.return_value = "A"
        self.assertTrue(activate_policy(self.factory, "DOC", "v2")["active"])
        sql = self.sql(self.session.execute.call_args.args[0])
        self.assertIn("ON CONFLICT (document_id) DO UPDATE", sql)
        self.assertIn("'DOC', 'v2', 'A'", sql)
        self.assertIn("FOR SHARE", self.sql(self.session.scalar.call_args.args[0]))
        self.factory.begin.assert_called_once()

    def test_deactivation_only_deletes_registry_row(self):
        self.assertFalse(deactivate_policy(self.factory, "DOC")["active"])
        self.assertEqual(self.sql(self.session.execute.call_args.args[0]),
                         "DELETE FROM active_policies WHERE active_policies.document_id = 'DOC'")

    def test_invalid_identifiers_do_not_access_database(self):
        for action, args in ((activate_policy, ("bad/id", "v1")),
                             (activate_policy, ("DOC", "")), (deactivate_policy, ("bad/id",))):
            with self.assertRaises(ValueError):
                action(self.factory, *args)
        self.factory.begin.assert_not_called()

    def test_listing_includes_inactive_versions_read_only(self):
        rows = [{"document_id": "DOC", "version": "v1", "sections": 2, "active": False}]
        self.session.execute.return_value.mappings.return_value = rows
        self.assertEqual(list_policies(self.factory), rows)
        calls = self.session.execute.call_args_list
        self.assertEqual(str(calls[0].args[0]), "SET TRANSACTION READ ONLY")
        sql = self.sql(calls[1].args[0])
        self.assertIn("LEFT OUTER JOIN active_policies", sql)
        self.assertIn("active_policies.version = policy_sections.version", sql)

    def test_empty_registry_read_only(self):
        self.session.scalar.return_value = None
        self.assertFalse(has_active_policies(self.factory))
        self.assertEqual(str(self.session.execute.call_args.args[0]), "SET TRANSACTION READ ONLY")

    def test_global_ranking_filters_by_registry_and_limits_once(self):
        self.session.execute.return_value = []
        self.assertEqual(nearest_sections(self.factory, [1.0] + [0.0] * (DIMENSIONS - 1)), [])
        calls = self.session.execute.call_args_list
        self.assertEqual(str(calls[0].args[0]), "SET TRANSACTION READ ONLY")
        sql = self.sql(calls[-1].args[0])
        self.assertIn("JOIN active_policies ON active_policies.document_id = policy_sections.document_id", sql)
        self.assertIn("active_policies.version = policy_sections.version", sql)
        self.assertIn("ORDER BY distance, policy_sections.document_id", sql)
        self.assertIn(f"LIMIT {TOP_K}", sql)
        self.assertIn("policy_sections.embedding_model =", sql)
        self.assertIn("policy_sections.dimensions = 768", sql)

    def test_migration_only_adds_empty_registry_with_foreign_key(self):
        spec = importlib.util.spec_from_file_location(
            "registry_migration", Path("migrations/versions/0003_active_policies.py"))
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.assertEqual(migration.down_revision, "0002")
        with patch.object(migration, "op") as op:
            migration.upgrade()
            self.assertEqual([call[0] for call in op.method_calls], ["create_table"])
            args = op.create_table.call_args.args
            self.assertEqual(args[0], "active_policies")
            self.assertTrue(args[1].primary_key)
            self.assertEqual([element.target_fullname for element in args[-1].elements],
                             ["policy_sections.document_id", "policy_sections.version", "policy_sections.section_id"])
            migration.downgrade()
            op.drop_table.assert_called_once_with("active_policies")


class AdministrationOfflineTests(unittest.TestCase):
    def test_commands_dispatch_and_dispose(self):
        from travel_agent.policy.admin import main

        for arguments, name, expected in ((["list"], "list_policies", ()),
                                           (["activate", "DOC", "v2"], "activate_policy", ("DOC", "v2")),
                                           (["deactivate", "DOC"], "deactivate_policy", ("DOC",))):
            with self.subTest(command=arguments), patch("sys.argv", ["policy-admin", *arguments]), patch(
                "travel_agent.policy.admin.create_policy_session_factory") as factory, patch(
                "travel_agent.policy.admin." + name, return_value=[]) as action, patch("builtins.print"):
                self.assertEqual(main(), 0)
                action.assert_called_once_with(factory.return_value, *expected)
                factory.return_value.kw["bind"].dispose.assert_called_once()

    def test_errors_hide_details_and_dispose(self):
        from travel_agent.policy.admin import main

        for error, code in ((ValueError("private-secret"), 2), (RuntimeError("private-secret"), 1)):
            with patch("sys.argv", ["policy-admin", "activate", "DOC", "v1"]), patch(
                "travel_agent.policy.admin.create_policy_session_factory") as factory, patch(
                "travel_agent.policy.admin.activate_policy", side_effect=error), patch("builtins.print") as output:
                self.assertEqual(main(), code)
                self.assertNotIn("private-secret", output.call_args.args[0])
                factory.return_value.kw["bind"].dispose.assert_called_once()

    def test_administration_is_not_an_agent_tool(self):
        from travel_agent.agent import root_agent
        from travel_agent.policy.admin import main

        for action in (main, activate_policy, deactivate_policy, list_policies):
            self.assertNotIn(action, root_agent.tools)
