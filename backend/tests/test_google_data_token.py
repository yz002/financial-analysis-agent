"""
Tests for Phase D session 3a: POST /v1/google/data-token (EXTENSION_INTEGRATION.md SS1a).
Mocked at the app.oauth_providers call site, the same boundary test_auth_exchange.py uses
(test_oauth_providers.py covers the underlying HTTP calls). Like the other backend
integration tests, these hit a real reachable Postgres via DATABASE_URL (backend/.env) --
the session the route requires is a real sessions row from a real /v1/auth/exchange call.
"""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.main import app
from app.oauth_providers import (
    GoogleDataToken,
    InvalidProviderTokenError,
    OAuthProviderConfigError,
    ProviderIdentity,
)
from db.base import get_session

client = TestClient(app)

URL = "/v1/google/data-token"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# Distinctive strings so the "logs nothing" test can search every log record for them.
_CODE = "data-code-LOGCHECK-1a2b"
_VERIFIER = "data-verifier-LOGCHECK-3c4d"
_ACCESS_TOKEN = "ya29.data-token-LOGCHECK-5e6f"
_BODY = {
    "code": _CODE,
    "code_verifier": _VERIFIER,
    "redirect_uri": "https://fake-extension-id.chromiumapp.org/",
}


def _mock_data_grant(monkeypatch, email="data-account@example.com", calls=None):
    """Mocks both outbound steps: the code exchange and the userinfo lookup. Must run AFTER
    auth_session(), since that fixture mocks verify_google_token for sign-in too."""

    def _exchange(code, code_verifier, redirect_uri):
        if calls is not None:
            calls.append((code, code_verifier, redirect_uri))
        return GoogleDataToken(
            access_token=_ACCESS_TOKEN, expires_in=3599, scope=f"openid email {SHEETS_SCOPE}"
        )

    monkeypatch.setattr(app_main.oauth_providers, "exchange_google_data_code", _exchange)
    monkeypatch.setattr(
        app_main.oauth_providers,
        "verify_google_token",
        lambda oauth_token: ProviderIdentity(provider="google", subject="data-sub", email=email),
    )


def _raise(exc):
    def _fn(*args, **kwargs):
        raise exc

    return _fn


def _row_counts() -> dict:
    session = get_session()
    try:
        return {
            table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("accounts", "linked_identities", "sessions")
        }
    finally:
        session.close()


# --- session requirement (401 only ever comes from get_current_account) --------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "not-a-bearer-token"},
        {"Authorization": "Bearer never-issued-token"},
    ],
    ids=["no-header", "no-bearer-prefix", "unknown-token"],
)
def test_without_a_valid_session_401s_and_never_exchanges(monkeypatch, headers):
    calls = []
    _mock_data_grant(monkeypatch, calls=calls)

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 401, resp.text
    assert resp.json() == {"detail": "Invalid or expired session token."}
    assert calls == []


def test_revoked_session_401s_and_never_exchanges(monkeypatch, auth_session):
    _, headers = auth_session()
    assert client.post("/v1/auth/logout", headers=headers).status_code == 200
    calls = []
    _mock_data_grant(monkeypatch, calls=calls)

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 401, resp.text
    assert calls == []


# --- success --------------------------------------------------------------------------------


def test_success_returns_exactly_the_four_fields_with_no_store(monkeypatch, auth_session):
    _, headers = auth_session()
    calls = []
    _mock_data_grant(monkeypatch, calls=calls)

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "access_token": _ACCESS_TOKEN,
        "expires_in": 3599,
        "scope": f"openid email {SHEETS_SCOPE}",
        "email": "data-account@example.com",
    }
    assert resp.headers["cache-control"] == "no-store"
    assert calls == [(_CODE, _VERIFIER, _BODY["redirect_uri"])]


@pytest.mark.parametrize("sign_in_provider", ["google", "microsoft"])
def test_data_account_may_differ_from_sign_in_identity(monkeypatch, auth_session, sign_in_provider):
    # docs/chrome-extension-design.md SS2 "Data account vs sign-in identity": no comparison
    # against the caller's linked identities, whichever provider they signed in with.
    _, headers = auth_session(email="signed-in-person@example.com", provider=sign_in_provider)
    _mock_data_grant(monkeypatch, email="someone-else@work.example")

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "someone-else@work.example"


def test_stores_nothing(monkeypatch, auth_session):
    _, headers = auth_session()
    _mock_data_grant(monkeypatch)
    before = _row_counts()

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 200, resp.text
    assert _row_counts() == before


def test_logs_nothing_about_the_code_or_token(monkeypatch, auth_session, caplog):
    _, headers = auth_session()
    _mock_data_grant(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        ok = client.post(URL, json=_BODY, headers=headers)
        monkeypatch.setattr(
            app_main.oauth_providers,
            "exchange_google_data_code",
            _raise(InvalidProviderTokenError("rejected")),
        )
        failed = client.post(URL, json=_BODY, headers=headers)

    assert ok.status_code == 200 and failed.status_code == 400
    for record in caplog.records:
        message = record.getMessage()
        for secret in (_CODE, _VERIFIER, _ACCESS_TOKEN):
            assert secret not in message, f"{record.name} logged {secret!r}"


# --- errors -----------------------------------------------------------------------------------


def test_failed_exchange_is_400_and_keeps_the_session(monkeypatch, auth_session):
    _, headers = auth_session()
    monkeypatch.setattr(
        app_main.oauth_providers,
        "exchange_google_data_code",
        _raise(InvalidProviderTokenError("invalid_grant")),
    )

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Google authorization could not be exchanged."}
    # Deliberately not a 401: the session must still be valid afterwards.
    assert client.get("/v1/usage", headers=headers).status_code == 200


def test_failed_userinfo_is_400(monkeypatch, auth_session):
    _, headers = auth_session()
    _mock_data_grant(monkeypatch)
    monkeypatch.setattr(
        app_main.oauth_providers,
        "verify_google_token",
        _raise(InvalidProviderTokenError("userinfo 401")),
    )

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Google authorization could not be exchanged."}


@pytest.mark.parametrize("missing", ["code", "code_verifier", "redirect_uri"])
@pytest.mark.parametrize("how", ["absent", "empty"])
def test_missing_field_is_422_with_named_detail(monkeypatch, auth_session, missing, how):
    _, headers = auth_session()
    calls = []
    _mock_data_grant(monkeypatch, calls=calls)
    body = dict(_BODY)
    if how == "absent":
        del body[missing]
    else:
        body[missing] = ""

    resp = client.post(URL, json=body, headers=headers)

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": "code, code_verifier, and redirect_uri are required."}
    assert calls == []


def test_oauth_not_configured_is_500(monkeypatch, auth_session):
    _, headers = auth_session()
    monkeypatch.setattr(
        app_main.oauth_providers,
        "exchange_google_data_code",
        _raise(OAuthProviderConfigError("not configured")),
    )

    resp = client.post(URL, json=_BODY, headers=headers)

    assert resp.status_code == 500, resp.text
    assert resp.json() == {"detail": "Google OAuth is not configured."}
