import asyncio
import sys
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from google.adk.events import Event
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.genai import types

from travel_agent.server import create_app
from travel_agent.session_config import session_database_kwargs, session_database_url


async def write(session_id):
    async with DatabaseSessionService(session_database_url(), **session_database_kwargs()) as service:
        session = await service.get_session(app_name='restart_test', user_id='synthetic', session_id=session_id)
        assert session is not None
        for author, role, text in [('user', 'user', 'Synthetic question'), ('assistant', 'model', 'Synthetic answer')]:
            await service.append_event(session, Event(author=author, content=types.Content(
                role=role, parts=[types.Part(text=text)])))


with patch('travel_agent.server.configure_tracing', return_value=MagicMock()):
    app = create_app()
with TestClient(app) as client:
    if sys.argv[1] == 'write':
        response = client.post('/apps/restart_test/users/synthetic/sessions/' + sys.argv[2],
                               json={'marker': 'survives'})
        assert response.status_code == 200
        asyncio.run(write(sys.argv[2]))
    else:
        response = client.get('/apps/restart_test/users/synthetic/sessions/' + sys.argv[2])
        assert response.status_code == 200
        session = response.json()
        assert session['state']['marker'] == 'survives'
        assert [event['content']['parts'][0]['text'] for event in session['events']] == [
            'Synthetic question', 'Synthetic answer']
