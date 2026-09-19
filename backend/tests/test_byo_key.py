"""
Tests for Phase B session 8: BYO Anthropic API key registration + encryption at rest
(design doc SS4), and wiring the decrypted key into /v1/ask's real Anthropic client
construction.

Like the other backend integration tests, these hit a real reachable Postgres via
DATABASE_URL (backend/.env). No real Anthropic or Fernet-external call is needed --
app.crypto's encrypt/decrypt round-trips locally against a test BYO_KEY_ENCRYPTION_KEY
set via monkeypatch.setenv (never a real production secret), and app_main.run_agent is
monkeypatched the same way test_tier_gating.py's HTTP-level tests do, except here the
lambda also captures its `client` kwarg so the test can assert on the decrypted key
actually threaded through to anthropic.Anthropic(api_key=...).

A submitted BYO key never appears in this file as anything other than the fixed test
literal below -- these tests aren't a substitute for a secrets-scanning tool, but they do
confirm app.crypto never logs and that /v1/byo-key's response never echoes the key back.
"""

import uuid

import anthropic
import httpx2 as httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.crypto import decrypt_byo_key, encrypt_byo_key, is_valid_byo_key_format
from app.main import app
from db.base import get_session
from db.models import Account, ByoKey

client = TestClient(app)

_TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()
_VALID_KEY = "sk-ant-api03-" + "a" * 40
_OTHER_VALID_KEY = "sk-ant-api03-" + "b" * 40


