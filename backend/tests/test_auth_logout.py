"""
Tests for Phase C session 6's revocation endpoints (design doc SS2's "Revocation" subsection):
POST /v1/auth/logout revokes only the presented token's own sessions row; POST
/v1/auth/sessions/revoke-all revokes every sessions row for the caller's account_id, including
the one making the revoke-all request itself.

Like the other backend integration tests, these hit a real reachable Postgres via
DATABASE_URL (backend/.env). GET /v1/usage is used as the "any authenticated route" probe,
matching test_get_current_account.py's own convention. Multiple "devices" on one account are
simulated by calling auth_session() more than once with the same subject/email, which
_resolve_account_for_identity resolves back to the same account_id (see test_auth_exchange.py).
"""

import hashlib
import secrets
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from db.base import get_session

client = TestClient(app)


def _revoked_at(headers: dict) -> datetime | None:
    """Reads sessions.revoked_at for the session backing the given auth headers."""
    raw_token = headers["Authorization"].removeprefix("Bearer ")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    session = get_session()
    try:
        return session.execute(
            text("SELECT revoked_at FROM sessions WHERE token_hash = :h"), {"h": token_hash}
        ).scalar_one()
    finally:
        session.close()


def test_logout_revokes_only_presented_session(auth_session):
    subject = "logout-device-test"
    email = "logout-device-test@example.com"
    _, headers_a = auth_session(subject=subject, email=email)
    _, headers_b = auth_session(subject=subject, email=email)

    resp = client.post("/v1/auth/logout", headers=headers_a)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True}

    assert _revoked_at(headers_a) is not None
    assert _revoked_at(headers_b) is None

    # The revoked token 401s on the very next request against any authenticated route.
    resp = client.get("/v1/usage", headers=headers_a)
    assert resp.status_code == 401, resp.text

    # The other device's session is completely untouched.
    resp = client.get("/v1/usage", headers=headers_b)
    assert resp.status_code == 200, resp.text


def test_revoke_all_revokes_every_session_including_caller(auth_session):
    subject = "revoke-all-device-test"
    email = "revoke-all-device-test@example.com"
    _, headers_1 = auth_session(subject=subject, email=email)
    _, headers_2 = auth_session(subject=subject, email=email)
    _, headers_3 = auth_session(subject=subject, email=email)

    # revoke-all called from device 2 -- must invalidate device 2's own token too.
    resp = client.post("/v1/auth/sessions/revoke-all", headers=headers_2)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True}

    for headers in (headers_1, headers_2, headers_3):
        resp = client.get("/v1/usage", headers=headers)
        assert resp.status_code == 401, resp.text


def test_revoke_all_does_not_touch_other_accounts(auth_session):
    owner_subject = "revoke-all-owner"
    owner_email = "revoke-all-owner@example.com"
    _, owner_headers_1 = auth_session(subject=owner_subject, email=owner_email)
    _, owner_headers_2 = auth_session(subject=owner_subject, email=owner_email)
    _, other_headers = auth_session()

    resp = client.post("/v1/auth/sessions/revoke-all", headers=owner_headers_1)
    assert resp.status_code == 200, resp.text

    resp = client.get("/v1/usage", headers=owner_headers_1)
    assert resp.status_code == 401, resp.text
    resp = client.get("/v1/usage", headers=owner_headers_2)
    assert resp.status_code == 401, resp.text

    # A different account's session is untouched by another account's revoke-all.
    resp = client.get("/v1/usage", headers=other_headers)
    assert resp.status_code == 200, resp.text


def test_logout_requires_valid_token():
    resp = client.post(
        "/v1/auth/logout", headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"}
    )
    assert resp.status_code == 401, resp.text

    resp = client.post("/v1/auth/logout", headers={"Authorization": "not-a-bearer-token"})
    assert resp.status_code == 401, resp.text


def test_revoke_all_requires_valid_token():
    resp = client.post(
        "/v1/auth/sessions/revoke-all",
        headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"},
    )
    assert resp.status_code == 401, resp.text

    resp = client.post(
        "/v1/auth/sessions/revoke-all", headers={"Authorization": "not-a-bearer-token"}
    )
    assert resp.status_code == 401, resp.text
