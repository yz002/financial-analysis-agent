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

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.main import app
from app.oauth_providers import ProviderIdentity
from db.base import get_session
from tests_support.network_guard import block_external_apis

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_real_stripe_or_anthropic_calls(request, monkeypatch):
    """
    Phase D session 6: backend/.env can hold a real Stripe test key and a real Anthropic key,
    so any unmocked call to either fails the test (tests_support/network_guard.py) -- raised
    at the call, and checked again here at teardown in case app code swallowed the error.
    Only a test marked @pytest.mark.real_stripe may reach Stripe; Anthropic is never allowed.
    """
    allow_stripe = request.node.get_closest_marker("real_stripe") is not None
    attempts = block_external_apis(monkeypatch, allow_stripe=allow_stripe)
    yield
    assert not attempts, f"real external API call(s) attempted: {attempts}"


@pytest.fixture(autouse=True)
def _app_logs_reach_caplog(caplog):
    """app/main.py gives the `app` logger its own stderr handler with propagate=False (so its
    lines are never duplicated), which also keeps app.* records away from pytest's root-level
    caplog handler. Attaching caplog's handler to `app` directly keeps caplog assertions working
    (test_billing.py's duplicate-subscription warning, test_google_data_token.py's no-logging
    check)."""
    app_logger = logging.getLogger("app")
    app_logger.addHandler(caplog.handler)
    yield
    app_logger.removeHandler(caplog.handler)


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

        if provider == "google":
            # Phase D session 2: Google's code-for-token exchange now happens
            # server-side (app/oauth_providers.py's exchange_google_code_for_token) --
            # mock it too, at the same boundary as verify_<provider>_token above, and
            # send the new request shape.
            monkeypatch.setattr(
                app_main.oauth_providers,
                "exchange_google_code_for_token",
                lambda code, code_verifier, redirect_uri: "fake-oauth-token",
            )
            payload = {
                "provider": provider,
                "code": "fake-code",
                "code_verifier": "fake-verifier",
                "redirect_uri": "https://fake-extension-id.chromiumapp.org/",
            }
        else:
            payload = {"provider": provider, "oauth_token": "t"}

        resp = client.post("/v1/auth/exchange", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        account_ids.append(body["account_id"])
        return body["account_id"], {"Authorization": f"Bearer {body['session_token']}"}

    return _make