@pytest.fixture(autouse=True)
def _byo_encryption_key(monkeypatch):
    monkeypatch.setenv("BYO_KEY_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)


def _seed_byo_key(session, account_id: str, is_active: bool) -> ByoKey:
    """Seeds an active byo_keys row directly and repoints accounts.byo_key_id at it --
    the account row itself already exists (created via a real /v1/auth/exchange call by the
    auth_session fixture), so only the ByoKey row and FK need seeding here."""
    byo_key = ByoKey(account_id=uuid.UUID(account_id), encrypted_key=encrypt_byo_key(_VALID_KEY), is_active=is_active)
    session.add(byo_key)
    session.flush()
    account = session.get(Account, uuid.UUID(account_id))
    account.byo_key_id = byo_key.id
    session.flush()
    return byo_key


def _fake_result(question: str) -> dict:
    return {
        "question": question,
        "final_answer": "Answer.",
        "hit_iteration_cap": False,
        "iterations_used": 1,
        "stop_reason": "end_turn",
        "figure_check": {},
        "tool_calls": [],
    }


# --- app.crypto unit tests -------------------------------------------------------------


def test_fernet_round_trip_recovers_exact_original_key():
    encrypted = encrypt_byo_key(_VALID_KEY)
    assert encrypted != _VALID_KEY.encode()
    assert decrypt_byo_key(encrypted) == _VALID_KEY


def test_encrypted_key_never_contains_plaintext_substring():
    encrypted = encrypt_byo_key(_VALID_KEY)
    assert _VALID_KEY.encode() not in encrypted


@pytest.mark.parametrize(
    "candidate",
    [
        "not-a-key-at-all",
        "sk-live-1234567890",  # a plausible-looking key from a different provider
        "sk-ant-",  # prefix only, no body
        "sk-ant-tooshort",
        "",
    ],
)
def test_invalid_key_formats_rejected(candidate):
    assert is_valid_byo_key_format(candidate) is False


def test_valid_key_format_accepted():
    assert is_valid_byo_key_format(_VALID_KEY) is True


# --- POST /v1/byo-key --------------------------------------------------------------------


def test_register_byo_key_stores_encrypted_and_updates_account_fk(auth_session):
    account_id, headers = auth_session()
    resp = client.post("/v1/byo-key", json={"api_key": _VALID_KEY}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"registered": True}
    # The raw key must never appear anywhere in the response body.
    assert _VALID_KEY not in resp.text

    session = get_session()
    try:
        account = session.get(Account, uuid.UUID(account_id))
        assert account.byo_key_id is not None
        byo_key = session.get(ByoKey, account.byo_key_id)
        assert byo_key.is_active is True
        assert byo_key.encrypted_key != _VALID_KEY.encode()
        assert decrypt_byo_key(byo_key.encrypted_key) == _VALID_KEY
    finally:
        session.close()


def test_register_byo_key_rejects_malformed_key_without_storing_anything(auth_session):
    account_id, headers = auth_session()
    resp = client.post("/v1/byo-key", json={"api_key": "not-a-real-key"}, headers=headers)
    assert resp.status_code == 422, resp.text

    session = get_session()
    try:
        account = session.get(Account, uuid.UUID(account_id))
        assert account.byo_key_id is None
        count = session.execute(
            text("SELECT count(*) FROM byo_keys WHERE account_id = :id"), {"id": account_id}
        ).scalar_one()
        assert count == 0
    finally:
        session.close()


def test_register_byo_key_replaces_prior_active_key(auth_session):
    account_id, headers = auth_session()
    first = client.post("/v1/byo-key", json={"api_key": _VALID_KEY}, headers=headers)
    assert first.status_code == 200, first.text

    session = get_session()
    try:
        account = session.get(Account, uuid.UUID(account_id))
        first_key_id = account.byo_key_id
    finally:
        session.close()

    second = client.post("/v1/byo-key", json={"api_key": _OTHER_VALID_KEY}, headers=headers)
    assert second.status_code == 200, second.text

    session = get_session()
    try:
        account = session.get(Account, uuid.UUID(account_id))
        assert account.byo_key_id != first_key_id

        old_key = session.get(ByoKey, first_key_id)
        assert old_key.is_active is False  # soft-deactivated, not deleted -- audit trail

        new_key = session.get(ByoKey, account.byo_key_id)
        assert new_key.is_active is True
        assert decrypt_byo_key(new_key.encrypted_key) == _OTHER_VALID_KEY

        # Exactly one active row for this account at a time.
        active_count = session.execute(
            text(
                "SELECT count(*) FROM byo_keys WHERE account_id = :id AND is_active = true"
            ),
            {"id": account_id},
        ).scalar_one()
        assert active_count == 1
    finally:
        session.close()


# --- /v1/ask BYO-key wiring ---------------------------------------------------------------


def test_ask_byo_key_tier_uses_decrypted_key_and_updates_last_used_at(auth_session, monkeypatch):
    account_id, headers = auth_session()
    session = get_session()
    try:
        byo_key = _seed_byo_key(session, account_id, is_active=True)
        session.commit()
        byo_key_id = byo_key.id
    finally:
        session.close()

    captured_clients = []

    def _fake_run_agent(question, prior_messages=None, client=None):
        captured_clients.append(client)
        return _fake_result(question)

    monkeypatch.setattr(app_main, "run_agent", _fake_run_agent)

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 200, resp.text

    assert len(captured_clients) == 1
    used_client = captured_clients[0]
    assert used_client is not None
    assert used_client.api_key == _VALID_KEY

    session = get_session()
    try:
        byo_key = session.get(ByoKey, byo_key_id)
        assert byo_key.last_used_at is not None
    finally:
        session.close()


def _httpx_request():
    # anthropic's exception constructors type-hint request/response as httpx2, not httpx --
    # use the real type (matching tests/test_app.py's existing idiom) rather than relying
    # on the hint going unenforced.
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_ask_byo_key_auth_failure_does_not_leak_key_in_response(auth_session, monkeypatch):
    """A bad/rejected BYO key must never put the key itself (or any fragment of it) into
    the HTTP error response -- the concrete risk this session's BYO-key wiring introduced,
    per the review that flagged it."""
    account_id, headers = auth_session()
    session = get_session()
    try:
        _seed_byo_key(session, account_id, is_active=True)
        session.commit()
    finally:
        session.close()

    auth_error_response = httpx.Response(401, request=_httpx_request())

    def _raise_auth_error(question, prior_messages=None, client=None):
        # The fake key appears in the raised exception's own message -- the exact
        # leak vector under test: a naive `detail=str(e)` would put it straight into
        # the HTTP response body.
        raise anthropic.AuthenticationError(
            f"invalid x-api-key: {_VALID_KEY}", response=auth_error_response, body=None
        )

    monkeypatch.setattr(app_main, "run_agent", _raise_auth_error)

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 502, resp.text
    assert _VALID_KEY not in resp.text
    assert "x-api-key" not in resp.text  # no fragment of the raw SDK message either
    assert (
        resp.json()["detail"]
        == "Your Anthropic API key was rejected. Please re-register a valid key."
    )


def test_ask_master_key_auth_failure_returns_generic_message_no_leak(auth_session, monkeypatch):
    """Same failure on the non-BYO (master-key) path: generic message, no leak, and a
    tier-appropriate detail distinct from the BYO case."""
    _, headers = auth_session()
    master_key_fragment = "sk-ant-api03-master-key-should-never-appear"
    auth_error_response = httpx.Response(401, request=_httpx_request())

    def _raise_auth_error(question, prior_messages=None):
        raise anthropic.AuthenticationError(
            f"invalid x-api-key: {master_key_fragment}", response=auth_error_response, body=None
        )

    monkeypatch.setattr(app_main, "run_agent", _raise_auth_error)

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 502, resp.text
    assert master_key_fragment not in resp.text
    assert resp.json()["detail"] == "Anthropic API authentication failed."


def test_ask_free_tier_passes_no_client_kwarg(auth_session, monkeypatch):
    """A non-BYO account must still go through run_agent's own default client (the master
    ANTHROPIC_API_KEY) -- confirmed here by asserting the client kwarg is omitted entirely,
    not passed as None (main.py builds run_agent_kwargs conditionally for exactly this)."""
    _, headers = auth_session()
    captured = {}

    def _fake_run_agent(question, **kwargs):
        captured.update(kwargs)
        return _fake_result(question)

    monkeypatch.setattr(app_main, "run_agent", _fake_run_agent)

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert "client" not in captured


# --- Session 10 security hardening: decrypt-failure recovery + str(e) leak fixes --------


def test_ask_byo_key_decrypt_failure_deactivates_key_and_recovers_next_request(
    auth_session, monkeypatch
):
    """A decrypt failure (the shape a Fernet key rotation produces against old ciphertext,
    per backend/SECURITY.md's hard-cutover runbook) must deactivate the row rather than
    fail the same way on every subsequent request -- the next /v1/ask for this account
    should fall through gating.evaluate_ask_gate's byo_key.is_active check to the free
    tier instead of repeating the decrypt error forever."""
    account_id, headers = auth_session()
    session = get_session()
    try:
        byo_key = _seed_byo_key(session, account_id, is_active=True)
        session.commit()
        byo_key_id = byo_key.id
    finally:
        session.close()

    # Simulate BYO_KEY_ENCRYPTION_KEY having been rotated out from under this
    # already-encrypted row -- decrypt_byo_key now raises against a different key.
    monkeypatch.setenv("BYO_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 500, resp.text
    assert "deactivated" in resp.json()["detail"]

    session = get_session()
    try:
        byo_key = session.get(ByoKey, byo_key_id)
        assert byo_key.is_active is False
    finally:
        session.close()

    captured: dict = {}

    def _fake_run_agent(question, **kwargs):
        captured.update(kwargs)
        return _fake_result(question)

    monkeypatch.setattr(app_main, "run_agent", _fake_run_agent)

    resp2 = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp2.status_code == 200, resp2.text
    assert "client" not in captured  # free tier now, not byo_key -- master client used


def test_ask_generic_api_error_does_not_leak_exception_detail(auth_session, monkeypatch):
    """anthropic.APIError (not the AuthenticationError subclass already covered above)
    must not have str(e) reach the HTTP response either -- session 10's security
    hardening fix applies to every handler in this route, not just the auth-specific
    one."""
    _, headers = auth_session()
    marker = "internal-anthropic-error-detail-should-not-leak"

    def _raise_api_error(question, prior_messages=None):
        raise anthropic.APIError(marker, _httpx_request(), body=None)

    monkeypatch.setattr(app_main, "run_agent", _raise_api_error)

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 502, resp.text
    assert marker not in resp.text
    assert resp.json()["detail"] == "Anthropic API error."


def test_ask_generic_exception_does_not_leak_exception_detail(auth_session, monkeypatch):
    """A non-Anthropic failure inside run_agent (a tool-execution bug, a pandas/EDGAR
    error, etc.) must not have str(e) reach the HTTP response either."""
    _, headers = auth_session()
    marker = "internal-tool-failure-detail-should-not-leak"

    def _raise_generic_error(question, prior_messages=None):
        raise RuntimeError(marker)

    monkeypatch.setattr(app_main, "run_agent", _raise_generic_error)

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 500, resp.text
    assert marker not in resp.text
    assert resp.json()["detail"] == "run_agent failed unexpectedly."
