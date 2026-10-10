"""
Proves tests_support/network_guard.py, which backend/tests/conftest.py applies to every
DB-backed test, without running anything under backend/tests. Each SDK is pointed at an
unroutable local address first, so even a guard that failed to engage couldn't reach Stripe
or Anthropic -- it would fail with a connection error, not RealNetworkCallBlocked.
"""

import ast
from pathlib import Path

import anthropic
import pytest
import stripe

from tests_support.network_guard import RealNetworkCallBlocked, block_external_apis

_UNROUTABLE = "http://127.0.0.1:9"
_TESTS_DIR = Path(__file__).resolve().parents[1] / "tests"


@pytest.fixture
def offline_stripe(monkeypatch):
    monkeypatch.setattr(stripe, "api_key", "sk_test_guard_dummy")
    monkeypatch.setattr(stripe, "api_base", _UNROUTABLE)
    monkeypatch.setattr(stripe, "max_network_retries", 0)


def _anthropic_client():
    return anthropic.Anthropic(api_key="sk-ant-guard-dummy", base_url=_UNROUTABLE, max_retries=0)


def test_a_stripe_resource_call_is_blocked_and_recorded(monkeypatch, offline_stripe):
    attempts = block_external_apis(monkeypatch)
    with pytest.raises(RealNetworkCallBlocked, match="Stripe"):
        stripe.Subscription.retrieve("sub_guard")
    assert attempts and attempts[0].startswith("Stripe")


def test_an_anthropic_call_is_blocked_and_recorded(monkeypatch):
    attempts = block_external_apis(monkeypatch)
    with pytest.raises(RealNetworkCallBlocked, match="Anthropic"):
        _anthropic_client().models.list(limit=1)
    assert attempts == ["Anthropic SyncAPIClient.request"]


def test_the_app_error_handlers_cannot_swallow_a_blocked_call():
    # app/main.py catches stripe.StripeError and anthropic.APIError around these calls.
    assert not issubclass(RealNetworkCallBlocked, stripe.StripeError)
    assert not issubclass(RealNetworkCallBlocked, anthropic.APIError)


def test_a_swallowed_attempt_is_still_recorded_for_teardown(monkeypatch, offline_stripe):
    attempts = block_external_apis(monkeypatch)
    try:
        stripe.checkout.Session.create(mode="subscription")
    except Exception:  # noqa: BLE001 -- the shape of a broad app-side handler
        pass
    assert attempts  # conftest's teardown assertion fails the test on this


def test_allow_stripe_lets_stripe_through_but_still_blocks_anthropic(monkeypatch, offline_stripe):
    attempts = block_external_apis(monkeypatch, allow_stripe=True)
    # Not blocked: it reaches the (unroutable) network and fails there instead.
    with pytest.raises(stripe.APIConnectionError):
        stripe.Subscription.retrieve("sub_guard")
    with pytest.raises(RealNetworkCallBlocked):
        _anthropic_client().models.list(limit=1)
    assert attempts == ["Anthropic SyncAPIClient.request"]


def test_the_guard_is_autouse_in_the_db_backed_conftest():
    tree = ast.parse((_TESTS_DIR / "conftest.py").read_text(encoding="utf-8"))
    autouse_fixtures = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any("autouse=True" in ast.unparse(d) for d in node.decorator_list)
    ]
    assert any("block_external_apis" in ast.unparse(f) for f in autouse_fixtures)


def test_only_the_sandbox_test_is_exempt():
    exempt = []
    for path in _TESTS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and any(
                "real_stripe" in ast.unparse(d) for d in node.decorator_list
            ):
                exempt.append(node.name)
    assert exempt == ["test_checkout_session_creation_hits_real_sandbox"]
