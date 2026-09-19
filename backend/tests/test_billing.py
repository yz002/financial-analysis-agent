"""
Tests for Phase B session 7b: the Stripe Checkout + webhook integration (design doc SS7.1/
SS7.4) built on session 7a's tier-gating logic and schema. Signature-verification and
idempotency tests never depend on real Stripe webhook config -- each sets its own fixed
STRIPE_WEBHOOK_SECRET via monkeypatch.setenv and signs payloads with that same value, using
Stripe's publicly documented manual-verification HMAC scheme (not stripe-python's private
test-only _compute_signature helper, so this can't silently break across SDK versions).

Like the other backend integration tests, these hit a real reachable Postgres via
DATABASE_URL (backend/.env). Three of the four event handlers (customer.subscription.updated/
.deleted, invoice.payment_failed) make no outbound Stripe API call at all, so those tests are
fully offline; checkout.session.completed's handler calls stripe.Subscription.retrieve, which
is monkeypatched here (the same MagicMock-external-API idiom test_csv_endpoints.py already
uses for the Anthropic client) -- except for test_checkout_session_creation_hits_real_sandbox,
which is deliberately NOT mocked, per the session brief's explicit requirement that
checkout-session creation be confirmed against the real sandbox account for at least one
request.

Event ids and Stripe object ids used in test payloads are randomized per test run (never a
literal fixed string) since stripe_webhook_events has no FK to accounts and so isn't cleaned
up by the account_ids fixture's cascading delete -- a fixed id would collide with a leftover
row from a prior run and silently trigger the idempotency short-circuit, invalidating the
test. webhook_event_ids below handles cleanup explicitly instead.
"""

import hashlib
import hmac
import json
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


def _seed_subscription(session, account_id: str, status: str, stripe_subscription_id: str) -> Subscription:
    now = datetime.now(timezone.utc)
    subscription = Subscription(
        account_id=uuid.UUID(account_id),
        stripe_customer_id="cus_existing",
        stripe_subscription_id=stripe_subscription_id,
        status=status,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
    )
    session.add(subscription)
    session.flush()
    return subscription


def _sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    signed = f"{ts}.{payload.decode()}".encode()
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event_payload(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}).encode()


def _post_webhook(payload: bytes, secret: str = _TEST_WEBHOOK_SECRET, sign: bool = True):
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["Stripe-Signature"] = _sign(payload, secret)
    return client.post("/v1/billing/webhook", content=payload, headers=headers)


def _subscription_updated_object(
    sub_id: str, status: str, period_start: int, period_end: int, cancel_at_period_end: bool = False
) -> dict:
    return {
        "id": sub_id,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "items": {"data": [{"current_period_start": period_start, "current_period_end": period_end}]},
    }


# --- signature verification -----------------------------------------------------------------


def test_webhook_rejects_missing_signature(monkeypatch, webhook_event_ids):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    event_id = _new_event_id(webhook_event_ids, "evt_missing_sig")
    payload = _event_payload(event_id, "customer.subscription.updated", {"id": "sub_x"})

    resp = _post_webhook(payload, sign=False)
    assert resp.status_code == 400, resp.text

    session = get_session()
    try:
        assert session.get(StripeWebhookEvent, event_id) is None
    finally:
        session.close()


def test_webhook_rejects_invalid_signature(monkeypatch, webhook_event_ids):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    event_id = _new_event_id(webhook_event_ids, "evt_bad_sig")
    payload = _event_payload(event_id, "customer.subscription.updated", {"id": "sub_x"})

    resp = client.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 400, resp.text

    session = get_session()
    try:
        assert session.get(StripeWebhookEvent, event_id) is None
    finally:
        session.close()


def test_webhook_accepts_validly_signed_event(monkeypatch, account_ids, webhook_event_ids):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_accept_{uuid.uuid4()}"
    session = get_session()
    try:
        _seed_subscription(session, account_id, "active", sub_id)
        session.commit()
    finally:
        session.close()

    event_id = _new_event_id(webhook_event_ids, "evt_accept")
    now = int(time.time())
    obj = _subscription_updated_object(sub_id, "past_due", now, now + 2592000)
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.updated", obj))
    assert resp.status_code == 200, resp.text


# --- idempotency ------------------------------------------------------------------------------


def test_webhook_idempotent_on_duplicate_event_id(monkeypatch, account_ids, webhook_event_ids):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_idempotent_{uuid.uuid4()}"
    session = get_session()
    try:
        _seed_subscription(session, account_id, "active", sub_id)
        session.commit()
    finally:
        session.close()

    event_id = _new_event_id(webhook_event_ids, "evt_idempotent")
    now = int(time.time())
    obj = _subscription_updated_object(sub_id, "past_due", now, now + 2592000)
    payload = _event_payload(event_id, "customer.subscription.updated", obj)

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

        row = session.execute(
            text("SELECT status FROM subscriptions WHERE stripe_subscription_id = :id"),
            {"id": sub_id},
        ).fetchone()
        assert row.status == "past_due"
    finally:
        session.close()


# --- checkout.session.completed ----------------------------------------------------------------


