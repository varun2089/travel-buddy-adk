import asyncio
import logging
import sys

from .session_config import session_database_kwargs, session_database_url


async def prepare(url):
    from google.adk.sessions.database_session_service import DatabaseSessionService

    service = DatabaseSessionService(url, **session_database_kwargs())
    try:
        await service.prepare_tables()
    finally:
        await service.close()


def main():
    try:
        url = session_database_url("SESSION_SETUP_DATABASE_URL")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    previous = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        asyncio.run(prepare(url))
    except Exception:
        print("Session setup failed. Check credentials, schema and database state privately.", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous)
    print("ADK session tables and indexes prepared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
