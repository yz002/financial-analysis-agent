"""
Stripe integration (Phase B session 7b, design doc SS7.1/SS7.4; reworked Phase D session 6):
Checkout and customer-portal session creation, and webhook event handling. Split from route
glue in main.py the same way gating.py is -- no HTTPException raised from in here; main.py
translates BillingUnavailable and Stripe errors into responses.

Stripe's "Basil" API version (2025-03-31) removed current_period_start/current_period_end
from the top-level Subscription object -- they now live on each SubscriptionItem instead
(subscription["items"]["data"][0]["current_period_start"]). Confirmed via web search
(docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end),
not assumed -- a fresh sandbox account defaults to Basil or later, so the old top-level
fields would silently read back empty rather than error. _subscription_state below reads
the items-based path accordingly; this codebase only ever creates single-item subscriptions
(one $2/mo price, quantity 1), so items.data[0] is always the right (and only) item.

Phase D session 6 (EXTENSION_INTEGRATION.md SS6, backend/SECURITY.md SS8):
- Test-mode only: _ensure_stripe_configured refuses any secret key that isn't sk_test_/rk_test_
  by raising BillingUnavailable, which the billing routes report as 503 billing_unavailable.
  Only billing is refused; the rest of the service runs.
- Webhook processing is two steps. prepare_event makes the Stripe call (network, no DB) and
  returns plain values; apply_event writes them (DB, no network). main.py runs prepare_event
  before opening a DB session, so a slow Stripe call never holds a transaction or the
  idempotency marker's lock.
- Subscription state comes from stripe.Subscription.retrieve at processing time, never from
  the event body, so a late or out-of-order customer.subscription.updated can't roll a
  subscription back to an older state.

STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET/STRIPE_PRICE_ID are read fresh from os.environ inside
the functions that need them, never cached at module import -- matching db/base.py's
get_database_url() convention (fail at call time, not import time; also lets tests
monkeypatch.setenv cleanly).
"""

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import stripe
from sqlalchemy import select

from db.models import Account, Subscription

# Only these keys are accepted while billing is in its test-mode phase. A restricted test key
# (rk_test_) is as safe as a standard one (sk_test_).
_TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")

# A subscription in one of these states is still live in Stripe: it bills, or will retry
# billing. A new checkout is refused while one exists (409 subscription_exists), and a second
# completed checkout never replaces it (apply_event's duplicate guard).
LIVE_SUBSCRIPTION_STATUSES = ("active", "trialing", "past_due", "unpaid")

# Stripe's minimum Checkout Session lifetime is 30 minutes, measured from when Stripe creates
# the session. expires_at is computed here, before the request reaches Stripe, so exactly 30
# minutes could land a second or two under the minimum and be rejected; the extra minute
# absorbs request latency and clock skew. A short lifetime narrows the window in which two
# open checkouts for one account can both complete.
CHECKOUT_SESSION_LIFETIME = timedelta(minutes=31)


class BillingUnavailable(Exception):
    """Billing can't be used on this server: a STRIPE_* value is missing, or the secret key
    isn't a test-mode key. str(e) names the failed check, never a value, so it's safe to log."""


def _get(obj, key: str, default=None):
    """
    obj["key"] works uniformly across a real stripe.StripeObject (parsed by
    construct_event) and a plain dict (used by this module's own tests) -- but
    StripeObject deliberately does NOT support .get() ("StripeObject is not a dict",
    confirmed empirically against the installed SDK), so an optional field read needs this
    instead of the dict.get(key, default) idiom used everywhere else in this codebase.
    """
    return obj[key] if key in obj else default


def _ensure_stripe_configured() -> None:
    api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not api_key:
        raise BillingUnavailable("STRIPE_SECRET_KEY is not set")
    if not api_key.startswith(_TEST_KEY_PREFIXES):
        raise BillingUnavailable("STRIPE_SECRET_KEY is not a test-mode key")
    stripe.api_key = api_key


def ensure_configured() -> None:
    """The secret-key check on its own, for routes that check config before any DB access."""
    _ensure_stripe_configured()


def checkout_price_id() -> str:
    price_id = os.environ.get("STRIPE_PRICE_ID")
    if not price_id:
        raise BillingUnavailable("STRIPE_PRICE_ID is not set")
    return price_id


def webhook_secret() -> str:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise BillingUnavailable("STRIPE_WEBHOOK_SECRET is not set")
    return secret


def verify_webhook_event(payload: bytes, sig_header: str, webhook_secret: str) -> stripe.Event:
    """
    Raises ValueError (malformed payload) or stripe.SignatureVerificationError (bad/missing
    signature, or timestamp outside Stripe's replay-protection tolerance) -- the caller
    (main.py's /v1/billing/webhook route) catches both and rejects with 400 before any DB
    access happens.
    """
    return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)


