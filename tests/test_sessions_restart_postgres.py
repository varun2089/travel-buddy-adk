import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from sqlalchemy.engine import make_url

from travel_agent.session_config import session_database_url


class SessionRestartTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('SESSION_TEST_DATABASE_URL'), 'Dedicated session test database not configured')
    def test_history_survives_server_process_replacement(self):
        value = session_database_url('SESSION_TEST_DATABASE_URL')
        url = make_url(value)
        self.assertEqual(url.drivername, 'postgresql+psycopg')
        self.assertTrue(url.database and url.database.endswith('_test'))
        for variable in ('DATABASE_URL', 'SESSION_DATABASE_URL', 'SESSION_SETUP_DATABASE_URL'):
            other = os.environ.get(variable)
            if other:
                self.assertNotEqual(url.database, make_url(other).database)
        environment = dict(os.environ, SESSION_DATABASE_URL=value, ADK_DISABLE_LOAD_DOTENV='true',
                           TRAVEL_TRACE_EXPORT='false', GOOGLE_API_KEY='offline-placeholder',
                           PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        identifier = str(uuid.uuid4())
        worker = Path(__file__).with_name('session_restart_worker.py')
        try:
            for action in ('write', 'read'):
                result = subprocess.run([sys.executable, '-B', str(worker), action, identifier],
                                        env=environment, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, 'Restart worker failed; inspect the dedicated test database privately.')
        finally:
            import psycopg
            with psycopg.connect(value.replace('postgresql+psycopg://', 'postgresql://', 1),
                                 options='-c search_path=adk_sessions') as connection:
                connection.execute('DELETE FROM sessions WHERE app_name=%s AND user_id=%s AND id=%s',
                                   ('restart_test', 'synthetic', identifier))
