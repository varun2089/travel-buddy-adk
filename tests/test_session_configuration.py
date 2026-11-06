import contextlib
import io
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from travel_agent.server import create_app
from travel_agent.session_config import session_database_kwargs, session_database_url
from travel_agent.session_setup import main


URL = 'postgresql+psycopg://runtime:private-password@localhost/travel_test'


class SessionConfigurationTests(unittest.TestCase):
    def test_invalid_configuration_is_safe_and_precedes_tracing(self):
        for value in ('', 'private-password', 'postgresql://runtime:private-password@localhost/db',
                      URL + '?options=private-password', URL + '?service=private-password',
                      'postgresql+psycopg://runtime:private-password@localhost:invalid/db'):
            with self.subTest(value=value), patch.dict(os.environ, {'SESSION_DATABASE_URL': value}, clear=True), patch(
                'travel_agent.server.configure_tracing') as tracing:
                with self.assertRaises(ValueError) as error:
                    create_app()
                self.assertNotIn('private-password', str(error.exception))
                self.assertIsNone(error.exception.__cause__)
                tracing.assert_not_called()

    def test_server_passes_fixed_database_settings_and_preserves_limits(self):
        with patch.dict(os.environ, {'SESSION_DATABASE_URL': URL}, clear=True), patch(
            'travel_agent.server.configure_tracing', return_value=MagicMock()), patch(
            'google.adk.cli.fast_api.get_fast_api_app') as factory:
            create_app()
        args = factory.call_args.kwargs
        self.assertEqual(args['session_service_uri'], URL)
        self.assertEqual(args['session_db_kwargs'], session_database_kwargs())
        self.assertEqual(args['session_db_kwargs']['connect_args'], {'options': '-c search_path=adk_sessions'})
        self.assertEqual(args['max_llm_calls'], 5)
        self.assertEqual(args['artifact_service_uri'], 'memory://')
        self.assertEqual(args['extra_plugins'], ['travel_agent.observability.ObservabilityPlugin'])
        self.assertFalse(args['trace_to_cloud'])
        self.assertFalse(args['otel_to_cloud'])

    def test_cloud_socket_url_is_accepted(self):
        value = 'postgresql+psycopg://runtime:encoded%40password@/travel_buddy?host=/cloudsql/project:region:instance'
        with patch.dict(os.environ, {'SESSION_DATABASE_URL': value}, clear=True):
            self.assertEqual(session_database_url(), value)

    def test_setup_never_falls_back_to_runtime_credentials(self):
        with patch.dict(os.environ, {'SESSION_DATABASE_URL': URL}, clear=True), patch(
            'travel_agent.session_setup.prepare') as prepare, contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(), 1)
        prepare.assert_not_called()
        self.assertNotIn('private-password', output.getvalue())


class SessionSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_closes_engine_on_success_and_failure(self):
        from travel_agent.session_setup import prepare

        for failure in (None, RuntimeError('private-password')):
            service = MagicMock()
            service.prepare_tables = AsyncMock(side_effect=failure)
            service.close = AsyncMock()
            with patch('google.adk.sessions.database_session_service.DatabaseSessionService',
                       return_value=service) as constructor:
                if failure:
                    with self.assertRaises(RuntimeError):
                        await prepare(URL)
                else:
                    await prepare(URL)
            constructor.assert_called_once_with(URL, **session_database_kwargs())
            service.prepare_tables.assert_awaited_once()
            service.close.assert_awaited_once()
