import os

from sqlalchemy.engine import make_url


def session_database_url(variable="SESSION_DATABASE_URL"):
    value = os.environ.get(variable, "")
    try:
        url = make_url(value)
        url.port
        if (url.drivername != "postgresql+psycopg" or not url.database
                or not url.username or url.query.get("options") is not None
                or "service" in url.query or "servicefile" in url.query
                or any(c in value for c in "\r\n\x00")):
            raise ValueError
    except Exception:
        raise ValueError(f"Set {variable} to a valid postgresql+psycopg URL without options or service overrides.") from None
    return value


def session_database_kwargs():
    return {
        "echo": False,
        "hide_parameters": True,
        "connect_args": {"options": "-c search_path=adk_sessions"},
    }
