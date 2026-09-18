"""
Tests for Phase C session 3: POST /v1/auth/exchange -- provider-token verification
(mocked at the app.oauth_providers call site, matching test_oauth_providers.py's own
scope boundary, since session 2 already tested the underlying HTTP calls), design doc
SS3's 3-step account-resolution order, and opaque session-token issuance/hashing.

Like the other backend integration tests, these hit a real reachable Postgres via
DATABASE_URL (backend/.env) -- not a mocked DB.
"""

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.main import app
from app.oauth_providers import (
    InvalidProviderTokenError,
    ProviderEmailUnavailableError,
    ProviderIdentity,
)
from db.base import get_session

client = TestClient(app)


@pytest.fixture
def account_ids():
    """Tracks account_ids created by a test; deletes them (cascading to
    linked_identities/sessions via ondelete=CASCADE) after."""
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


def _fake_verify(identity: ProviderIdentity):
    return lambda oauth_token: identity


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

    resp = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "t"})
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

    first = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "t1"})
    second = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "t2"})
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


# --- cross-provider merge via same email ---------------------------------------------------


def test_second_provider_same_email_merges_into_existing_account(monkeypatch, account_ids):
    google_identity = ProviderIdentity(provider="google", subject="g-sub-3", email="cross@example.com")
    ms_identity = ProviderIdentity(provider="microsoft", subject="ms-sub-3", email="cross@example.com")
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(google_identity))
    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(ms_identity))

    first = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "t"})
    second = client.post("/v1/auth/exchange", json={"provider": "microsoft", "oauth_token": "t"})
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    account_ids.append(first.json()["account_id"])

    assert first.json()["account_id"] == second.json()["account_id"]

    session = get_session()
    try:
        assert _count(session, "accounts", id=first.json()["account_id"]) == 1
        assert _count(session, "linked_identities", account_id=first.json()["account_id"]) == 2
        providers = session.execute(
            text("SELECT provider FROM linked_identities WHERE account_id = :id ORDER BY provider"),
            {"id": first.json()["account_id"]},
        ).scalars().all()
        assert providers == ["google", "microsoft"]
    finally:
        session.close()


# --- different email + different provider => separate accounts -----------------------------


def test_different_email_different_provider_creates_separate_accounts(monkeypatch, account_ids):
    google_identity = ProviderIdentity(provider="google", subject="g-sub-4", email="a4@example.com")
    ms_identity = ProviderIdentity(provider="microsoft", subject="ms-sub-4", email="b4@example.com")
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(google_identity))
    monkeypatch.setattr(app_main.oauth_providers, "verify_microsoft_token", _fake_verify(ms_identity))

    first = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "t"})
    second = client.post("/v1/auth/exchange", json={"provider": "microsoft", "oauth_token": "t"})
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    account_ids.extend([first.json()["account_id"], second.json()["account_id"]])

    assert first.json()["account_id"] != second.json()["account_id"]


# --- 401 / 422 create no rows ---------------------------------------------------------------


def test_invalid_token_returns_401_and_creates_no_rows(monkeypatch):
    def _raise(oauth_token):
        raise InvalidProviderTokenError("bad token")

    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _raise)

    session = get_session()
    try:
        before = _count(session, "accounts")
    finally:
        session.close()

    resp = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "bad"})
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


# --- session_token hash discipline ----------------------------------------------------------


def test_session_token_hash_matches_stored_token_hash_and_plaintext_not_persisted(
    monkeypatch, account_ids
):
    identity = ProviderIdentity(provider="google", subject="g-sub-5", email="hash@example.com")
    monkeypatch.setattr(app_main.oauth_providers, "verify_google_token", _fake_verify(identity))

    resp = client.post("/v1/auth/exchange", json={"provider": "google", "oauth_token": "t"})
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
