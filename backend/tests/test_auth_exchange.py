"""
Tests for Phase C session 3: POST /v1/auth/exchange -- provider-token verification
(mocked at the app.oauth_providers call site, matching test_oauth_providers.py's own
scope boundary, since session 2 already tested the underlying HTTP calls), design doc
SS3's account-resolution order (provider+subject match, else a new account -- email never
selects one, per the 2026-09-29 amendment), and opaque session-token issuance/hashing.

Like the other backend integration tests, these hit a real reachable Postgres via
DATABASE_URL (backend/.env) -- not a mocked DB.
"""

import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.main import app
from app.oauth_providers import (
    InvalidProviderTokenError,
    OAuthProviderConfigError,
    ProviderEmailUnavailableError,
    ProviderIdentity,
)
from db.base import get_session

client = TestClient(app)

# account_ids fixture now lives in conftest.py (Phase C session 4), shared across every test
# file that authenticates through a real /v1/auth/exchange call.

# Phase D session 2: Google's code-for-token exchange now happens server-side
# (app/oauth_providers.py's exchange_google_code_for_token), so every test that signs in via
# Google mocks it too, at the same boundary as _fake_verify below, and posts the new
# code/code_verifier/redirect_uri request shape instead of a pre-exchanged oauth_token.
_GOOGLE_CODE_BODY = {
    "provider": "google",
    "code": "fake-code",
    "code_verifier": "fake-verifier",
    "redirect_uri": "https://fake-extension-id.chromiumapp.org/",
}


def _fake_verify(identity: ProviderIdentity):
    return lambda oauth_token: identity


def _fake_exchange(oauth_token: str = "fake-oauth-token"):
    return lambda code, code_verifier, redirect_uri: oauth_token


def _count(session, table: str, **where) -> int:
    clause = " AND ".join(f"{k} = :{k}" for k in where)
    query = f"SELECT count(*) FROM {table}"
    if clause:
        query += f" WHERE {clause}"
    return session.execute(text(query), where).scalar_one()


# --- new account creation -----------------------------------------------------------------


def test_new_identity_creates_account_linked_identity_and_session(monkeypatch, account_ids):
    identity = ProviderIdentity(provider="google", subject="g-sub-1", email="new@example.com")
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(identity))
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())

    resp = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    account_ids.append(body["account_id"])
    assert body["session_token"]
    assert body["expires_at"]

    session = get_session()
    try:
        assert _count(session, "accounts", id=body["account_id"]) == 1
        assert _count(session, "linked_identities", account_id=body["account_id"]) == 1
        row = session.execute(
            text(
                "SELECT provider, provider_subject, provider_email FROM linked_identities "
                "WHERE account_id = :id"
            ),
            {"id": body["account_id"]},
        ).one()
        assert (row.provider, row.provider_subject, row.provider_email) == (
            "google",
            "g-sub-1",
            "new@example.com",
        )
        account_email = session.execute(
            text("SELECT primary_email FROM accounts WHERE id = :id"), {"id": body["account_id"]}
        ).scalar_one()
        assert account_email == "new@example.com"
    finally:
        session.close()


# --- same (provider, subject) resolves to same account, no duplication --------------------


def test_repeat_exchange_same_provider_subject_reuses_account(monkeypatch, account_ids):
    identity = ProviderIdentity(provider="google", subject="g-sub-2", email="repeat@example.com")
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(identity))
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())

    first = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)
    second = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    account_ids.append(first.json()["account_id"])

    assert first.json()["account_id"] == second.json()["account_id"]
    assert first.json()["session_token"] != second.json()["session_token"]

    session = get_session()
    try:
        assert _count(session, "accounts", id=first.json()["account_id"]) == 1
        assert _count(session, "linked_identities", account_id=first.json()["account_id"]) == 1
        assert _count(session, "sessions", account_id=first.json()["account_id"]) == 2
    finally:
        session.close()


