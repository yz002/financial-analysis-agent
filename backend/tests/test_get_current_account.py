"""
Tests for Phase C session 4's get_current_account dependency (design doc SS2): parses
Authorization: Bearer <token>, validates against a live (not expired, not revoked) sessions
row, and on success extends expires_at (sliding window, not an absolute cap) and updates
last_used_at.

Like the other backend integration tests, these hit a real reachable Postgres via
DATABASE_URL (backend/.env). GET /v1/usage is used as the "any authenticated route" probe
throughout -- it's the cheapest authenticated route to call (no CSV/Anthropic side effects to
mock) and its response body conveniently echoes account_id.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from db.base import get_session
from db.models import Account
from db.models import Session as SessionModel

client = TestClient(app)


def _seed_session_row(
    session, account_id: uuid.UUID, *, expires_at: datetime, revoked_at: datetime | None = None
) -> str:
    """Seeds a sessions row directly (bypassing /v1/auth/exchange) so a specific
    expires_at/revoked_at can be controlled precisely. Returns the raw bearer token."""
    raw_token = secrets.token_urlsafe(32)
    session.add(
        SessionModel(
            account_id=account_id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            created_via_provider="google",
            last_used_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            revoked_at=revoked_at,
        )
    )
    session.flush()
    return raw_token


def _seed_bare_account(session, account_ids: list[str]) -> uuid.UUID:
    account = Account(id=uuid.uuid4(), last_seen_at=datetime.now(timezone.utc))
    session.add(account)
    session.flush()
    account_ids.append(str(account.id))
    return account.id


def test_valid_token_succeeds_and_resolves_right_account(auth_session):
    account_id, headers = auth_session()
    resp = client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] == account_id


def test_expired_token_401s(account_ids):
    session = get_session()
    try:
        account_id = _seed_bare_account(session, account_ids)
        now = datetime.now(timezone.utc)
        raw_token = _seed_session_row(session, account_id, expires_at=now - timedelta(minutes=1))
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers={"Authorization": f"Bearer {raw_token}"})
    assert resp.status_code == 401, resp.text


def test_revoked_token_401s(account_ids):
    session = get_session()
    try:
        account_id = _seed_bare_account(session, account_ids)
        now = datetime.now(timezone.utc)
        # Not yet expired, but revoked -- must still 401, indistinguishable from expired/unknown.
        raw_token = _seed_session_row(
            session, account_id, expires_at=now + timedelta(days=90), revoked_at=now
        )
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers={"Authorization": f"Bearer {raw_token}"})
    assert resp.status_code == 401, resp.text


def test_unknown_token_401s():
    resp = client.get("/v1/usage", headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"})
    assert resp.status_code == 401, resp.text


def test_missing_bearer_prefix_401s():
    resp = client.get("/v1/usage", headers={"Authorization": "not-a-bearer-token"})
    assert resp.status_code == 401, resp.text


def test_valid_token_expires_at_advances_on_use(auth_session):
    _, headers = auth_session()
    raw_token = headers["Authorization"].removeprefix("Bearer ")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    session = get_session()
    try:
        before = session.execute(
            text("SELECT expires_at FROM sessions WHERE token_hash = :h"), {"h": token_hash}
        ).scalar_one()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        after = session.execute(
            text("SELECT expires_at FROM sessions WHERE token_hash = :h"), {"h": token_hash}
        ).scalar_one()
    finally:
        session.close()

    # Confirmed via a direct DB query, not just that the request succeeded -- the sliding
    # window (design doc SS2) must actually advance expires_at on every successful validation.
    assert after > before
