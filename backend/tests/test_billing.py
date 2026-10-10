"""
Tests for Phase B session 7b: the Stripe Checkout + webhook integration (design doc SS7.1/
SS7.4) built on session 7a's tier-gating logic and schema, reworked in Phase D session 6
(EXTENSION_INTEGRATION.md SS6, backend/SECURITY.md SS8). Signature-verification and
idempotency tests never depend on real Stripe webhook config -- each sets its own fixed
STRIPE_WEBHOOK_SECRET (the stripe_env fixture) and signs payloads with that same value, using
Stripe's publicly documented manual-verification HMAC scheme (not stripe-python's private
test-only _compute_signature helper, so this can't silently break across SDK versions).

Like the other backend integration tests, these hit a real reachable Postgres via
DATABASE_URL (backend/.env). Since session 6 the webhook reads subscription state from
stripe.Subscription.retrieve rather than the event body, for checkout.session.completed and
customer.subscription.updated alike; every such test monkeypatches retrieve (the same
MagicMock-external-API idiom test_csv_endpoints.py uses for the Anthropic client), with a
dummy sk_test_ key that's never sent anywhere. customer.subscription.deleted needs no Stripe
call. test_checkout_session_creation_hits_real_sandbox is the one deliberately unmocked test,
and it skips unless backend/.env has a real sk_test_ key and STRIPE_PRICE_ID.

Event ids and Stripe object ids used in test payloads are randomized per test run (never a
literal fixed string) since stripe_webhook_events has no FK to accounts and so isn't cleaned
up by the account_ids fixture's cascading delete -- a fixed id would collide with a leftover
row from a prior run and silently trigger the idempotency short-circuit, invalidating the
test. webhook_event_ids below handles cleanup explicitly instead.
"""

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.billing as app_billing
from app.gating import evaluate_ask_gate
from app.main import app
from db.base import get_session
from db.models import Account, StripeWebhookEvent, Subscription

client = TestClient(app)

_TEST_WEBHOOK_SECRET = "whsec_test_secret_for_pytest"
# A syntactically test-mode key, never sent anywhere: every test using it mocks the Stripe
# call it would authenticate (session 6's test-mode guard needs the sk_test_ prefix).
_DUMMY_TEST_KEY = "sk_test_dummy_for_pytest"


