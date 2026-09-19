"""
Shared pytest fixtures for the backend integration suite (Phase C session 4). Every test file
used to duplicate its own install_ids/_new_install_id fixture pair; since every authenticated
route now requires a real sessions-table token (get_current_account, app/main.py), that duplicated
X-Install-Id plumbing is replaced by two fixtures here:

- account_ids: tracks account ids created by a test and deletes them (cascading to
  linked_identities/sessions/byo_keys/subscriptions/csv_statements/conversations/usage_events via
  each table's ondelete=CASCADE) on teardown -- the exact pattern test_auth_exchange.py already
  wrote for itself in session 3, promoted here so every other file can share it.
- auth_session: performs a REAL POST /v1/auth/exchange call (never a shortcut that inserts a
  sessions row directly) by monkeypatching app.oauth_providers.verify_<provider>_token to return a
  fake ProviderIdentity, matching test_auth_exchange.py's own mocking boundary (session 2 already
  tested the underlying HTTP calls to Google/Microsoft). Returns (account_id, auth_headers) with
  auth_headers ready to pass straight to TestClient as `headers=`.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.main import app
from app.oauth_providers import ProviderIdentity
from db.base import get_session

client = TestClient(app)


@pytest.fixture
def account_ids():
    """Tracks account_ids created by a test; deletes them (cascading to every FK'd table) after."""
    ids: list[str] = []
    yield ids
    if not ids:
        return
    session = get_session()
    try:
        session.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": ids})
        session.commit()
    finally:
        session.close()


@pytest.fixture
def auth_session(monkeypatch, account_ids):
    def _make(
        subject: str | None = None, email: str | None = None, provider: str = "google"
    ) -> tuple[str, dict]:
        subject = subject or f"sub-{uuid.uuid4()}"
        email = email or f"{subject}@example.com"
        identity = ProviderIdentity(provider=provider, subject=subject, email=email)
        verify_attr = "verify_google_token" if provider == "google" else "verify_microsoft_token"
        monkeypatch.setattr(app_main.oauth_providers, verify_attr, lambda oauth_token: identity)

        resp = client.post("/v1/auth/exchange", json={"provider": provider, "oauth_token": "t"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        account_ids.append(body["account_id"])
        return body["account_id"], {"Authorization": f"Bearer {body['session_token']}"}

    return _make
