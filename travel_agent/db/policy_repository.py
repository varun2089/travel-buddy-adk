from sqlalchemy import and_, create_engine, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
import os

from travel_agent.policy.config import DIMENSIONS, MODEL, TOP_K, identifier
from travel_agent.policy.embeddings import normalize_vector


class VersionConflict(ValueError):
    pass


def create_policy_session_factory(database_url=None):
    url = make_url(database_url or os.environ.get("DATABASE_URL", ""))
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Configure a PostgreSQL Psycopg database URL.")
    engine = create_engine(url, echo=False, hide_parameters=True,
                           connect_args={"connect_timeout": 5,
                                         "options": "-c statement_timeout=5000 -c lock_timeout=5000"},
                           pool_timeout=5)
    return sessionmaker(engine, expire_on_commit=False)


def ingest_sections(factory, sections, source_path, embedder):
    from .policy_models import PolicySection

    first = sections[0]
    with factory.begin() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": first.document_id + "/" + first.version})
        existing = session.scalars(select(PolicySection).where(
            PolicySection.document_id == first.document_id,
            PolicySection.version == first.version)).all()
        expected = {section.section_id: section.content_hash for section in sections}
        if existing:
            actual = {row.section_id: row.content_hash for row in existing}
            if actual != expected or any(row.embedding_model != MODEL or row.dimensions != DIMENSIONS
                                         for row in existing):
                raise VersionConflict("Existing version differs; create a new version.")
            return {"status": "ok", "inserted": 0, "unchanged": len(existing)}
        for section in sections:
            vector = normalize_vector(embedder(section.heading + "\n" + section.text,
                                               "RETRIEVAL_DOCUMENT"))
            session.add(PolicySection(
                document_id=section.document_id, version=section.version,
                section_id=section.section_id, heading=section.heading, text=section.text,
                source_path=source_path, content_hash=section.content_hash,
                embedding_model=MODEL, dimensions=DIMENSIONS, embedding=vector))
        return {"status": "ok", "inserted": len(sections), "unchanged": 0}


def list_policies(factory):
    from .policy_models import ActivePolicy, PolicySection

    statement = select(
        PolicySection.document_id, PolicySection.version,
        func.count().label("sections"),
        ActivePolicy.document_id.is_not(None).label("active"),
    ).outerjoin(ActivePolicy, and_(
        ActivePolicy.document_id == PolicySection.document_id,
        ActivePolicy.version == PolicySection.version,
    )).group_by(PolicySection.document_id, PolicySection.version, ActivePolicy.document_id).order_by(
        PolicySection.document_id, PolicySection.version)
    with factory.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        return [dict(row) for row in session.execute(statement).mappings()]


def activate_policy(factory, document_id, version):
    from .policy_models import ActivePolicy, PolicySection

    identifier(document_id)
    identifier(version)
    with factory.begin() as session:
        section_id = session.scalar(select(PolicySection.section_id).where(
            PolicySection.document_id == document_id, PolicySection.version == version,
        ).order_by(PolicySection.section_id).limit(1).with_for_update(read=True))
        if section_id is None:
            raise ValueError("Policy version has not been ingested.")
        statement = insert(ActivePolicy).values(
            document_id=document_id, version=version, section_id=section_id)
        session.execute(statement.on_conflict_do_update(
            index_elements=[ActivePolicy.document_id],
            set_={"version": version, "section_id": section_id},
        ))
    return {"status": "ok", "document_id": document_id, "version": version, "active": True}


def deactivate_policy(factory, document_id):
    from .policy_models import ActivePolicy

    identifier(document_id)
    with factory.begin() as session:
        session.execute(delete(ActivePolicy).where(ActivePolicy.document_id == document_id))
    return {"status": "ok", "document_id": document_id, "active": False}


def has_active_policies(factory):
    from .policy_models import ActivePolicy

    with factory.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        return session.scalar(select(ActivePolicy.document_id).limit(1)) is not None


def nearest_sections(factory, vector):
    from .policy_models import ActivePolicy, PolicySection

    vector = normalize_vector(vector)
    distance = PolicySection.embedding.cosine_distance(vector).label("distance")
    statement = select(PolicySection, distance).join(ActivePolicy, and_(
        ActivePolicy.document_id == PolicySection.document_id,
        ActivePolicy.version == PolicySection.version,
    )).where(
        PolicySection.embedding_model == MODEL, PolicySection.dimensions == DIMENSIONS,
    ).order_by(distance, PolicySection.document_id, PolicySection.version, PolicySection.section_id).limit(TOP_K)
    with factory.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL enable_indexscan = off"))
        session.execute(text("SET LOCAL enable_bitmapscan = off"))
        return [dict(document_id=row.document_id, version=row.version,
                     section_id=row.section_id, heading=row.heading, text=row.text,
                     distance=float(score)) for row, score in session.execute(statement)]
