"""
Phase B session 9: the retention job (design doc SS3.4's 12-month-inactivity
conversation purge, and SS1's short-TTL cleanup of unconfirmed csv_statements
rows). Meant to run as a Render Cron Job -- a separate scheduled resource,
decoupled from the web service, invoked directly as a command rather than
over HTTP (see this session's plan for why). Run from inside backend/:

    backend/.venv/Scripts/python.exe -m scripts.retention_cleanup

with DATABASE_URL set (backend/.env) so it reaches the same Postgres instance
as the web service.

Activity signal for the 12-month purge is each account's own conversation/turn
activity (MAX(conversations.last_turn_at)), not accounts.last_seen_at --
last_seen_at is touched by every authenticated endpoint, including read-only
ones like GET /v1/usage, so it would keep a dormant account's stale
conversation content alive indefinitely just because the sidebar still pings
that endpoint. See this session's plan for the full reasoning, including why
the accounts row itself (and byo_keys/subscriptions/usage_events/
csv_statements) are deliberately left untouched by this task -- SS3.4 scopes
the purge to "Conversations (and their turns)" only.

Both cleanup tasks are plain DELETE ... WHERE <condition on current row
state> with no read-modify-write gap, so they're safe to run repeatedly or
concurrently with themselves: two overlapping invocations racing on the same
rows are serialized by Postgres's own row-level locking, and whichever
commits second simply matches zero rows for anything the first already
deleted -- no error, no double-delete.
"""

import logging
import sys
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, func, select

from db.base import get_session
from db.models import Conversation, CsvStatement, Turn

# No logging framework/handler config exists yet in this project (see
# app/main.py's own logger docstring) -- this module-level logger relies on
# that same convention. Unlike main.py's FastAPI process, this script's own
# __main__ block below adds a minimal logging.basicConfig call, since a Cron
# Job run has no other handler and its whole point is producing an
# inspectable log of what was deleted.
logger = logging.getLogger(__name__)

INACTIVITY_MONTHS = 12


def purge_inactive_conversations(session, now: datetime) -> tuple[int, int]:
    """
    Hard-deletes conversations (and, via turns.conversation_id's existing
    ON DELETE CASCADE, their turns) for every account whose most recent
    conversation activity is more than INACTIVITY_MONTHS months old.
    Evaluated per account, not per conversation -- an account with one stale
    and one recent conversation keeps both, since the account itself isn't
    dormant. Returns (conversations_deleted, turns_deleted).
    """
    cutoff = now - relativedelta(months=INACTIVITY_MONTHS)

    dormant_account_ids = (
        session.execute(
            select(Conversation.account_id)
            .group_by(Conversation.account_id)
            .having(func.max(Conversation.last_turn_at) < cutoff)
        )
        .scalars()
        .all()
    )
    if not dormant_account_ids:
        return 0, 0

    turns_deleted = session.execute(
        select(func.count())
        .select_from(Turn)
        .join(Conversation, Turn.conversation_id == Conversation.id)
        .where(Conversation.account_id.in_(dormant_account_ids))
    ).scalar_one()

    result = session.execute(
        delete(Conversation).where(Conversation.account_id.in_(dormant_account_ids))
    )
    return result.rowcount, turns_deleted


def purge_expired_csv_statements(session, now: datetime) -> int:
    """
    Hard-deletes csv_statements rows still sitting in the disposable
    unconfirmed state (design doc SS1) whose ~1-hour expires_at has passed.
    Confirmed rows never have this deleted -- confirm_mapping (app/main.py)
    sets expires_at=None on confirmation, so a confirmed row's expires_at is
    always NULL and never matches this filter.
    """
    result = session.execute(
        delete(CsvStatement).where(
            CsvStatement.status == "unconfirmed",
            CsvStatement.expires_at.isnot(None),
            CsvStatement.expires_at < now,
        )
    )
    return result.rowcount


def main() -> int:
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        conversations_deleted, turns_deleted = purge_inactive_conversations(session, now)
        csv_statements_deleted = purge_expired_csv_statements(session, now)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("retention_cleanup failed")
        raise
    finally:
        session.close()

    logger.info(
        "retention_cleanup: purged %d dormant conversations (%d turns); "
        "purged %d expired unconfirmed csv_statements rows",
        conversations_deleted,
        turns_deleted,
        csv_statements_deleted,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
