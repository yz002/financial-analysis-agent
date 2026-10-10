"""
Pure tests for Phase D session 6's BYO-key changes (app/main.py's /v1/byo-key routes and
_check_byo_key). No database and no Anthropic call: tests_unit/conftest.py makes get_session
raise unless a test supplies a fake, and anthropic.Anthropic is replaced with a fake whose
models.list raises or returns as each test needs. The DB-backed counterparts live in
backend/tests/test_byo_key.py.
"""

import logging
import uuid

import anthropic
import httpx2 as httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app.main as app_main
from app.crypto import decrypt_byo_key
from db.models import Account, ByoKey

client = TestClient(app_main.app)

ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")
_KEY = "sk-ant-api03-" + "k" * 40


def _models_request():
    return httpx.Request("GET", "https://api.anthropic.com/v1/models")


class _FakeAnthropic:
    """Stands in for anthropic.Anthropic; models.list does whatever `behavior` says."""

    instances: list["_FakeAnthropic"] = []
    behavior = None  # an exception to raise, or None to succeed

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.list_calls = []
        _FakeAnthropic.instances.append(self)
        self.models = self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if _FakeAnthropic.behavior is not None:
            raise _FakeAnthropic.behavior
        return []


@pytest.fixture
def fake_anthropic(monkeypatch):
    _FakeAnthropic.instances = []
    _FakeAnthropic.behavior = None
    monkeypatch.setattr(app_main.anthropic, "Anthropic", _FakeAnthropic)
    return _FakeAnthropic


@pytest.fixture
def signed_in():
    app_main.app.dependency_overrides[app_main.get_current_account] = lambda: Account(
        id=ACCOUNT_ID, byo_key_id=None
    )
    yield
    app_main.app.dependency_overrides.pop(app_main.get_current_account, None)


class _FakeSession:
    def __init__(self, log):
        self.log = log

    def execute(self, statement):
        self.log.append(("execute", statement))

    def add(self, obj):
        self.log.append(("add", obj))

    def flush(self):
        for kind, obj in self.log:
            if kind == "add" and getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def commit(self):
        self.log.append(("commit", None))

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    log = []
    monkeypatch.setattr(app_main, "get_session", lambda: _FakeSession(log))
    return log


def _auth_error(status: int, cls):
    message = f"invalid x-api-key: {_KEY}"  # the leak vector: never logged or returned
    return cls(message, response=httpx.Response(status, request=_models_request()), body=None)


# --- _check_byo_key: classifying Anthropic's answer ---------------------------------------------


def test_check_uses_a_no_token_call_with_no_retries(fake_anthropic):
    assert app_main._check_byo_key(_KEY) == "ok"
    [instance] = fake_anthropic.instances
    assert instance.kwargs == {
        "api_key": _KEY,
        "timeout": app_main.BYO_KEY_CHECK_TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert instance.list_calls == [{"limit": 1}]


@pytest.mark.parametrize(
    "status, cls", [(401, anthropic.AuthenticationError), (403, anthropic.PermissionDeniedError)]
)
def test_check_reports_a_refused_key_as_rejected_without_logging_it(fake_anthropic, caplog, status, cls):
    fake_anthropic.behavior = _auth_error(status, cls)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert app_main._check_byo_key(_KEY) == "rejected"
    assert cls.__name__ in caplog.text
    assert _KEY not in caplog.text
    assert "x-api-key" not in caplog.text  # never str(e)


def test_check_reports_an_unreachable_anthropic_as_unavailable(fake_anthropic, caplog):
    fake_anthropic.behavior = anthropic.APIConnectionError(request=_models_request())
    with caplog.at_level(logging.WARNING, logger="app.main"):
        assert app_main._check_byo_key(_KEY) == "unavailable"
    assert _KEY not in caplog.text


# --- POST /v1/byo-key: nothing stored unless every step passes --------------------------------


def test_a_rejected_key_is_422_before_any_database_access(fake_anthropic, signed_in):
    fake_anthropic.behavior = _auth_error(401, anthropic.AuthenticationError)
    resp = client.post("/v1/byo-key", json={"api_key": _KEY})
    # conftest's get_session raises, so a 422 (not a 500) proves nothing was stored.
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": {"error": "byo_key_rejected"}}
    assert _KEY not in resp.text


def test_an_unreachable_anthropic_is_502_and_nothing_is_stored(fake_anthropic, signed_in):
    fake_anthropic.behavior = anthropic.APIConnectionError(request=_models_request())
    resp = client.post("/v1/byo-key", json={"api_key": _KEY})
    assert resp.status_code == 502, resp.text
    assert resp.json() == {"detail": {"error": "byo_key_check_failed"}}


def test_a_malformed_key_is_422_before_any_anthropic_call(fake_anthropic, signed_in):
    resp = client.post("/v1/byo-key", json={"api_key": "not-a-key"})
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": "That doesn't look like a valid Anthropic API key."}
    assert fake_anthropic.instances == []


def test_whitespace_is_stripped_before_the_check_and_the_stored_key(
    fake_anthropic, signed_in, fake_db, monkeypatch
):
    monkeypatch.setenv("BYO_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    resp = client.post("/v1/byo-key", json={"api_key": f"  {_KEY}\n"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"registered": True}
    assert _KEY not in resp.text

    [instance] = fake_anthropic.instances
    assert instance.kwargs["api_key"] == _KEY
    [stored] = [obj for kind, obj in fake_db if kind == "add" and isinstance(obj, ByoKey)]
    assert decrypt_byo_key(stored.encrypted_key) == _KEY  # stripped, not the pasted value


def test_registering_retires_every_earlier_key_and_erases_its_ciphertext(
    fake_anthropic, signed_in, fake_db, monkeypatch
):
    monkeypatch.setenv("BYO_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert client.post("/v1/byo-key", json={"api_key": _KEY}).status_code == 200

    retire = next(obj for kind, obj in fake_db if kind == "execute")
    sql = str(retire.compile(compile_kwargs={"literal_binds": False}))
    assert sql.startswith("UPDATE byo_keys SET")
    assert "encrypted_key" in sql and "is_active" in sql
    assert retire.compile().params["encrypted_key"] == b""
    assert retire.compile().params["is_active"] is False
    # Retired before the new key is added, so the new row is never caught by it.
    kinds = [kind for kind, _ in fake_db]
    assert kinds.index("execute") < kinds.index("add")


# --- DELETE /v1/byo-key --------------------------------------------------------------------


def test_removing_a_key_unsets_the_account_and_erases_ciphertext(signed_in, fake_db):
    resp = client.delete("/v1/byo-key")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"removed": True}

    statements = [obj for kind, obj in fake_db if kind == "execute"]
    params = [s.compile().params for s in statements]
    # Each UPDATE is scoped to the caller's own account (the WHERE clause's bound id).
    assert {"byo_key_id": None, "id_1": ACCOUNT_ID} in params
    assert {"is_active": False, "encrypted_key": b"", "account_id_1": ACCOUNT_ID} in params
    assert fake_db[-1] == ("commit", None)
