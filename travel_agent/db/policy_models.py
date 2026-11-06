from datetime import datetime

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class PolicySection(Base):
    __tablename__ = "policy_sections"
    __table_args__ = (CheckConstraint("dimensions = 768"),)

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    section_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    heading: Mapped[str] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(String(64))
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(VECTOR(768))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActivePolicy(Base):
    __tablename__ = "active_policies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "version", "section_id"],
            ["policy_sections.document_id", "policy_sections.version", "policy_sections.section_id"],
            name="active_policy_ingested_section_fk",
        ),
    )

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    section_id: Mapped[str] = mapped_column(String(64))