def is_livemode(event) -> bool:
    return bool(_get(event, "livemode", False))


def checkout_session_kwargs(
    account_id: uuid.UUID,
    primary_email: str | None,
    price_id: str,
    success_url: str,
    cancel_url: str,
    existing_customer_id: str | None,
    now: datetime,
) -> dict:
    """
    The Checkout Session's parameters, pure so they can be tested without Stripe.

    A returning customer (an earlier, now-ended subscription) is checked out as the same
    Stripe customer, so their billing history stays under one customer in the portal. Stripe
    refuses customer and customer_email together, so the email prefill is only for a first
    checkout. account_id is also recorded in metadata on the session and the subscription,
    for tracing a subscription back to an account from the Stripe dashboard; the webhook still
    reads the account only from client_reference_id.
    """
    kwargs = {
        "mode": "subscription",
        "client_reference_id": str(account_id),
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "expires_at": int((now + CHECKOUT_SESSION_LIFETIME).timestamp()),
        "metadata": {"account_id": str(account_id)},
        "subscription_data": {"metadata": {"account_id": str(account_id)}},
    }
    if existing_customer_id:
        kwargs["customer"] = existing_customer_id
    elif primary_email:
        kwargs["customer_email"] = primary_email
    return kwargs


def create_checkout_session(
    account_id: uuid.UUID,
    primary_email: str | None,
    price_id: str,
    success_url: str,
    cancel_url: str,
    existing_customer_id: str | None = None,
) -> str:
    """
    Takes plain values, not a db.models.Account row -- the caller (main.py) reads these off
    the Account (via get_current_account) and its subscriptions row before making this
    outbound Stripe call, so a slow network call never holds a DB connection open. Returns the
    Checkout Session's hosted URL. Raises BillingUnavailable or stripe.StripeError.
    """
    _ensure_stripe_configured()
    kwargs = checkout_session_kwargs(
        account_id,
        primary_email,
        price_id,
        success_url,
        cancel_url,
        existing_customer_id,
        datetime.now(timezone.utc),
    )
    checkout_session = stripe.checkout.Session.create(**kwargs)
    return checkout_session.url


def create_portal_session(customer_id: str, return_url: str) -> str:
    """Stripe's hosted customer portal for one customer. The caller supplies the customer id
    from the caller's own subscriptions row, never from the request. Raises BillingUnavailable
    or stripe.StripeError."""
    _ensure_stripe_configured()
    portal_session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return portal_session.url


@dataclass(frozen=True)
class SubscriptionState:
    """A Stripe subscription's current state as plain values, read in prepare_event."""

    stripe_subscription_id: str
    stripe_customer_id: str
    status: str
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool


@dataclass(frozen=True)
class PreparedEvent:
    kind: Literal["checkout_completed", "subscription_changed", "subscription_deleted"]
    stripe_subscription_id: str
    state: SubscriptionState | None  # None for subscription_deleted, which needs no Stripe call
    account_id: uuid.UUID | None  # checkout_completed only


@dataclass(frozen=True)
class ApplyResult:
    """What apply_event did, for main.py's logging -- ids only, never an email."""

    outcome: Literal[
        "applied", "no_local_row", "unknown_account", "duplicate_subscription_ignored", "ignored"
    ]
    existing_subscription_id: str | None = None
    new_subscription_id: str | None = None


def cancellation_scheduled(subscription) -> bool:
    """
    Whether a cancellation is scheduled. Stripe can record it as cancel_at_period_end or as a
    cancel_at date (newer API versions use cancel_at for some portal cancellations); either
    one means the subscription ends unless renewed. Which one the test-mode portal sets is
    pending live verification (live-test step L7).
    """
    return bool(_get(subscription, "cancel_at_period_end", False)) or (
        _get(subscription, "cancel_at") is not None
    )


def _customer_id(subscription) -> str:
    customer = subscription["customer"]
    return customer if isinstance(customer, str) else customer["id"]


def _subscription_state(subscription) -> SubscriptionState:
    item = subscription["items"]["data"][0]
    return SubscriptionState(
        stripe_subscription_id=subscription["id"],
        stripe_customer_id=_customer_id(subscription),
        status=subscription["status"],
        current_period_start=datetime.fromtimestamp(item["current_period_start"], tz=timezone.utc),
        current_period_end=datetime.fromtimestamp(item["current_period_end"], tz=timezone.utc),
        cancel_at_period_end=cancellation_scheduled(subscription),
    )


