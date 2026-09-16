"""
Tests for Phase B session 9: the retention job (design doc SS3.4's 12-month-
inactivity conversation purge, and SS1's short-TTL csv_statements cleanup).

Like the other backend integration tests, these hit a real reachable Postgres
via DATABASE_URL (backend/.env) -- rows are seeded directly via get_session(),
never through the HTTP API, matching test_billing.py's/
test_conversation_history.py's own convention.

Seeding, purging, and asserting each use their own fresh get_session() call
(closing the previous one first) rather than reusing one long-lived session --
reusing a session that already holds a since-bulk-deleted row in its identity
map makes SQLAlchemy raise ObjectDeletedError on a post-commit session.get()
instead of returning None, since a bulk DELETE (used by
scripts.retention_cleanup, deliberately, to stay a single idempotent
statement) bypasses the ORM's own delete-tracking. A brand-new session has no
such stale expectation and just reports "not found" normally.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import text

from db.base import get_session
from db.models import ByoKey, Conversation, CsvStatement, Install, Turn, UsageEvent
from scripts.retention_cleanup import purge_expired_csv_statements, purge_inactive_conversations

NOW = datetime.now(timezone.utc)


@pytest.fixture
def install_ids():
    """Tracks install_ids created by a test; deletes them (cascading to everything) after."""
    ids: list[str] = []
    yield ids
    if not ids:
        return
    session = get_session()
    try:
        session.execute(
            text("DELETE FROM installs WHERE install_id = ANY(:ids)"),
            {"ids": ids},
        )
        session.commit()
    finally:
        session.close()


def _new_install_id(install_ids: list[str]) -> str:
    install_id = str(uuid.uuid4())
    install_ids.append(install_id)
    return install_id


def _seed_install(session, install_id: str) -> Install:
    install = Install(
        install_id=uuid.UUID(install_id),
        identity_type="uuid",
        identity_value=install_id,
        last_seen_at=NOW,
    )
    session.add(install)
    session.flush()
    return install


def _seed_conversation_with_turn(session, install: Install, last_turn_at: datetime) -> uuid.UUID:
    conversation = Conversation(
        install_id=install.install_id,
        title="test conversation",
        last_turn_at=last_turn_at,
    )
    session.add(conversation)
    session.flush()
    session.add(
        Turn(
            conversation_id=conversation.id,
            question="A question.",
            final_answer="An answer.",
            hit_iteration_cap=False,
            iterations_used=1,
            stop_reason="end_turn",
            figure_check={},
            tool_calls=[],
            model="claude-sonnet-5",
            created_at=last_turn_at,
        )
    )
    return conversation.id


def _seed_csv_statement(session, install: Install, status: str, expires_at: datetime | None) -> uuid.UUID:
    row = CsvStatement(
        install_id=install.install_id,
        status=status,
        filename="test.csv",
        uploaded_at=NOW,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return row.id


# --- purge_inactive_conversations: boundary -------------------------------------------------


def test_purge_inactive_conversations_respects_boundary(install_ids):
    recent_id = _new_install_id(install_ids)
    boundary_id = _new_install_id(install_ids)
    dormant_id = _new_install_id(install_ids)

    session = get_session()
    try:
        recent = _seed_install(session, recent_id)
        boundary = _seed_install(session, boundary_id)
        dormant = _seed_install(session, dormant_id)
        recent_conv_id = _seed_conversation_with_turn(session, recent, NOW - relativedelta(months=11))
        boundary_conv_id = _seed_conversation_with_turn(session, boundary, NOW - relativedelta(months=12))
        dormant_conv_id = _seed_conversation_with_turn(session, dormant, NOW - relativedelta(months=13))
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        conversations_deleted, turns_deleted = purge_inactive_conversations(session, NOW)
        session.commit()
    finally:
        session.close()
    assert conversations_deleted == 1
    assert turns_deleted == 1

    session = get_session()
    try:
        assert session.get(Conversation, recent_conv_id) is not None
        # Exactly at the boundary is retained -- strict "<", conservative by design.
        assert session.get(Conversation, boundary_conv_id) is not None
        assert session.get(Conversation, dormant_conv_id) is None
        assert (
            session.execute(
                text("SELECT COUNT(*) FROM turns WHERE conversation_id = :id"),
                {"id": dormant_conv_id},
            ).scalar()
            == 0
        )
    finally:
        session.close()


def test_purge_inactive_conversations_evaluates_whole_install_not_per_conversation(install_ids):
    install_id = _new_install_id(install_ids)

    session = get_session()
    try:
        install = _seed_install(session, install_id)
        stale_conv_id = _seed_conversation_with_turn(session, install, NOW - relativedelta(months=14))
        recent_conv_id = _seed_conversation_with_turn(session, install, NOW - relativedelta(months=1))
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        conversations_deleted, turns_deleted = purge_inactive_conversations(session, NOW)
        session.commit()
    finally:
        session.close()

    # The install's most recent activity is 1 month ago -- not dormant -- so
    # neither conversation is purged, even the individually-stale one.
    assert conversations_deleted == 0
    assert turns_deleted == 0

    session = get_session()
    try:
        assert session.get(Conversation, stale_conv_id) is not None
        assert session.get(Conversation, recent_conv_id) is not None
    finally:
        session.close()


def test_purge_inactive_conversations_idempotent_on_repeated_run(install_ids):
    install_id = _new_install_id(install_ids)

    session = get_session()
    try:
        install = _seed_install(session, install_id)
        _seed_conversation_with_turn(session, install, NOW - relativedelta(months=13))
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        first = purge_inactive_conversations(session, NOW)
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        second = purge_inactive_conversations(session, NOW)
        session.commit()
    finally:
        session.close()

    assert first == (1, 1)
    assert second == (0, 0)


def test_purge_does_not_delete_install_row_or_unrelated_tables(install_ids):
    install_id = _new_install_id(install_ids)

    session = get_session()
    try:
        install = _seed_install(session, install_id)
        _seed_conversation_with_turn(session, install, NOW - relativedelta(months=13))
        session.add(
            UsageEvent(install_id=install.install_id, occurred_at=NOW - relativedelta(months=13), outcome="answered")
        )
        session.add(ByoKey(install_id=install.install_id, encrypted_key=b"not-real", is_active=True))
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        purge_inactive_conversations(session, NOW)
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        assert session.get(Install, uuid.UUID(install_id)) is not None
        assert (
            session.execute(
                text("SELECT COUNT(*) FROM usage_events WHERE install_id = :id"),
                {"id": install_id},
            ).scalar()
            == 1
        )
        assert (
            session.execute(
                text("SELECT COUNT(*) FROM byo_keys WHERE install_id = :id"),
                {"id": install_id},
            ).scalar()
            == 1
        )
    finally:
        session.close()


# --- purge_expired_csv_statements -----------------------------------------------------------


def test_purge_expired_csv_statements(install_ids):
    install_id = _new_install_id(install_ids)

    session = get_session()
    try:
        install = _seed_install(session, install_id)
        expired_id = _seed_csv_statement(session, install, "unconfirmed", NOW - timedelta(hours=1))
        not_yet_expired_id = _seed_csv_statement(session, install, "unconfirmed", NOW + timedelta(hours=1))
        confirmed_id = _seed_csv_statement(session, install, "confirmed", None)
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        first_count = purge_expired_csv_statements(session, NOW)
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        second_count = purge_expired_csv_statements(session, NOW)
        session.commit()
    finally:
        session.close()

    assert first_count == 1
    assert second_count == 0

    session = get_session()
    try:
        assert session.get(CsvStatement, expired_id) is None
        assert session.get(CsvStatement, not_yet_expired_id) is not None
        assert session.get(CsvStatement, confirmed_id) is not None
    finally:
        session.close()
