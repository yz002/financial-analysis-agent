"""
Pure tests for the Phase D session 6 billing changes (app/billing.py and the billing routes in
app/main.py). Nothing here reaches Stripe or the database: tests_unit/conftest.py makes any
get_session call raise, so a route that returns its status without raising proves it never
touched the database. The DB-backed counterparts live in backend/tests/test_billing.py.
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.billing as billing
import app.main as app_main
from db.models import Account, Subscription

client = TestClient(app_main.app)

_SECRET = "whsec_unit_test_secret"
ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-00000000b111")


def _sign(payload: bytes, secret: str = _SECRET) -> str:
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload.decode()}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(event_type: str, obj: dict, livemode: bool = False) -> dict:
    return {"id": f"evt_{uuid.uuid4()}", "type": event_type, "livemode": livemode, "data": {"object": obj}}


def _post_webhook(event: dict, signature: str | None = None):
    payload = json.dumps(event).encode()
    headers = {"Content-Type": "application/json", "Stripe-Signature": signature or _sign(payload)}
    return client.post("/v1/billing/webhook", content=payload, headers=headers)


def _stripe_subscription(sub_id: str, status: str = "active", **extra) -> dict:
    return {
        "id": sub_id,
        "customer": "cus_unit",
        "status": status,
        "cancel_at_period_end": False,
        "cancel_at": None,
        "items": {"data": [{"current_period_start": 1_700_000_000, "current_period_end": 1_702_592_000}]},
        **extra,
    }


@pytest.fixture
def signed_in():
    app_main.app.dependency_overrides[app_main.get_current_account] = lambda: Account(
        id=ACCOUNT_ID, primary_email="owner@example.com"
    )
    yield
    app_main.app.dependency_overrides.pop(app_main.get_current_account, None)


# --- the test-mode key guard ---------------------------------------------------------------


def test_missing_secret_key_is_refused(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(billing.BillingUnavailable, match="not set"):
        billing.ensure_configured()


@pytest.mark.parametrize("key", ["sk_live_abc123", "rk_live_abc123", "not-a-stripe-key"])
def test_non_test_mode_secret_key_is_refused(monkeypatch, key):
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    with pytest.raises(billing.BillingUnavailable) as excinfo:
        billing.ensure_configured()
    assert "test-mode" in str(excinfo.value)
    assert key not in str(excinfo.value)  # the reason never carries the value


@pytest.mark.parametrize("key", ["sk_test_abc123", "rk_test_abc123"])
def test_test_mode_secret_key_is_accepted(monkeypatch, key):
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    billing.ensure_configured()


def test_checkout_route_with_live_key_is_503_before_any_database_access(monkeypatch, signed_in):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc123")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_unit")
    resp = client.post("/v1/billing/checkout-session")
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": {"error": "billing_unavailable"}}


def test_portal_route_unconfigured_is_503_before_any_database_access(monkeypatch, signed_in):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    resp = client.post("/v1/billing/portal-session")
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": {"error": "billing_unavailable"}}


# --- checkout and portal session parameters -------------------------------------------------


def test_checkout_kwargs_first_checkout_prefills_email_and_records_account():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    kwargs = billing.checkout_session_kwargs(
        ACCOUNT_ID, "owner@example.com", "price_unit", "https://s", "https://c", None, now
    )
    assert kwargs["client_reference_id"] == str(ACCOUNT_ID)
    assert kwargs["customer_email"] == "owner@example.com"
    assert "customer" not in kwargs
    assert kwargs["metadata"] == {"account_id": str(ACCOUNT_ID)}
    assert kwargs["subscription_data"] == {"metadata": {"account_id": str(ACCOUNT_ID)}}
    # At least Stripe's 30-minute minimum, with a margin for latency.
    assert kwargs["expires_at"] - int(now.timestamp()) == 31 * 60


def test_checkout_kwargs_returning_customer_reuses_customer_without_email():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    kwargs = billing.checkout_session_kwargs(
        ACCOUNT_ID, "owner@example.com", "price_unit", "https://s", "https://c", "cus_old", now
    )
    assert kwargs["customer"] == "cus_old"
    assert "customer_email" not in kwargs  # Stripe refuses both together


def test_portal_session_uses_the_given_customer_and_return_url(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc123")
    captured = {}

    class _FakePortal:
        url = "https://billing.stripe.com/p/session/unit"

    def _create(**kwargs):
        captured.update(kwargs)
        return _FakePortal()

    monkeypatch.setattr(billing.stripe.billing_portal.Session, "create", _create)
    url = billing.create_portal_session("cus_unit", "https://backend/v1/billing/return")
    assert url == "https://billing.stripe.com/p/session/unit"
    assert captured == {"customer": "cus_unit", "return_url": "https://backend/v1/billing/return"}


# --- cancellation, from either Stripe field ---------------------------------------------------


def test_cancellation_scheduled_from_cancel_at_period_end():
    assert billing.cancellation_scheduled({"cancel_at_period_end": True, "cancel_at": None}) is True


def test_cancellation_scheduled_from_cancel_at():
    assert billing.cancellation_scheduled({"cancel_at_period_end": False, "cancel_at": 1_702_592_000}) is True
    assert billing.cancellation_scheduled({"cancel_at_period_end": False, "cancel_at": None}) is False


# --- the webhook, before any database access -------------------------------------------------


def test_webhook_bad_signature_is_400_before_any_database_access(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _SECRET)
    resp = _post_webhook(_event("customer.subscription.updated", {"id": "sub_x"}), "t=1,v1=bad")
    assert resp.status_code == 400, resp.text


def test_webhook_unconfigured_is_503_before_any_database_access(monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    resp = _post_webhook(_event("customer.subscription.updated", {"id": "sub_x"}))
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": {"error": "billing_unavailable"}}


def test_webhook_livemode_event_is_400_before_any_database_access(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _SECRET)
    resp = _post_webhook(_event("customer.subscription.updated", {"id": "sub_x"}, livemode=True))
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": {"error": "livemode_event_rejected"}}


def test_webhook_redelivery_never_calls_stripe(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(app_main, "_stripe_event_seen", lambda event_id: True)

    def _must_not_prepare(event):
        raise AssertionError("a redelivered event must not reach Stripe")

    monkeypatch.setattr(billing, "prepare_event", _must_not_prepare)
    resp = _post_webhook(_event("customer.subscription.updated", {"id": "sub_x"}))
    assert resp.status_code == 200, resp.text


# --- prepare_event: what gets read from Stripe --------------------------------------------------


def test_prepare_ignores_a_checkout_with_a_malformed_reference():
    event = _event(
        "checkout.session.completed",
        {"mode": "subscription", "subscription": "sub_x", "client_reference_id": "not-a-uuid"},
    )
    assert billing.prepare_event(event) is None


def test_prepare_ignores_a_checkout_that_is_not_a_subscription():
    event = _event(
        "checkout.session.completed",
        {"mode": "payment", "subscription": None, "client_reference_id": str(ACCOUNT_ID)},
    )
    assert billing.prepare_event(event) is None


def test_prepare_updated_uses_stripe_state_not_the_event_body(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc123")
    monkeypatch.setattr(
        billing.stripe.Subscription,
        "retrieve",
        lambda sub_id: _stripe_subscription(sub_id, status="active", cancel_at=1_702_592_000),
    )
    # A stale body: says past_due with no cancellation; Stripe now says active, cancel scheduled.
    stale = _stripe_subscription("sub_live", status="past_due")
    prepared = billing.prepare_event(_event("customer.subscription.updated", stale))
    assert prepared.kind == "subscription_changed"
    assert prepared.state.status == "active"
    assert prepared.state.cancel_at_period_end is True


def test_prepare_deleted_needs_no_stripe_call(monkeypatch):
    def _must_not_retrieve(sub_id):
        raise AssertionError("a deletion is terminal; no Stripe call needed")

    monkeypatch.setattr(billing.stripe.Subscription, "retrieve", _must_not_retrieve)
    prepared = billing.prepare_event(_event("customer.subscription.deleted", {"id": "sub_gone"}))
    assert prepared.kind == "subscription_deleted"
    assert prepared.stripe_subscription_id == "sub_gone"


# --- apply_event's duplicate guard, against a fake session --------------------------------------


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, account, row):
        self.account, self.row, self.added = account, row, []

    def get(self, model, key):
        return self.account if model is Account else None

    def execute(self, statement):
        return _Result(self.row)

    def add(self, obj):
        self.added.append(obj)


def _prepared_checkout(sub_id: str) -> billing.PreparedEvent:
    now = datetime.now(timezone.utc)
    state = billing.SubscriptionState(sub_id, "cus_unit", "active", now, now, False)
    return billing.PreparedEvent("checkout_completed", sub_id, state, ACCOUNT_ID)


def test_apply_keeps_a_live_subscription_when_a_second_checkout_completes():
    existing = Subscription(account_id=ACCOUNT_ID, stripe_subscription_id="sub_A", status="active")
    session = _FakeSession(Account(id=ACCOUNT_ID), existing)
    result = billing.apply_event(session, _prepared_checkout("sub_B"), datetime.now(timezone.utc))
    assert result.outcome == "duplicate_subscription_ignored"
    assert (result.existing_subscription_id, result.new_subscription_id) == ("sub_A", "sub_B")
    assert existing.stripe_subscription_id == "sub_A" and existing.status == "active"


def test_apply_ignores_a_checkout_for_an_unknown_account():
    session = _FakeSession(None, None)
    result = billing.apply_event(session, _prepared_checkout("sub_B"), datetime.now(timezone.utc))
    assert result.outcome == "unknown_account"
    assert session.added == []