def _parse_account_id(raw) -> uuid.UUID | None:
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def prepare_event(event) -> PreparedEvent | None:
    """
    Network only, no DB: reads what apply_event needs. Returns None for an event this
    service ignores -- an unhandled type (invoice.payment_failed included: status changes
    arrive as customer.subscription.updated), a checkout that isn't a subscription, or a
    checkout whose client_reference_id isn't an account id. The caller still records the
    event's marker, so an ignored event is acknowledged once and never retried.

    Raises BillingUnavailable, or stripe.StripeError if Stripe can't be reached -- the caller
    then fails the request (no marker written) so Stripe retries later.
    """
    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        if _get(obj, "mode") != "subscription" or not _get(obj, "subscription"):
            return None
        account_id = _parse_account_id(_get(obj, "client_reference_id"))
        if account_id is None:
            return None
        subscription_id = obj["subscription"]
        if not isinstance(subscription_id, str):
            subscription_id = subscription_id["id"]
        _ensure_stripe_configured()
        state = _subscription_state(stripe.Subscription.retrieve(subscription_id))
        return PreparedEvent("checkout_completed", state.stripe_subscription_id, state, account_id)

    if event_type == "customer.subscription.updated":
        _ensure_stripe_configured()
        state = _subscription_state(stripe.Subscription.retrieve(obj["id"]))
        return PreparedEvent("subscription_changed", state.stripe_subscription_id, state, None)

    if event_type == "customer.subscription.deleted":
        # Terminal: nothing can follow a deletion for this subscription id, so the event body
        # can't be stale and no Stripe call is needed.
        return PreparedEvent("subscription_deleted", obj["id"], None, None)

    return None


def _write_state(row: Subscription, state: SubscriptionState, now: datetime) -> None:
    row.stripe_customer_id = state.stripe_customer_id
    row.stripe_subscription_id = state.stripe_subscription_id
    row.status = state.status
    row.current_period_start = state.current_period_start
    row.current_period_end = state.current_period_end
    row.cancel_at_period_end = state.cancel_at_period_end
    row.updated_at = now


def _apply_checkout_completed(session, prepared: PreparedEvent, now: datetime) -> ApplyResult:
    state = prepared.state
    if session.get(Account, prepared.account_id) is None:
        # A deleted account, or a reference this service never issued. Acknowledged rather
        # than failed: a 500 would have Stripe retry it for days to no effect.
        return ApplyResult("unknown_account", new_subscription_id=state.stripe_subscription_id)

    # Upsert keyed by account_id, not a blind insert: subscriptions.account_id is UNIQUE
    # (design doc SS7.3), and a user who canceled and is now re-subscribing gets a brand new
    # stripe_subscription_id -- a blind insert would violate that constraint.
    row = session.execute(
        select(Subscription).where(Subscription.account_id == prepared.account_id)
    ).scalar_one_or_none()
    if row is None:
        row = Subscription(account_id=prepared.account_id)
        _write_state(row, state, now)
        session.add(row)
        return ApplyResult("applied", new_subscription_id=state.stripe_subscription_id)

    if (
        row.stripe_subscription_id != state.stripe_subscription_id
        and row.status in LIVE_SUBSCRIPTION_STATUSES
    ):
        # Two checkouts created before either completed both succeeded. Keep the subscription
        # this account already has: overwriting it would orphan a live subscription that
        # Stripe keeps billing. The new one is logged by the caller for manual cancel and
        # refund (backend/DEPLOYMENT.md, duplicate subscription runbook).
        return ApplyResult(
            "duplicate_subscription_ignored",
            existing_subscription_id=row.stripe_subscription_id,
            new_subscription_id=state.stripe_subscription_id,
        )

    _write_state(row, state, now)
    return ApplyResult("applied", new_subscription_id=state.stripe_subscription_id)


def apply_event(session, prepared: PreparedEvent | None, now: datetime) -> ApplyResult:
    """DB only, no network: writes what prepare_event read, inside the caller's transaction
    (the same one that inserted the event's stripe_webhook_events marker)."""
    if prepared is None:
        return ApplyResult("ignored")

    if prepared.kind == "checkout_completed":
        return _apply_checkout_completed(session, prepared, now)

    row = session.execute(
        select(Subscription).where(
            Subscription.stripe_subscription_id == prepared.stripe_subscription_id
        )
    ).scalar_one_or_none()
    if row is None:
        # No local row: a subscription that was replaced, a duplicate kept out by the guard
        # above, or an update ahead of its checkout.session.completed. That checkout event
        # re-reads the subscription from Stripe itself, so nothing is lost by skipping this.
        return ApplyResult("no_local_row")

    if prepared.kind == "subscription_deleted":
        # Soft update -- never a row delete, matching byo_keys.is_active's audit-trail
        # convention (design doc SS7.1).
        row.status = "canceled"
        row.updated_at = now
    else:
        _write_state(row, prepared.state, now)
    return ApplyResult("applied")