# --- email never selects an account (SECURITY.md SS7) --------------------------------------
#
# The former resolution step (b) attached any new identity to whichever account already had a
# matching provider_email -- an nOAuth-class takeover, since Microsoft's mail/UPN are
# tenant-controlled and unverified. Each test uses a fresh uuid-suffixed email so a shared
# address can't collide with rows from any other test.


def _sign_in(provider: str):
    body = _GOOGLE_CODE_BODY if provider == "google" else {"provider": "microsoft", "oauth_token": "t"}
    resp = client.post("/v1/auth/exchange", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["account_id"]


def _linked_providers(account_id: str) -> list[str]:
    session = get_session()
    try:
        return session.execute(
            text("SELECT provider FROM linked_identities WHERE account_id = :id ORDER BY provider"),
            {"id": account_id},
        ).scalars().all()
    finally:
        session.close()


def test_microsoft_sign_in_matching_google_account_email_creates_new_account(monkeypatch, account_ids):
    email = f"victim-{uuid.uuid4()}@example.com"
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(
        ProviderIdentity(provider="google", subject=f"g-{uuid.uuid4()}", email=email)))
    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(
        ProviderIdentity(provider="microsoft", subject=f"ms-{uuid.uuid4()}", email=email)))

    google_account = _sign_in("google")
    account_ids.append(google_account)
    microsoft_account = _sign_in("microsoft")
    account_ids.append(microsoft_account)

    assert microsoft_account != google_account
    assert _linked_providers(google_account) == ["google"]
    assert _linked_providers(microsoft_account) == ["microsoft"]


def test_google_sign_in_matching_microsoft_account_email_creates_new_account(monkeypatch, account_ids):
    email = f"victim-{uuid.uuid4()}@example.com"
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())
    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(
        ProviderIdentity(provider="microsoft", subject=f"ms-{uuid.uuid4()}", email=email)))
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(
        ProviderIdentity(provider="google", subject=f"g-{uuid.uuid4()}", email=email)))

    microsoft_account = _sign_in("microsoft")
    account_ids.append(microsoft_account)
    google_account = _sign_in("google")
    account_ids.append(google_account)

    assert google_account != microsoft_account
    assert _linked_providers(microsoft_account) == ["microsoft"]
    assert _linked_providers(google_account) == ["google"]


def test_two_microsoft_subjects_with_same_email_get_different_accounts(monkeypatch, account_ids):
    # The same-provider variant: an attacker's own Entra tenant user whose `mail` is set to
    # the victim's address, signing in after the victim's real Microsoft account.
    email = f"victim-{uuid.uuid4()}@example.com"
    victim = ProviderIdentity(provider="microsoft", subject=f"ms-{uuid.uuid4()}", email=email)
    attacker = ProviderIdentity(provider="microsoft", subject=f"ms-{uuid.uuid4()}", email=email)

    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(victim))
    victim_account = _sign_in("microsoft")
    account_ids.append(victim_account)
    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(attacker))
    attacker_account = _sign_in("microsoft")
    account_ids.append(attacker_account)

    assert attacker_account != victim_account
    assert _linked_providers(victim_account) == ["microsoft"]


def test_exact_provider_subject_still_resolves_after_same_email_identity_signs_in(
    monkeypatch, account_ids
):
    email = f"owner-{uuid.uuid4()}@example.com"
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(
        ProviderIdentity(provider="google", subject=f"g-{uuid.uuid4()}", email=email)))
    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(
        ProviderIdentity(provider="microsoft", subject=f"ms-{uuid.uuid4()}", email=email)))

    original = _sign_in("google")
    account_ids.append(original)
    other = _sign_in("microsoft")
    account_ids.append(other)

    assert _sign_in("google") == original
    assert _sign_in("microsoft") == other
    assert _linked_providers(original) == ["google"]


# --- different email + different provider => separate accounts -----------------------------