@pytest.fixture
def stripe_env(monkeypatch):
    """The webhook secret plus a dummy test-mode key and price, independent of backend/.env."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    monkeypatch.setenv("STRIPE_SECRET_KEY", _DUMMY_TEST_KEY)
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_dummy_for_pytest")


@pytest.fixture
def webhook_event_ids():
    """Tracks stripe_event_ids created by a test; deletes them after (stripe_webhook_events
    has no FK to accounts, so account_ids' cascade doesn't cover it)."""
    ids: list[str] = []
    yield ids
    if not ids:
        return
    session = get_session()
    try:
        session.execute(
            text("DELETE FROM stripe_webhook_events WHERE stripe_event_id = ANY(:ids)"),
            {"ids": ids},
        )
        session.commit()
    finally:
        session.close()


def _new_event_id(webhook_event_ids: list[str], prefix: str) -> str:
    event_id = f"{prefix}_{uuid.uuid4()}"
    webhook_event_ids.append(event_id)
    return event_id


def _new_account_id(account_ids: list[str]) -> str:
    """Seeds a bare accounts row directly (webhook tests never authenticate -- /v1/billing/webhook
    stays unauthenticated -- so there's no /v1/auth/exchange call to create the account here)."""
    account_id = uuid.uuid4()
    session = get_session()
    try:
        session.add(Account(id=account_id, last_seen_at=datetime.now(timezone.utc)))
        session.commit()
    finally:
        session.close()
    account_ids.append(str(account_id))
    return str(account_id)


def _seed_subscription(
    session, account_id: str, status: str, stripe_subscription_id: str, customer: str = "cus_existing"
) -> Subscription:
    now = datetime.now(timezone.utc)
    subscription = Subscription(
        account_id=uuid.UUID(account_id),
        stripe_customer_id=customer,
        stripe_subscription_id=stripe_subscription_id,
        status=status,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
    )
    session.add(subscription)
    session.flush()
    return subscription


def _seed(account_id: str, status: str, sub_id: str, customer: str = "cus_existing") -> None:
    session = get_session()
    try:
        _seed_subscription(session, account_id, status, sub_id, customer)
        session.commit()
    finally:
        session.close()


def _subscription_row(account_id: str):
    session = get_session()
    try:
        return session.execute(
            text(
                "SELECT stripe_subscription_id, stripe_customer_id, status, cancel_at_period_end, "
                "current_period_start, current_period_end FROM subscriptions WHERE account_id = :id"
            ),
            {"id": account_id},
        ).fetchone()
    finally:
        session.close()


def _marker_exists(event_id: str) -> bool:
    session = get_session()
    try:
        return session.get(StripeWebhookEvent, event_id) is not None
    finally:
        session.close()


def _sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    signed = f"{ts}.{payload.decode()}".encode()
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event_payload(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps(
        {"id": event_id, "type": event_type, "livemode": False, "data": {"object": obj}}
    ).encode()


def _post_webhook(payload: bytes, secret: str = _TEST_WEBHOOK_SECRET, sign: bool = True):
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["Stripe-Signature"] = _sign(payload, secret)
    return client.post("/v1/billing/webhook", content=payload, headers=headers)


def _stripe_subscription(
    sub_id: str,
    status: str = "active",
    customer: str = "cus_pytest",
    period_start: int | None = None,
    period_end: int | None = None,
    cancel_at_period_end: bool = False,
) -> dict:
    """What stripe.Subscription.retrieve returns (and what a subscription event body carries),
    as a plain dict -- billing._get handles plain dicts and StripeObjects alike."""
    start = period_start if period_start is not None else int(time.time())
    end = period_end if period_end is not None else start + 2592000
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "cancel_at": None,
        "items": {"data": [{"current_period_start": start, "current_period_end": end}]},
    }


def _mock_retrieve(monkeypatch, subscription: dict) -> list[str]:
    """Makes stripe.Subscription.retrieve return `subscription`; returns the ids it was asked
    for, so a test can assert whether Stripe was called at all."""
    calls: list[str] = []

    def _retrieve(sub_id):
        calls.append(sub_id)
        return subscription

    monkeypatch.setattr(app_billing.stripe.Subscription, "retrieve", _retrieve)
    return calls


def _checkout_completed_object(account_id: str, sub_id: str, mode: str = "subscription") -> dict:
    return {
        "id": f"cs_{uuid.uuid4()}",
        "mode": mode,
        "client_reference_id": account_id,
        "customer": "cus_from_checkout",
        "subscription": sub_id,
    }


# --- signature verification -----------------------------------------------------------------


def test_webhook_rejects_missing_signature(stripe_env, webhook_event_ids):
    event_id = _new_event_id(webhook_event_ids, "evt_missing_sig")
    payload = _event_payload(event_id, "customer.subscription.updated", {"id": "sub_x"})

    resp = _post_webhook(payload, sign=False)
    assert resp.status_code == 400, resp.text
    assert not _marker_exists(event_id)


def test_webhook_rejects_invalid_signature(stripe_env, webhook_event_ids):
    event_id = _new_event_id(webhook_event_ids, "evt_bad_sig")
    payload = _event_payload(event_id, "customer.subscription.updated", {"id": "sub_x"})

    resp = client.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 400, resp.text
    assert not _marker_exists(event_id)


def test_webhook_accepts_validly_signed_event(stripe_env, monkeypatch, account_ids, webhook_event_ids):
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_accept_{uuid.uuid4()}"
    _seed(account_id, "active", sub_id)
    _mock_retrieve(monkeypatch, _stripe_subscription(sub_id, "past_due"))

    event_id = _new_event_id(webhook_event_ids, "evt_accept")
    obj = _stripe_subscription(sub_id, "past_due")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.updated", obj))
    assert resp.status_code == 200, resp.text


# --- idempotency ------------------------------------------------------------------------------


def test_webhook_idempotent_on_duplicate_event_id(stripe_env, monkeypatch, account_ids, webhook_event_ids):
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_idempotent_{uuid.uuid4()}"
    _seed(account_id, "active", sub_id)
    _mock_retrieve(monkeypatch, _stripe_subscription(sub_id, "past_due"))

    event_id = _new_event_id(webhook_event_ids, "evt_idempotent")
    payload = _event_payload(event_id, "customer.subscription.updated", _stripe_subscription(sub_id, "past_due"))

    resp_1 = _post_webhook(payload)
    assert resp_1.status_code == 200, resp_1.text
    resp_2 = _post_webhook(payload)
    assert resp_2.status_code == 200, resp_2.text

    session = get_session()
    try:
        count = session.execute(
            text("SELECT COUNT(*) FROM stripe_webhook_events WHERE stripe_event_id = :id"),
            {"id": event_id},
        ).scalar()
        assert count == 1
    finally:
        session.close()
    assert _subscription_row(account_id).status == "past_due"


def test_redelivered_event_never_calls_stripe(stripe_env, monkeypatch, account_ids, webhook_event_ids):
    """Session 6: the marker is read first, so a redelivery is acknowledged with no Stripe
    call and no change -- even when Stripe's current state has moved on since."""
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_redeliver_{uuid.uuid4()}"
    _seed(account_id, "active", sub_id)
    calls = _mock_retrieve(monkeypatch, _stripe_subscription(sub_id, "past_due"))

    event_id = _new_event_id(webhook_event_ids, "evt_redeliver")
    payload = _event_payload(event_id, "customer.subscription.updated", _stripe_subscription(sub_id))
    assert _post_webhook(payload).status_code == 200
    assert calls == [sub_id]

    calls_after = _mock_retrieve(monkeypatch, _stripe_subscription(sub_id, "unpaid"))
    assert _post_webhook(payload).status_code == 200
    assert calls_after == []
    assert _subscription_row(account_id).status == "past_due"


# --- checkout.session.completed ----------------------------------------------------------------


def test_checkout_session_completed_creates_subscription(stripe_env, monkeypatch, account_ids, webhook_event_ids):
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_new_{uuid.uuid4()}"
    _mock_retrieve(monkeypatch, _stripe_subscription(sub_id, "active", customer="cus_new"))

    event_id = _new_event_id(webhook_event_ids, "evt_checkout_completed")
    obj = _checkout_completed_object(account_id, sub_id)
    resp = _post_webhook(_event_payload(event_id, "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text

    row = _subscription_row(account_id)
    assert row is not None
    assert row.status == "active"
    assert row.stripe_subscription_id == sub_id
    assert row.stripe_customer_id == "cus_new"  # from the retrieved subscription
    assert row.cancel_at_period_end is False


def test_checkout_session_completed_upserts_existing_canceled_subscription(
    stripe_env, monkeypatch, account_ids, webhook_event_ids
):
    account_id = _new_account_id(account_ids)
    old_sub_id = f"sub_old_{uuid.uuid4()}"
    _seed(account_id, "canceled", old_sub_id)

    new_sub_id = f"sub_resub_{uuid.uuid4()}"
    _mock_retrieve(monkeypatch, _stripe_subscription(new_sub_id, "active", customer="cus_existing"))

    event_id = _new_event_id(webhook_event_ids, "evt_checkout_resub")
    obj = _checkout_completed_object(account_id, new_sub_id)
    resp = _post_webhook(_event_payload(event_id, "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        rows = session.execute(
            text("SELECT stripe_subscription_id, status FROM subscriptions WHERE account_id = :id"),
            {"id": account_id},
        ).fetchall()
        # Upsert, not a second row -- subscriptions.account_id is UNIQUE, so a blind insert
        # here would have raised instead.
        assert len(rows) == 1
        assert rows[0].stripe_subscription_id == new_sub_id
        assert rows[0].status == "active"
    finally:
        session.close()


def test_checkout_completed_keeps_existing_live_subscription_and_logs_both_ids(
    stripe_env, monkeypatch, caplog, account_ids, webhook_event_ids
):
    """Session 6 duplicate guard: two checkouts created before either completed. The second
    must not overwrite (and so orphan) the live subscription the account already has."""
    account_id = _new_account_id(account_ids)
    sub_a = f"sub_A_{uuid.uuid4()}"
    sub_b = f"sub_B_{uuid.uuid4()}"
    _seed(account_id, "active", sub_a)
    before = _subscription_row(account_id)
    _mock_retrieve(monkeypatch, _stripe_subscription(sub_b, "active", customer="cus_second"))

    event_id = _new_event_id(webhook_event_ids, "evt_checkout_duplicate")
    obj = _checkout_completed_object(account_id, sub_b)
    obj["customer_email"] = "duplicate-owner@example.com"
    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = _post_webhook(_event_payload(event_id, "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text

    after = _subscription_row(account_id)
    assert after.stripe_subscription_id == sub_a
    assert after.status == "active"
    assert after.stripe_customer_id == before.stripe_customer_id
    assert after.current_period_end == before.current_period_end
    assert _marker_exists(event_id)  # acknowledged once, not retried

    warnings = [r.getMessage() for r in caplog.records if "duplicate subscription" in r.getMessage()]
    assert len(warnings) == 1
    assert sub_a in warnings[0] and sub_b in warnings[0]
    assert "duplicate-owner@example.com" not in caplog.text


def test_checkout_completed_for_unknown_account_is_acknowledged_without_a_row(
    stripe_env, monkeypatch, webhook_event_ids
):
    unknown_account_id = str(uuid.uuid4())  # never seeded
    sub_id = f"sub_unknown_acct_{uuid.uuid4()}"
    _mock_retrieve(monkeypatch, _stripe_subscription(sub_id))

    event_id = _new_event_id(webhook_event_ids, "evt_checkout_unknown_account")
    obj = _checkout_completed_object(unknown_account_id, sub_id)
    resp = _post_webhook(_event_payload(event_id, "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text  # not a 500 Stripe would retry for days
    assert _subscription_row(unknown_account_id) is None
    assert _marker_exists(event_id)


def test_checkout_completed_that_is_not_a_subscription_is_ignored(
    stripe_env, monkeypatch, account_ids, webhook_event_ids
):
    account_id = _new_account_id(account_ids)
    calls = _mock_retrieve(monkeypatch, _stripe_subscription("sub_never"))

    event_id = _new_event_id(webhook_event_ids, "evt_checkout_payment_mode")
    obj = _checkout_completed_object(account_id, None, mode="payment")
    resp = _post_webhook(_event_payload(event_id, "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text
    assert calls == []
    assert _subscription_row(account_id) is None
    assert _marker_exists(event_id)


# --- customer.subscription.updated (+ gating cross-check, session brief item 5) ----------------


def test_subscription_updated_syncs_status_and_period_and_gate_agrees(
    stripe_env, monkeypatch, account_ids, webhook_event_ids
):
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_update_{uuid.uuid4()}"
    _seed(account_id, "active", sub_id)

    now_ts = int(time.time())
    period_start_ts = now_ts - 86400
    period_end_ts = now_ts + 2592000
    current = _stripe_subscription(sub_id, "active", period_start=period_start_ts, period_end=period_end_ts)
    _mock_retrieve(monkeypatch, current)
    event_id = _new_event_id(webhook_event_ids, "evt_update_gate")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.updated", current))
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        row = session.execute(
            text(
                "SELECT current_period_start, current_period_end FROM subscriptions "
                "WHERE stripe_subscription_id = :id"
            ),
            {"id": sub_id},
        ).fetchone()
        # Confirms the Unix-epoch -> tz-aware datetime conversion round-trips correctly --
        # session 7a's tests only ever seeded plain Python datetimes directly, never a value
        # that went through this conversion.
        assert row.current_period_start.timestamp() == pytest.approx(period_start_ts, abs=1)
        assert row.current_period_end.timestamp() == pytest.approx(period_end_ts, abs=1)

        account = session.get(Account, uuid.UUID(account_id))
        decision = evaluate_ask_gate(session, account, datetime.now(timezone.utc))
        assert decision.tier == "paid"
        assert decision.allowed is True
    finally:
        session.close()


def test_subscription_updated_writes_stripe_state_not_a_stale_event_body(
    stripe_env, monkeypatch, account_ids, webhook_event_ids
):
    """Session 6: a late or redelivered older event can't roll the subscription back. The
    body here says active with no cancellation; Stripe's current state has a cancellation
    scheduled, and that's what gets written."""
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_stale_{uuid.uuid4()}"
    _seed(account_id, "active", sub_id)
    _mock_retrieve(monkeypatch, _stripe_subscription(sub_id, "active", cancel_at_period_end=True))

    stale_body = _stripe_subscription(sub_id, "active", cancel_at_period_end=False)
    event_id = _new_event_id(webhook_event_ids, "evt_update_stale")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.updated", stale_body))
    assert resp.status_code == 200, resp.text
    assert _subscription_row(account_id).cancel_at_period_end is True


def test_subscription_updated_noops_when_no_local_row_matches(stripe_env, monkeypatch, webhook_event_ids):
    sub_id = f"sub_unknown_{uuid.uuid4()}"
    _mock_retrieve(monkeypatch, _stripe_subscription(sub_id))
    event_id = _new_event_id(webhook_event_ids, "evt_update_unknown")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.updated", _stripe_subscription(sub_id)))
    assert resp.status_code == 200, resp.text  # doesn't error -- just nothing to update


# --- customer.subscription.deleted --------------------------------------------------------------


def test_subscription_deleted_sets_canceled_without_deleting_row_or_calling_stripe(
    stripe_env, monkeypatch, account_ids, webhook_event_ids
):
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_delete_{uuid.uuid4()}"
    _seed(account_id, "active", sub_id)
    calls = _mock_retrieve(monkeypatch, _stripe_subscription(sub_id, "canceled"))

    obj = {"id": sub_id, "status": "canceled"}
    event_id = _new_event_id(webhook_event_ids, "evt_delete")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.deleted", obj))
    assert resp.status_code == 200, resp.text

    row = _subscription_row(account_id)
    assert row is not None  # soft update -- the row still exists
    assert row.status == "canceled"
    assert calls == []  # a deletion is terminal; nothing to re-read


# --- invoice.payment_failed -----------------------------------------------------------------


def test_invoice_payment_failed_is_recorded_but_does_not_touch_subscriptions(
    stripe_env, account_ids, webhook_event_ids
):
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_invoice_{uuid.uuid4()}"
    _seed(account_id, "active", sub_id)

    obj = {"id": f"in_{uuid.uuid4()}", "subscription": sub_id}
    event_id = _new_event_id(webhook_event_ids, "evt_invoice_failed")
    resp = _post_webhook(_event_payload(event_id, "invoice.payment_failed", obj))
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        marker = session.get(StripeWebhookEvent, event_id)
        assert marker is not None
        assert marker.event_type == "invoice.payment_failed"
    finally:
        session.close()
    assert _subscription_row(account_id).status == "active"  # untouched by this event type


# --- checkout-session route ----------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "unpaid"])
def test_checkout_session_refused_while_a_subscription_is_live(stripe_env, monkeypatch, auth_session, status):
    account_id, headers = auth_session()
    _seed(account_id, status, f"sub_live_{uuid.uuid4()}")

    def _must_not_create(**kwargs):
        raise AssertionError("no checkout while a subscription is live")

    monkeypatch.setattr(app_billing.stripe.checkout.Session, "create", _must_not_create)
    resp = client.post("/v1/billing/checkout-session", headers=headers)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": {"error": "subscription_exists"}}


def test_checkout_session_reuses_the_customer_after_a_cancel(stripe_env, monkeypatch, auth_session):
    account_id, headers = auth_session(email="returning@example.com")
    _seed(account_id, "canceled", f"sub_ended_{uuid.uuid4()}", customer="cus_returning")
    captured = {}

    class _FakeSession:
        url = "https://checkout.stripe.com/fake-returning"

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(app_billing.stripe.checkout.Session, "create", _fake_create)
    resp = client.post("/v1/billing/checkout-session", headers=headers)
    assert resp.status_code == 200, resp.text
    assert captured["customer"] == "cus_returning"
    assert "customer_email" not in captured


# --- portal-session route ----------------------------------------------------------------


def test_portal_session_404_without_a_subscription(stripe_env, monkeypatch, auth_session):
    _, headers = auth_session()

    def _must_not_create(**kwargs):
        raise AssertionError("no portal without a billing account")

    monkeypatch.setattr(app_billing.stripe.billing_portal.Session, "create", _must_not_create)
    resp = client.post("/v1/billing/portal-session", headers=headers)
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": {"error": "no_billing_account"}}


def test_portal_session_uses_only_the_callers_own_customer(stripe_env, monkeypatch, auth_session):
    account_a, headers_a = auth_session()
    account_b, _ = auth_session()
    _seed(account_a, "active", f"sub_a_{uuid.uuid4()}", customer="cus_account_a")
    _seed(account_b, "active", f"sub_b_{uuid.uuid4()}", customer="cus_account_b")
    captured = {}

    class _FakePortal:
        url = "https://billing.stripe.com/p/session/fake"

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _FakePortal()

    monkeypatch.setattr(app_billing.stripe.billing_portal.Session, "create", _fake_create)
    # A body naming another customer is ignored: the route takes none.
    resp = client.post(
        "/v1/billing/portal-session", headers=headers_a, json={"customer": "cus_account_b"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"portal_url": "https://billing.stripe.com/p/session/fake"}
    assert captured["customer"] == "cus_account_a"
    assert captured["return_url"].endswith("/v1/billing/return")


# --- real checkout-session creation (deliberately unmocked) ----------------------------------


@pytest.mark.real_stripe  # exempt from conftest's network guard -- the only such test
@pytest.mark.skipif(
    not os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_")
    or not os.environ.get("STRIPE_PRICE_ID"),
    reason="needs a real Stripe test-mode key (sk_test_) and STRIPE_PRICE_ID in backend/.env",
)
def test_checkout_session_creation_hits_real_sandbox(auth_session):
    # backend/.env is loaded by db.base at import (above), before this marker is evaluated.
    _, headers = auth_session()
    resp = client.post("/v1/billing/checkout-session", headers=headers)
    assert resp.status_code == 200, resp.text
    checkout_url = resp.json()["checkout_url"]
    assert checkout_url.startswith("https://checkout.stripe.com/")


# --- checkout-session field prefill (mocked -- verifies what the sandbox test can't) ---------
# Phase C session 5: the sandbox test above only confirms an HTTP 200 and a checkout.stripe.com
# URL, never that account_id/primary_email actually reached Stripe. These mock
# stripe.checkout.Session.create (the same idiom used above for stripe.Subscription.retrieve) to
# assert on the exact kwargs create_checkout_session builds.


def test_checkout_session_creation_sets_client_reference_id_and_customer_email(
    stripe_env, monkeypatch, auth_session
):
    account_id, headers = auth_session(email="prefill-check@example.com")
    captured = {}

    class _FakeSession:
        url = "https://checkout.stripe.com/fake-session"

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(app_billing.stripe.checkout.Session, "create", _fake_create)

    resp = client.post("/v1/billing/checkout-session", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/fake-session"
    assert captured["client_reference_id"] == account_id
    assert captured["customer_email"] == "prefill-check@example.com"


def test_checkout_session_creation_omits_customer_email_when_primary_email_none(stripe_env, monkeypatch):
    """create_checkout_session is a pure function taking primary_email directly, so the
    nullable-primary_email path (design doc SS4's schema marks it nullable) is exercised
    directly rather than via auth_session, which always derives a non-null email."""
    captured = {}

    class _FakeSession:
        url = "https://checkout.stripe.com/fake-no-email"

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(app_billing.stripe.checkout.Session, "create", _fake_create)

    account_id = uuid.uuid4()
    url = app_billing.create_checkout_session(
        account_id,
        None,
        "price_test_123",
        "https://example.com/success",
        "https://example.com/cancel",
    )
    assert url == "https://checkout.stripe.com/fake-no-email"
    assert captured["client_reference_id"] == str(account_id)
    assert "customer_email" not in captured
