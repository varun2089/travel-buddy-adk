import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


def create_session_factory(database_url=None):
    value = database_url or os.environ.get("DATABASE_URL")
    if not value:
        raise ValueError("Set DATABASE_URL to a PostgreSQL database URL.")
    try:
        url = make_url(value)
    except Exception:
        raise ValueError("DATABASE_URL is invalid.") from None
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Use the postgresql+psycopg driver in DATABASE_URL.")
    engine = create_engine(url, echo=False, hide_parameters=True)
    return sessionmaker(engine, expire_on_commit=False)