def test_checkout_session_completed_creates_subscription(monkeypatch, account_ids, webhook_event_ids):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    account_id = _new_account_id(account_ids)

    sub_id = f"sub_new_{uuid.uuid4()}"
    now = int(time.time())
    fake_subscription = {
        "id": sub_id,
        "status": "active",
        "cancel_at_period_end": False,
        "items": {"data": [{"current_period_start": now, "current_period_end": now + 2592000}]},
    }
    monkeypatch.setattr(app_billing.stripe.Subscription, "retrieve", lambda sub: fake_subscription)

    obj = {
        "id": f"cs_{uuid.uuid4()}",
        "client_reference_id": account_id,
        "customer": "cus_new",
        "subscription": sub_id,
    }
    event_id = _new_event_id(webhook_event_ids, "evt_checkout_completed")
    resp = _post_webhook(_event_payload(event_id, "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        row = session.execute(
            text(
                "SELECT account_id, status, stripe_subscription_id FROM subscriptions "
                "WHERE account_id = :id"
            ),
            {"id": account_id},
        ).fetchone()
        assert row is not None
        assert str(row.account_id) == account_id
        assert row.status == "active"
        assert row.stripe_subscription_id == sub_id
    finally:
        session.close()


def test_checkout_session_completed_upserts_existing_canceled_subscription(
    monkeypatch, account_ids, webhook_event_ids
):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    account_id = _new_account_id(account_ids)
    old_sub_id = f"sub_old_{uuid.uuid4()}"
    session = get_session()
    try:
        _seed_subscription(session, account_id, "canceled", old_sub_id)
        session.commit()
    finally:
        session.close()

    new_sub_id = f"sub_resub_{uuid.uuid4()}"
    now = int(time.time())
    fake_subscription = {
        "id": new_sub_id,
        "status": "active",
        "cancel_at_period_end": False,
        "items": {"data": [{"current_period_start": now, "current_period_end": now + 2592000}]},
    }
    monkeypatch.setattr(app_billing.stripe.Subscription, "retrieve", lambda sub: fake_subscription)

    obj = {
        "id": f"cs_{uuid.uuid4()}",
        "client_reference_id": account_id,
        "customer": "cus_resub",
        "subscription": new_sub_id,
    }
    event_id = _new_event_id(webhook_event_ids, "evt_checkout_resub")
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


# --- customer.subscription.updated (+ gating cross-check, session brief item 5) ----------------


def test_subscription_updated_syncs_status_and_period_and_gate_agrees(
    monkeypatch, account_ids, webhook_event_ids
):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_update_{uuid.uuid4()}"
    session = get_session()
    try:
        _seed_subscription(session, account_id, "active", sub_id)
        session.commit()
    finally:
        session.close()

    now_ts = int(time.time())
    period_start_ts = now_ts - 86400
    period_end_ts = now_ts + 2592000
    obj = _subscription_updated_object(sub_id, "active", period_start_ts, period_end_ts)
    event_id = _new_event_id(webhook_event_ids, "evt_update_gate")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.updated", obj))
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


def test_subscription_updated_noops_when_no_local_row_matches(monkeypatch, webhook_event_ids):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    now = int(time.time())
    obj = _subscription_updated_object(f"sub_unknown_{uuid.uuid4()}", "active", now, now + 2592000)
    event_id = _new_event_id(webhook_event_ids, "evt_update_unknown")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.updated", obj))
    assert resp.status_code == 200, resp.text  # doesn't error -- just nothing to update


# --- customer.subscription.deleted --------------------------------------------------------------


def test_subscription_deleted_sets_canceled_without_deleting_row(
    monkeypatch, account_ids, webhook_event_ids
):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_delete_{uuid.uuid4()}"
    session = get_session()
    try:
        _seed_subscription(session, account_id, "active", sub_id)
        session.commit()
    finally:
        session.close()

    obj = {"id": sub_id, "status": "canceled"}
    event_id = _new_event_id(webhook_event_ids, "evt_delete")
    resp = _post_webhook(_event_payload(event_id, "customer.subscription.deleted", obj))
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        row = session.execute(
            text("SELECT status FROM subscriptions WHERE stripe_subscription_id = :id"),
            {"id": sub_id},
        ).fetchone()
        assert row is not None  # soft update -- the row still exists
        assert row.status == "canceled"
    finally:
        session.close()


# --- invoice.payment_failed -----------------------------------------------------------------


def test_invoice_payment_failed_is_recorded_but_does_not_touch_subscriptions(
    monkeypatch, account_ids, webhook_event_ids
):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    account_id = _new_account_id(account_ids)
    sub_id = f"sub_invoice_{uuid.uuid4()}"
    session = get_session()
    try:
        _seed_subscription(session, account_id, "active", sub_id)
        session.commit()
    finally:
        session.close()

    obj = {"id": f"in_{uuid.uuid4()}", "subscription": sub_id}
    event_id = _new_event_id(webhook_event_ids, "evt_invoice_failed")
    resp = _post_webhook(_event_payload(event_id, "invoice.payment_failed", obj))
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        marker = session.get(StripeWebhookEvent, event_id)
        assert marker is not None
        assert marker.event_type == "invoice.payment_failed"

        row = session.execute(
            text("SELECT status FROM subscriptions WHERE stripe_subscription_id = :id"),
            {"id": sub_id},
        ).fetchone()
        assert row.status == "active"  # untouched by this event type
    finally:
        session.close()


# --- real checkout-session creation (deliberately unmocked) ----------------------------------


def test_checkout_session_creation_hits_real_sandbox(auth_session):
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
    monkeypatch, auth_session
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


def test_checkout_session_creation_omits_customer_email_when_primary_email_none(monkeypatch):
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
