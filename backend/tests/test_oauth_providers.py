"""
Tests for Phase C session 2: the provider verification module (design doc
oauth-identity-session-design.md SS1/SS7 step 2). Unlike this repo's other backend/tests/*
files, these need no reachable Postgres and make no live network calls -- the one outbound
HTTP call each function makes is mocked via monkeypatch.setattr on oauth_providers.requests.get,
matching this project's existing external-API-mocking convention (test_billing.py's
monkeypatch.setattr on stripe.Subscription.retrieve).
"""

import pytest

import app.oauth_providers as oauth_providers
from app.oauth_providers import (
    GoogleDataToken,
    InvalidProviderTokenError,
    OAuthProviderConfigError,
    ProviderEmailUnavailableError,
    exchange_google_code_for_token,
    exchange_google_data_code,
    verify_google_token,
    verify_microsoft_token,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def _mock_get(monkeypatch, response: _FakeResponse):
    monkeypatch.setattr(oauth_providers.requests, "get", lambda *args, **kwargs: response)


def _mock_post(monkeypatch, response: _FakeResponse):
    monkeypatch.setattr(oauth_providers.requests, "post", lambda *args, **kwargs: response)


def _set_google_env(monkeypatch, client_id="fake-client-id", client_secret="fake-client-secret"):
    if client_id is None:
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", client_id)
    if client_secret is None:
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", client_secret)


# --- exchange_google_code_for_token (Phase D session 2 -- Google's Web application client
# type requires a client_secret at its token endpoint; this backend performs that exchange
# server-side instead of the extension doing it client-side) -------------------------------


def test_exchange_google_code_for_token_success(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_post(monkeypatch, _FakeResponse(200, {"access_token": "real-access-token"}))

    token = exchange_google_code_for_token("code-1", "verifier-1", "https://ext.chromiumapp.org/")

    assert token == "real-access-token"


def test_exchange_google_code_for_token_sends_client_secret_and_pkce_fields(monkeypatch):
    _set_google_env(monkeypatch, client_id="cid-123", client_secret="csecret-456")
    captured = {}

    def _fake_post(url, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse(200, {"access_token": "tok"})

    monkeypatch.setattr(oauth_providers.requests, "post", _fake_post)

    exchange_google_code_for_token("code-1", "verifier-1", "https://ext.chromiumapp.org/")

    assert captured["url"] == oauth_providers.GOOGLE_TOKEN_URL
    assert captured["data"] == {
        "grant_type": "authorization_code",
        "client_id": "cid-123",
        "client_secret": "csecret-456",
        "code": "code-1",
        "code_verifier": "verifier-1",
        "redirect_uri": "https://ext.chromiumapp.org/",
    }


def test_exchange_google_code_for_token_refuses_on_non_200(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_post(monkeypatch, _FakeResponse(400, {"error": "invalid_grant"}))

    with pytest.raises(InvalidProviderTokenError):
        exchange_google_code_for_token("code-1", "verifier-1", "https://ext.chromiumapp.org/")


def test_exchange_google_code_for_token_refuses_when_access_token_missing(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_post(monkeypatch, _FakeResponse(200, {"token_type": "Bearer"}))

    with pytest.raises(InvalidProviderTokenError):
        exchange_google_code_for_token("code-1", "verifier-1", "https://ext.chromiumapp.org/")


def test_exchange_google_code_for_token_refuses_when_client_id_missing(monkeypatch):
    _set_google_env(monkeypatch, client_id=None)

    with pytest.raises(OAuthProviderConfigError):
        exchange_google_code_for_token("code-1", "verifier-1", "https://ext.chromiumapp.org/")


def test_exchange_google_code_for_token_refuses_when_client_secret_missing(monkeypatch):
    _set_google_env(monkeypatch, client_secret=None)

    with pytest.raises(OAuthProviderConfigError):
        exchange_google_code_for_token("code-1", "verifier-1", "https://ext.chromiumapp.org/")


# --- exchange_google_data_code (Phase D session 3a -- the separate Google data grant behind
# POST /v1/google/data-token) ------------------------------------------------------------------


def test_exchange_google_data_code_returns_only_the_three_fields(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_post(monkeypatch, _FakeResponse(200, {
        "access_token": "data-access-token",
        "expires_in": 3599,
        "scope": "openid email https://www.googleapis.com/auth/spreadsheets.readonly",
        "token_type": "Bearer",
        "id_token": "header.payload.sig",
        # Never requested (no access_type=offline), but must be dropped if it ever appears.
        "refresh_token": "RT-must-not-survive",
    }))

    result = exchange_google_data_code("code-1", "verifier-1", "https://ext.chromiumapp.org/")

    assert result == GoogleDataToken(
        access_token="data-access-token",
        expires_in=3599,
        scope="openid email https://www.googleapis.com/auth/spreadsheets.readonly",
    )
    assert "RT-must-not-survive" not in repr(result)


def test_exchange_google_data_code_never_sends_access_type(monkeypatch):
    _set_google_env(monkeypatch, client_id="cid-123", client_secret="csecret-456")
    captured = {}

    def _fake_post(url, data=None, timeout=None):
        captured["data"] = data
        return _FakeResponse(200, {"access_token": "tok", "expires_in": 3599, "scope": "openid"})

    monkeypatch.setattr(oauth_providers.requests, "post", _fake_post)

    exchange_google_data_code("code-1", "verifier-1", "https://ext.chromiumapp.org/")

    assert "access_type" not in captured["data"]
    assert captured["data"] == {
        "grant_type": "authorization_code",
        "client_id": "cid-123",
        "client_secret": "csecret-456",
        "code": "code-1",
        "code_verifier": "verifier-1",
        "redirect_uri": "https://ext.chromiumapp.org/",
    }


def test_exchange_google_data_code_refuses_on_non_200(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_post(monkeypatch, _FakeResponse(400, {"error": "invalid_grant"}))

    with pytest.raises(InvalidProviderTokenError):
        exchange_google_data_code("code-1", "verifier-1", "https://ext.chromiumapp.org/")


def test_exchange_google_data_code_refuses_when_access_token_missing(monkeypatch):
    _set_google_env(monkeypatch)
    _mock_post(monkeypatch, _FakeResponse(200, {"scope": "openid"}))

    with pytest.raises(InvalidProviderTokenError):
        exchange_google_data_code("code-1", "verifier-1", "https://ext.chromiumapp.org/")


def test_exchange_google_data_code_refuses_when_not_configured(monkeypatch):
    _set_google_env(monkeypatch, client_secret=None)

    with pytest.raises(OAuthProviderConfigError):
        exchange_google_data_code("code-1", "verifier-1", "https://ext.chromiumapp.org/")


# --- verify_google_token -----------------------------------------------------------------


def test_verify_google_token_success(monkeypatch):
    _mock_get(
        monkeypatch,
        _FakeResponse(200, {"sub": "google-sub-123", "email": "person@example.com", "email_verified": True}),
    )

    identity = verify_google_token("fake-token")

    assert identity.provider == "google"
    assert identity.subject == "google-sub-123"
    assert identity.email == "person@example.com"


def test_verify_google_token_refuses_when_email_not_verified(monkeypatch):
    _mock_get(
        monkeypatch,
        _FakeResponse(200, {"sub": "google-sub-123", "email": "person@example.com", "email_verified": False}),
    )

    with pytest.raises(InvalidProviderTokenError):
        verify_google_token("fake-token")


def test_verify_google_token_refuses_on_non_200(monkeypatch):
    _mock_get(monkeypatch, _FakeResponse(401, {"error": "invalid_token"}))

    with pytest.raises(InvalidProviderTokenError):
        verify_google_token("fake-token")


# --- verify_microsoft_token ----------------------------------------------------------------


def test_verify_microsoft_token_success_with_mail(monkeypatch):
    _mock_get(
        monkeypatch,
        _FakeResponse(200, {"id": "ms-id-123", "mail": "person@example.com", "userPrincipalName": "person@example.com"}),
    )

    identity = verify_microsoft_token("fake-token")

    assert identity.provider == "microsoft"
    assert identity.subject == "ms-id-123"
    assert identity.email == "person@example.com"


def test_verify_microsoft_token_falls_back_to_valid_upn_when_mail_null(monkeypatch):
    _mock_get(
        monkeypatch,
        _FakeResponse(200, {"id": "ms-id-123", "mail": None, "userPrincipalName": "person@tenant.onmicrosoft.com"}),
    )

    identity = verify_microsoft_token("fake-token")

    assert identity.provider == "microsoft"
    assert identity.subject == "ms-id-123"
    assert identity.email == "person@tenant.onmicrosoft.com"


def test_verify_microsoft_token_refuses_when_upn_not_email_shaped(monkeypatch):
    _mock_get(
        monkeypatch,
        _FakeResponse(200, {"id": "ms-id-123", "mail": None, "userPrincipalName": "+1-555-000-1234"}),
    )

    with pytest.raises(ProviderEmailUnavailableError):
        verify_microsoft_token("fake-token")


def test_verify_microsoft_token_refuses_on_non_200(monkeypatch):
    _mock_get(monkeypatch, _FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}}))

    with pytest.raises(InvalidProviderTokenError):
        verify_microsoft_token("fake-token")
