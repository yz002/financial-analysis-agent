"""
ORM models for the Sheets Add-on backend, matching the approved architecture
design's schema exactly (installs/usage_events/conversations/turns/byo_keys are
verbatim from the design doc's SS2.4/SS3.1/SS4; csv_statements is a concrete
elaboration of the design doc's SS1, which described it only conceptually -- see
this session's plan for the reasoning behind every column there).

This module defines Base.metadata (used by Alembic's env.py as the
autogenerate-comparison target) but the initial migration
(alembic/versions/0001_initial_schema.py) is hand-written, not generated from
these models, specifically so every column/type/constraint is a deliberate
choice rather than whatever autogenerate happens to infer. Keep the two in
sync by hand for now.

installs.byo_key_id and byo_keys.install_id are a circular foreign-key pair --
each table references the other. use_alter=True on installs.byo_key_id's
ForeignKey tells SQLAlchemy to defer that constraint to a post-creation ALTER
TABLE if Base.metadata.create_all() is ever invoked directly (e.g. in a test),
matching the same two-step ordering the hand-written migration uses.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

InstallIdentityType = Enum("google_email", "uuid", name="install_identity_type")
UsageEventOutcome = Enum(
    "answered",
    "hit_iteration_cap",
    "error",
    "rejected_free_window",
    "rejected_daily_cap",
    name="usage_event_outcome",
)
CsvStatementStatus = Enum("unconfirmed", "confirmed", name="csv_statement_status")


class Install(Base):
    __tablename__ = "installs"
    __table_args__ = (
        UniqueConstraint("identity_type", "identity_value", name="uq_installs_identity"),
    )

    install_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    identity_type: Mapped[str] = mapped_column(InstallIdentityType, nullable=False)
    identity_value: Mapped[str] = mapped_column(Text, nullable=False)
    free_window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Circular FK with byo_keys -- see module docstring. Deferred via use_alter in the
    # hand-written migration; use_alter=True here keeps metadata-driven creation consistent.
    byo_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("byo_keys.id", use_alter=True, name="fk_installs_byo_key_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    byo_keys: Mapped[list["ByoKey"]] = relationship(
        back_populates="install", foreign_keys="ByoKey.install_id"
    )


class ByoKey(Base):
    __tablename__ = "byo_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    install_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installs.install_id", ondelete="CASCADE"), nullable=False
    )
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    install: Mapped["Install"] = relationship(back_populates="byo_keys", foreign_keys=[install_id])


class CsvStatement(Base):
    __tablename__ = "csv_statements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    install_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installs.install_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(CsvStatementStatus, nullable=False, default="unconfirmed")
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_columns: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proposed_mapping: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confirmed_mapping: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    entity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    cadence: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    statement_attrs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    install_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installs.install_id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    csv_context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("csv_statements.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_turn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Turn(Base):
    __tablename__ = "turns"

    # This is the turn_id returned by POST /v1/ask in a later session.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    final_answer: Mapped[str] = mapped_column(Text, nullable=False)
    hit_iteration_cap: Mapped[bool] = mapped_column(Boolean, nullable=False)
    iterations_used: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    figure_check: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tool_calls: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        # Composite, not a single-column index on occurred_at alone -- the daily-cap query
        # (design doc SS2.2) always filters by install_id and a date range together.
        Index("ix_usage_events_install_id_occurred_at", "install_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    install_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installs.install_id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("turns.id"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(UsageEventOutcome, nullable=False)
