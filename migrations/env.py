import os

from alembic import context

from travel_agent.db.models import Base
from travel_agent.db.policy_models import PolicySection
from travel_agent.db.session import create_session_factory


def run_migrations():
    if context.is_offline_mode():
        context.configure(url=os.environ["DATABASE_URL"], target_metadata=Base.metadata,
                          literal_binds=True, dialect_opts={"paramstyle": "named"})
        with context.begin_transaction():
            context.run_migrations()
        return
    factory = create_session_factory()
    engine = factory.kw["bind"]
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
