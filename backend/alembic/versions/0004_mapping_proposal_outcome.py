"""mapping proposals: add usage_event_outcome 'mapping_proposal'

Revision ID: 0004_mapping_proposal_outcome
Revises: 0003_oauth_identity
Create Date: 2026-10-03

Phase D session 4. Every fresh model call made by POST /v1/csv/{id}/propose-mapping is now
recorded as a usage_events row with outcome 'mapping_proposal', so a per-account daily cap on
proposals can be counted from durable rows. Counting csv_statements rows instead would leak:
the retention cron hard-deletes expired unconfirmed rows about an hour after parse, which
would reset the count. 'mapping_proposal' is deliberately absent from app/gating.py's
_COUNTED_OUTCOMES, so it never counts toward the question caps.

Additive only: upgrade adds one enum value and touches no table, column, or existing row.
Already-deployed code keeps working after it runs -- that code only loads usage_events rows it
inserted itself (by id), and otherwise only counts rows with a question outcome in SQL, so it
never meets the new label.

ALTER TYPE ... ADD VALUE runs in an autocommit block: older Postgres versions refuse it inside
a transaction, and no version lets the same transaction use the new value, so it can't share
alembic's migration transaction safely whatever version production runs.

Downgrade can't simply drop the value (Postgres has no ALTER TYPE ... DROP VALUE). It deletes
the 'mapping_proposal' rows -- proposal-cap bookkeeping only, never question usage -- then does
0002's full type swap back to the previous value set. Stated plainly rather than left a no-op.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_mapping_proposal_outcome"
down_revision: Union[str, None] = "0003_oauth_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The value set before this migration (0002's v2), for the downgrade's type swap.
_usage_event_outcome_before = sa.Enum(
    "answered",
    "hit_iteration_cap",
    "error",
    "rejected_daily_cap",
    "rejected_monthly_cap",
    name="usage_event_outcome",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE usage_event_outcome ADD VALUE IF NOT EXISTS 'mapping_proposal'")


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DELETE FROM usage_events WHERE outcome = 'mapping_proposal'")
    op.execute("ALTER TYPE usage_event_outcome RENAME TO usage_event_outcome_new")
    _usage_event_outcome_before.create(bind, checkfirst=False)
    op.execute(
        "ALTER TABLE usage_events ALTER COLUMN outcome TYPE usage_event_outcome "
        "USING outcome::text::usage_event_outcome"
    )
    op.execute("DROP TYPE usage_event_outcome_new")
