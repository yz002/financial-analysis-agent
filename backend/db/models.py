"""
ORM models for the Sheets Add-on backend, matching the approved architecture
design's schema exactly (usage_events/conversations/turns/byo_keys are
verbatim from the design doc's SS2.4/SS3.1/SS4; csv_statements is a concrete
elaboration of the design doc's SS1, which described it only conceptually -- see
this session's plan for the reasoning behind every column there). subscriptions/
stripe_webhook_events and the updated usage_event_outcome enum are from the
monetization amendment's SS7.3 (Phase B session 7a); installs.free_window_started_at,
from the amendment's superseded two-tier model, was dropped as of that session.

accounts/linked_identities/sessions are from the OAuth identity design doc's SS4
(Phase C session 1), replacing installs entirely: every account is now
provider-verified by construction, so the old identity_type/identity_value split
has nothing left to distinguish. Every install_id FK on byo_keys/subscriptions/
csv_statements/conversations/usage_events is renamed account_id and repointed at
accounts.id as part of the same session -- a rename-and-repoint, not a redesign of
those tables.

This module defines Base.metadata (used by Alembic's env.py as the
autogenerate-comparison target) but the initial migration
(alembic/versions/0001_initial_schema.py) is hand-written, not generated from
these models, specifically so every column/type/constraint is a deliberate
choice rather than whatever autogenerate happens to infer. Keep the two in
sync by hand for now.

accounts.byo_key_id and byo_keys.account_id are a circular foreign-key pair --
each table references the other. use_alter=True on accounts.byo_key_id's
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

UsageEventOutcome = Enum(
    "answered",
    "hit_iteration_cap",
    "error",
    "rejected_daily_cap",
    "rejected_monthly_cap",
    name="usage_event_outcome",
)
CsvStatementStatus = Enum("unconfirmed", "confirmed", name="csv_statement_status")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Informational/Stripe-prefill convenience only, set once at account creation from
    # whichever provider identity created it -- never a security boundary (sessions/account_id
    # are), so it's deliberately not kept in sync if a person's provider email later changes.
    primary_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Circular FK with byo_keys -- see module docstring. Deferred via use_alter in the
    # hand-written migration; use_alter=True here keeps metadata-driven creation consistent.
    byo_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("byo_keys.id", use_alter=True, name="fk_accounts_byo_key_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    byo_keys: Mapped[list["ByoKey"]] = relationship(
        back_populates="account", foreign_keys="ByoKey.account_id"
    )


class LinkedIdentity(Base):
    __tablename__ = "linked_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_subject", name="uq_linked_identities_provider_subject"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    # "google"|"microsoft" -- Text, not a native enum, matching subscriptions.status's own
    # rationale (design doc SS4/SS7.3): kept deliberately loose rather than a Postgres enum.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # The provider's own stable per-account identifier (Google's `sub`, Microsoft Graph's
    # `id`) -- the per-provider anti-duplicate key, since an account's own email is
    # technically mutable while this isn't (design doc SS3).
    provider_subject: Mapped[str] = mapped_column(Text, nullable=False)
    provider_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 of the opaque bearer token, never the plaintext -- mirrors why
    # byo_keys.encrypted_key isn't stored as plaintext: a DB dump alone shouldn't hand out
    # live sessions (design doc SS2).
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_via_provider: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Sliding-window expiry: extended on every successful validation, not an absolute cap
    # (design doc SS2).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ByoKey(Base):
    __tablename__ = "byo_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    account: Mapped["Account"] = relationship(back_populates="byo_keys", foreign_keys=[account_id])


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    stripe_customer_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Stripe's own status values, stored verbatim -- deliberately Text, not a native
    # Postgres enum (design doc SS7.3): a Stripe status this design didn't anticipate
    # shouldn't silently fail to fit an enum we'd have to migrate to extend.
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_events"

    # Stripe's own evt_... id -- the idempotency guard's natural key (session 7b writes
    # to this table; created now, empty, matching this project's pattern of standing up a
    # table in the session that designs its schema even before something populates it).
    stripe_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CsvStatement(Base):
    __tablename__ = "csv_statements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
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
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
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
        # (design doc SS2.2) always filters by account_id and a date range together.
        Index("ix_usage_events_account_id_occurred_at", "account_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("turns.id"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(UsageEventOutcome, nullable=False)