def test_different_email_different_provider_creates_separate_accounts(monkeypatch, account_ids):
    google_identity = ProviderIdentity(provider="google", subject="g-sub-4", email="a4@example.com")
    ms_identity = ProviderIdentity(provider="microsoft", subject="ms-sub-4", email="b4@example.com")
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(google_identity))
    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(ms_identity))
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())

    first = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)
    second = client.post("/v1/auth/exchange", json={"provider": "microsoft", "oauth_token": "t"})
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    account_ids.extend([first.json()["account_id"], second.json()["account_id"]])

    assert first.json()["account_id"] != second.json()["account_id"]


# --- 401 / 422 create no rows ---------------------------------------------------------------


def test_invalid_token_returns_401_and_creates_no_rows(monkeypatch):
    def _raise(oauth_token):
        raise InvalidProviderTokenError("bad token")

    # exchange_google_code_for_token must succeed for verify_google_token (which raises,
    # below) to even be reached -- mock it to a fixed fake token.
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _raise)

    session = get_session()
    try:
        before = _count(session, "accounts")
    finally:
        session.close()

    resp = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)
    assert resp.status_code == 401, resp.text

    session = get_session()
    try:
        after = _count(session, "accounts")
    finally:
        session.close()
    assert after == before


def test_unavailable_email_returns_422_and_creates_no_rows(monkeypatch):
    def _raise(oauth_token):
        raise ProviderEmailUnavailableError("no usable email")

    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _raise)

    session = get_session()
    try:
        before = _count(session, "accounts")
    finally:
        session.close()

    resp = client.post("/v1/auth/exchange", json={"provider": "microsoft", "oauth_token": "bad"})
    assert resp.status_code == 422, resp.text

    session = get_session()
    try:
        after = _count(session, "accounts")
    finally:
        session.close()
    assert after == before


# --- Phase D session 2: request-shape validation and the Google code-exchange leg ----------


def test_google_missing_code_fields_returns_422():
    # Old-shape request (pre-session-2 oauth_token) sent for provider "google" -- this is
    # a client bug, not an account condition, per backend/EXTENSION_INTEGRATION.md SS1.
    resp = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "t"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Google sign-in requires code, code_verifier, and redirect_uri."


def test_microsoft_missing_oauth_token_returns_422():
    resp = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY | {"provider": "microsoft"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Microsoft sign-in requires oauth_token."


def test_google_code_exchange_failure_returns_401_and_creates_no_rows(monkeypatch):
    def _raise(code, code_verifier, redirect_uri):
        raise InvalidProviderTokenError("bad code")

    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _raise)

    session = get_session()
    try:
        before = _count(session, "accounts")
    finally:
        session.close()

    resp = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "OAuth token could not be verified."

    session = get_session()
    try:
        after = _count(session, "accounts")
    finally:
        session.close()
    assert after == before


def test_google_oauth_not_configured_returns_500(monkeypatch):
    def _raise(code, code_verifier, redirect_uri):
        raise OAuthProviderConfigError("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET is not configured.")

    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _raise)
    resp = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"] == "Google OAuth is not configured."


# --- session_token hash discipline ----------------------------------------------------------


def test_session_token_hash_matches_stored_token_hash_and_plaintext_not_persisted(
    monkeypatch, account_ids
):
    identity = ProviderIdentity(provider="google", subject="g-sub-5", email="hash@example.com")
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(identity))
    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_code_for_token", _fake_exchange())

    resp = client.post("/v1/auth/exchange", json=_GOOGLE_CODE_BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    account_ids.append(body["account_id"])
    raw_token = body["session_token"]
    expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    session = get_session()
    try:
        row = session.execute(
            text("SELECT token_hash, created_via_provider FROM sessions WHERE account_id = :id"),
            {"id": body["account_id"]},
        ).one()
        assert row.token_hash == expected_hash
        assert row.created_via_provider == "google"

        all_hashes = session.execute(text("SELECT token_hash FROM sessions")).scalars().all()
        assert raw_token not in all_hashes
    finally:
        session.close()
