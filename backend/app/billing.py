"""
Stripe integration (Phase B session 7b, design doc SS7.1/SS7.4): Checkout Session creation
and webhook event handling. Split from route glue in main.py the same way gating.py is --
pure logic taking an already-open session, no HTTPException raised from in here (main.py's
/v1/billing/webhook route does that translation for signature/config errors).

Stripe's "Basil" API version (2025-03-31) removed current_period_start/current_period_end
from the top-level Subscription object -- they now live on each SubscriptionItem instead
(subscription["items"]["data"][0]["current_period_start"]). Confirmed via web search this
session (docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end),
not assumed -- a fresh sandbox account defaults to Basil or later, so the old top-level
fields would silently read back empty rather than error. _extract_current_period below reads
the items-based path accordingly; this codebase only ever creates single-item subscriptions
(one $2/mo price, quantity 1), so items.data[0] is always the right (and only) item.

STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET/STRIPE_PRICE_ID are read fresh from os.environ inside
the functions that need them, never cached at module import -- matching db/base.py's
get_database_url() convention (fail loud at call time, not import time; also lets tests
monkeypatch.setenv cleanly).
"""

import os
import uuid
from datetime import datetime, timezone

import stripe
from sqlalchemy import select

from db.models import Subscription


def _get(obj, key: str, default=None):
    """
    obj["key"] works uniformly across a real stripe.StripeObject (parsed by
    construct_event) and a plain dict (used by this module's own tests) -- but
    StripeObject deliberately does NOT support .get() ("StripeObject is not a dict",
    confirmed empirically against the installed SDK this session), so an optional field
    read needs this instead of the dict.get(key, default) idiom used everywhere else in
    this codebase.
    """
    return obj[key] if key in obj else default


def _ensure_stripe_configured() -> None:
    api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not set. Copy backend/.env.example to backend/.env and "
            "fill it in with a Stripe test-mode secret key."
        )
    stripe.api_key = api_key


def verify_webhook_event(payload: bytes, sig_header: str, webhook_secret: str) -> stripe.Event:
    """
    Raises ValueError (malformed payload) or stripe.SignatureVerificationError (bad/missing
    signature, or timestamp outside Stripe's replay-protection tolerance) -- the caller
    (main.py's /v1/billing/webhook route) catches both and rejects with 400 before any DB
    access happens.
    """
    return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)


def create_checkout_session(
    account_id: uuid.UUID,
    primary_email: str | None,
    price_id: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """
    Takes plain values, not a db.models.Account row -- the caller (main.py) reads these off
    the Account (via get_current_account) before making this outbound Stripe call, so a slow
    network call never holds a DB connection open. Returns the Checkout Session's hosted URL.
    primary_email is set unconditionally now (not gated on an identity_type check) -- every
    account is OAuth-verified by construction (design doc SS4), so there's no longer an
    unverified-identity case where it would be wrong to prefill Stripe's customer_email.
    """
    _ensure_stripe_configured()
    kwargs = {
        "mode": "subscription",
        "client_reference_id": str(account_id),
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    if primary_email:
        kwargs["customer_email"] = primary_email
    checkout_session = stripe.checkout.Session.create(**kwargs)
    return checkout_session.url


def _extract_current_period(subscription) -> tuple[datetime, datetime]:
    item = subscription["items"]["data"][0]
    start = datetime.fromtimestamp(item["current_period_start"], tz=timezone.utc)
    end = datetime.fromtimestamp(item["current_period_end"], tz=timezone.utc)
    return start, end


def _handle_checkout_session_completed(session, event) -> None:
    checkout_session = event["data"]["object"]
    account_id_raw = _get(checkout_session, "client_reference_id")
    if not account_id_raw:
        return  # not a checkout session we created (shouldn't happen) -- nothing to do
    account_id = uuid.UUID(account_id_raw)

    _ensure_stripe_configured()
    subscription = stripe.Subscription.retrieve(checkout_session["subscription"])
    period_start, period_end = _extract_current_period(subscription)
    now = datetime.now(timezone.utc)

    # Upsert keyed by account_id, not a blind insert: subscriptions.account_id is UNIQUE
    # (design doc SS7.3), and a user who canceled and is now re-subscribing gets a brand new
    # stripe_subscription_id -- a blind insert would violate that constraint.
    row = session.execute(
        select(Subscription).where(Subscription.account_id == account_id)
    ).scalar_one_or_none()
    if row is None:
        session.add(
            Subscription(
                account_id=account_id,
                stripe_customer_id=checkout_session["customer"],
                stripe_subscription_id=subscription["id"],
                status=subscription["status"],
                current_period_start=period_start,
                current_period_end=period_end,
            )
        )
    else:
        row.stripe_customer_id = checkout_session["customer"]
        row.stripe_subscription_id = subscription["id"]
        row.status = subscription["status"]
        row.current_period_start = period_start
        row.current_period_end = period_end
        row.cancel_at_period_end = _get(subscription, "cancel_at_period_end", False)
        row.updated_at = now


def _handle_subscription_updated(session, event) -> None:
    subscription = event["data"]["object"]
    row = session.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == subscription["id"])
    ).scalar_one_or_none()
    if row is None:
        # No local row yet -- e.g. theoretically out-of-order delivery ahead of
        # checkout.session.completed. Nothing to update; a later event reconciles this.
        return
    period_start, period_end = _extract_current_period(subscription)
    row.status = subscription["status"]
    row.current_period_start = period_start
    row.current_period_end = period_end
    row.cancel_at_period_end = _get(subscription, "cancel_at_period_end", False)
    row.updated_at = datetime.now(timezone.utc)


def _handle_subscription_deleted(session, event) -> None:
    subscription = event["data"]["object"]
    row = session.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == subscription["id"])
    ).scalar_one_or_none()
    if row is None:
        return
    # Soft update -- never a row delete, matching byo_keys.is_active's audit-trail
    # convention (design doc SS7.1).
    row.status = "canceled"
    row.updated_at = datetime.now(timezone.utc)


def _handle_invoice_payment_failed(session, event) -> None:
    # No-op: the stripe_webhook_events marker row (inserted by the route before any handler
    # runs) already satisfies "persisted for visibility" -- status transitions are
    # customer.subscription.updated's job, not this event's. No dunning/notification logic
    # this session (design doc SS7.1).
    return


_EVENT_HANDLERS = {
    "checkout.session.completed": _handle_checkout_session_completed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_failed": _handle_invoice_payment_failed,
}


def handle_stripe_event(session, event) -> None:
    """
    Dispatches to the handler for event["type"]; no-ops for any other event type (Stripe
    sends many others, and `stripe listen` forwards everything unless filtered) -- the
    caller has already inserted this event's stripe_webhook_events marker row regardless, so
    an unhandled type is still recorded as seen and won't be reprocessed on retry.
    """
    handler = _EVENT_HANDLERS.get(event["type"])
    if handler is not None:
        handler(session, event)
