"""monetization tiers: subscriptions, stripe_webhook_events, drop free_window_started_at

Revision ID: 0002_monetization_tiers
Revises: 0001_initial_schema
Create Date: 2026-09-16

Phase B session 7a, implementing the monetization amendment's SS7.3 schema against the
three-tier model (BYO-key / paid subscription / recurring free daily cap) that replaced
the original 7-day-free-window design. Hand-written, matching 0001_initial_schema's own
style and its documented lesson about native Postgres enum types needing explicit
create/drop (see the usage_event_outcome swap below).

Three changes:
1. New subscriptions table (Stripe customer/subscription id, status stored as plain Text
   -- not a native enum, so a Stripe status this design didn't anticipate doesn't have to
   fit one -- current_period_start/end, cancel_at_period_end).
2. New stripe_webhook_events table -- an idempotency guard for session 7b's webhook
   handler; created now, empty, matching this project's pattern of standing up a table's
   schema in the session that designs it even before something populates it.
3. installs.free_window_started_at is dropped (superseded by the three-tier model), and
   usage_events.outcome's native enum swaps rejected_free_window for rejected_monthly_cap.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_monetization_tiers"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres native enums can't drop a value at all, and ALTER TYPE ... ADD VALUE can't
# safely share a transaction with using that value -- so upgrading/downgrading
# usage_event_outcome does a full type swap (rename the live type out of the way, create
# the target type under the original name, cast the column across, drop the renamed-away
# type) rather than an in-place ALTER TYPE. Both enum versions are declared here, matching
# 0001's own explicit-constant convention for every type this migration touches.
_usage_event_outcome_v1 = sa.Enum(
    "answered",
    "hit_iteration_cap",
    "error",
    "rejected_free_window",
    "rejected_daily_cap",
    name="usage_event_outcome",
)
_usage_event_outcome_v2 = sa.Enum(
    "answered",
    "hit_iteration_cap",
    "error",
    "rejected_daily_cap",
    "rejected_monthly_cap",
    name="usage_event_outcome",
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. subscriptions -- FK to installs.install_id.
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "install_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("installs.install_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stripe_customer_id", sa.Text(), nullable=False),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_unique_constraint("uq_subscriptions_install_id", "subscriptions", ["install_id"])
    op.create_unique_constraint(
        "uq_subscriptions_stripe_subscription_id", "subscriptions", ["stripe_subscription_id"]
    )
    op.create_index("ix_subscriptions_stripe_customer_id", "subscriptions", ["stripe_customer_id"])

    # 2. stripe_webhook_events -- no FKs, standalone idempotency guard.
    op.create_table(
        "stripe_webhook_events",
        sa.Column("stripe_event_id", sa.Text(), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )

    # 3. Drop the superseded 7-day-window column.
    op.drop_column("installs", "free_window_started_at")

    # 4. Swap usage_event_outcome: drop rejected_free_window, add rejected_monthly_cap.
    # Safe as a straight USING cast today since usage_events is empty in every environment
    # (nothing writes to it before this session) -- written as a real type-swap rather than
    # assuming that stays true, in case it doesn't by the time this migration runs.
    op.execute("ALTER TYPE usage_event_outcome RENAME TO usage_event_outcome_old")
    _usage_event_outcome_v2.create(bind, checkfirst=False)
    op.execute(
        "ALTER TABLE usage_events ALTER COLUMN outcome TYPE usage_event_outcome "
        "USING outcome::text::usage_event_outcome"
    )
    op.execute("DROP TYPE usage_event_outcome_old")


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse of upgrade step 4. Will fail the USING cast if any rejected_monthly_cap row
    # exists by the time this runs -- accepted, matching how enum-narrowing downgrades work
    # in general (no value in the target type to cast it to).
    op.execute("ALTER TYPE usage_event_outcome RENAME TO usage_event_outcome_new")
    _usage_event_outcome_v1.create(bind, checkfirst=False)
    op.execute(
        "ALTER TABLE usage_events ALTER COLUMN outcome TYPE usage_event_outcome "
        "USING outcome::text::usage_event_outcome"
    )
    op.execute("DROP TYPE usage_event_outcome_new")

    # Reverse of upgrade step 3. Re-added nullable, not NOT NULL like the original --
    # restoring the original NOT NULL constraint on a possibly-populated table would need
    # a backfill this migration doesn't attempt; a known, stated asymmetry rather than a
    # silently incomplete downgrade.
    op.add_column(
        "installs",
        sa.Column("free_window_started_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Reverse of upgrade step 2.
    op.drop_table("stripe_webhook_events")

    # Reverse of upgrade step 1.
    op.drop_index("ix_subscriptions_stripe_customer_id", table_name="subscriptions")
    op.drop_table("subscriptions")
