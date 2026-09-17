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
    InvalidProviderTokenError,
    ProviderEmailUnavailableError,
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
