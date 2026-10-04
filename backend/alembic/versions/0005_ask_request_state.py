"""ask request state: request_id replay, in_progress outcome, stored citations, statement binding

Revision ID: 0005_ask_request_state
Revises: 0004_mapping_proposal_outcome
Create Date: 2026-10-04

Phase D session 5 (EXTENSION_INTEGRATION.md SS6 /v1/ask, amended session 5):
- usage_event_outcome 'in_progress': the explicit state of a question that's running. It
  replaces the old 'answered'-with-no-turn placeholder, and counts toward the question caps
  while it runs.
- usage_events.request_id / request_fingerprint: the caller's per-question id, and a hash of
  the request it was first sent with, so a replay never runs or charges twice and a reused id
  with a different body is refused. The partial unique index makes (account_id, request_id)
  the lookup key. Rejected (429) rows never carry a request_id.
- turns.citations: the citations computed when the answer was produced, returned verbatim on
  replay rather than rebuilt.
- conversations.bound_csv_context_id: the statement a conversation was bound to at creation.

Additive only: one enum value, four nullable columns, one partial unique index. Already-
deployed code keeps working after it runs -- it maps none of the new columns (its inserts
simply omit them), never writes or reads 'in_progress', and counts only its own question
outcomes in SQL.

ALTER TYPE ... ADD VALUE runs in an autocommit block (same reason as 0004: older Postgres
refuses it inside a transaction, and no version lets the same transaction use the new value).
So it commits on its own: if a later step of this upgrade fails, 'in_progress' stays in the
enum while the rest rolls back. That's harmless -- no deployed code uses the value -- and
IF NOT EXISTS makes re-running the upgrade safe.

Downgrade: Postgres has no ALTER TYPE ... DROP VALUE. Any 'in_progress' rows (counted question
attempts) are remapped to 'error' -- also a counted outcome, so cap totals don't change --
then the same full type swap as 0004's downgrade restores the 0004 value set.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_ask_request_state"
down_revision: Union[str, None] = "0004_mapping_proposal_outcome"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The value set before this migration (0004's), for the downgrade's type swap.
_usage_event_outcome_before = sa.Enum(
    "answered",
    "hit_iteration_cap",
    "error",
    "rejected_daily_cap",
    "rejected_monthly_cap",
    "mapping_proposal",
    name="usage_event_outcome",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE usage_event_outcome ADD VALUE IF NOT EXISTS 'in_progress'")
    op.add_column(
        "usage_events", sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("usage_events", sa.Column("request_fingerprint", sa.Text(), nullable=True))
    op.create_index(
        "uq_usage_events_account_request",
        "usage_events",
        ["account_id", "request_id"],
        unique=True,
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )
    op.add_column("turns", sa.Column("citations", postgresql.JSONB(), nullable=True))
    # No foreign key on purpose: unlike csv_context_id (ON DELETE SET NULL), this has to keep
    # recording that the conversation was bound even if the statement row is later deleted.
    # It's only ever read through the account-scoped, confirmed-only statement loader.
    op.add_column(
        "conversations",
        sa.Column("bound_csv_context_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_column("conversations", "bound_csv_context_id")
    op.drop_column("turns", "citations")
    op.drop_index("uq_usage_events_account_request", table_name="usage_events")
    op.drop_column("usage_events", "request_fingerprint")
    op.drop_column("usage_events", "request_id")
    op.execute("UPDATE usage_events SET outcome = 'error' WHERE outcome = 'in_progress'")
    op.execute("ALTER TYPE usage_event_outcome RENAME TO usage_event_outcome_new")
    _usage_event_outcome_before.create(bind, checkfirst=False)
    op.execute(
        "ALTER TABLE usage_events ALTER COLUMN outcome TYPE usage_event_outcome "
        "USING outcome::text::usage_event_outcome"
    )
    op.execute("DROP TYPE usage_event_outcome_new")
