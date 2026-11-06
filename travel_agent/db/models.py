from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Proposal(Base):
    __tablename__ = "proposals"

    __table_args__ = (
        UniqueConstraint("request_scope", "request_key", name="proposals_request_unique"),
        CheckConstraint("(request_scope IS NULL AND request_key IS NULL AND normalized_inputs IS NULL) OR "
                        "(request_scope IS NOT NULL AND request_key IS NOT NULL AND normalized_inputs IS NOT NULL)",
                        name="proposals_request_complete"),
    )

    request_scope: Mapped[str | None] = mapped_column(String(128))
    request_key: Mapped[str | None] = mapped_column(String(128))
    normalized_inputs: Mapped[dict | None] = mapped_column(JSONB)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    inputs: Mapped[dict] = mapped_column(JSONB)
    results: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("status IN ('NEEDS_REVIEW', 'SUBMITTED', 'REJECTED')"),
        CheckConstraint("(status = 'SUBMITTED') = (confirmation IS NOT NULL)"),
    )

    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="NEEDS_REVIEW")
    confirmation: Mapped[str | None] = mapped_column(String(41), unique=True)


class AuditRecord(Base):
    __tablename__ = "review_audit"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id"), unique=True)
    reviewer: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(20))
    confirmation: Mapped[str | None] = mapped_column(String(41))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
