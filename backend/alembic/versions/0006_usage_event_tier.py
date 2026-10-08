"""usage_events.tier: which tier paid for a question, so BYO-key questions don't count toward caps

Revision ID: 0006_usage_event_tier
Revises: 0005_ask_request_state
Create Date: 2026-10-08

Phase D session 6 (EXTENSION_INTEGRATION.md SS6 /v1/usage, amended session 6). A question
asked on the caller's own Anthropic key costs this service nothing, so it shouldn't use up the
free daily or paid per-period allowance once the key is removed. New code writes tier
('free' | 'paid' | 'byo_key') when it inserts a question row (in_progress and rejected_*);
app/gating.py counts rows WHERE tier IS DISTINCT FROM 'byo_key'.

Additive only: one nullable Text column plus a CHECK constraint that NULL satisfies. Already-
deployed code keeps working after it runs -- it never maps the column, its inserts leave it
NULL, and NULL rows keep counting exactly as before. Text + CHECK rather than a native enum,
matching subscriptions.status's reasoning (db/models.py), and avoiding the ALTER TYPE
autocommit handling 0004/0005 needed.

Downgrade drops the constraint and the column. The tier values are lost, which only means
BYO-key rows count toward caps again -- the pre-0006 behavior.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_usage_event_tier"
down_revision: Union[str, None] = "0005_ask_request_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("tier", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_usage_events_tier",
        "usage_events",
        "tier IN ('free', 'paid', 'byo_key')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_usage_events_tier", "usage_events", type_="check")
    op.drop_column("usage_events", "tier")
