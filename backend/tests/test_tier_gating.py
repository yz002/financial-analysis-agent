"""
Tests for Phase B session 7a: the three-tier /v1/ask gating logic (design doc SS7.2) and
the updated GET /v1/usage shape. There is no prior gating implementation or test to mirror
-- before this session /v1/ask called run_agent unconditionally and /v1/usage was fully
stubbed (hardcoded/random values, no DB read) -- so these are the first tests of their kind.

Like test_csv_endpoints.py / test_conversation_history.py, the integration tests here hit
a real reachable Postgres via DATABASE_URL (backend/.env). No Stripe account or network
access is used or needed -- a subscription row is always seeded directly in the test DB
(app.gating never talks to Stripe; that's session 7b's job). No freezegun/time-mocking
dependency is added either: boundary tests seed usage_events.occurred_at /
subscriptions.current_period_* at controlled offsets from the real
datetime.now(timezone.utc) computed at test time, the same direct-DB-seeding idiom these
tests already use elsewhere, rather than freezing wall-clock time.

app_main.run_agent is monkeypatched for the one HTTP-level "allowed" test, the same idiom
test_conversation_history.py already uses -- never a real Anthropic API call. The
HTTP-level rejection test needs no such mock: the gate short-circuits before run_agent is
ever called.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_main
from app.gating import FREE_DAILY_CAP, PAID_MONTHLY_CAP, evaluate_ask_gate
from app.main import app
from db.base import get_session
from db.models import Account, ByoKey, Subscription, UsageEvent

client = TestClient(app)


def _account(session, account_id: str) -> Account:
    return session.get(Account, uuid.UUID(account_id))


def _seed_byo_key(session, account_id: str, is_active: bool) -> ByoKey:
    byo_key = ByoKey(
        account_id=uuid.UUID(account_id), encrypted_key=b"fake-key", is_active=is_active
    )
    session.add(byo_key)
    session.flush()
    account = _account(session, account_id)
    account.byo_key_id = byo_key.id
    session.flush()
    return byo_key


def _seed_subscription(
    session,
    account_id: str,
    status: str,
    current_period_start: datetime,
    current_period_end: datetime,
    cancel_at_period_end: bool = False,
) -> Subscription:
    subscription = Subscription(
        account_id=uuid.UUID(account_id),
        stripe_customer_id="cus_test",
        stripe_subscription_id=f"sub_test_{uuid.uuid4()}",
        status=status,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
        cancel_at_period_end=cancel_at_period_end,
    )
    session.add(subscription)
    session.flush()
    return subscription


def _seed_usage_events(
    session,
    account_id: str,
    count: int,
    occurred_at: datetime,
    outcome: str = "answered",
    tier: str | None = None,
) -> None:
    for _ in range(count):
        session.add(
            UsageEvent(
                account_id=uuid.UUID(account_id), occurred_at=occurred_at, outcome=outcome, tier=tier
            )
        )
    session.flush()


def _fake_result(question: str, hit_iteration_cap: bool = False) -> dict:
    return {
        "question": question,
        "final_answer": "Answer.",
        "hit_iteration_cap": hit_iteration_cap,
        "iterations_used": 1,
        "stop_reason": "end_turn",
        "figure_check": {},
        "tool_calls": [],
    }


# --- evaluate_ask_gate unit tests (no HTTP, no run_agent) ----------------------------------


def test_byo_key_bypasses_free_tier_cap(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        _seed_byo_key(session, account_id, is_active=True)
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, FREE_DAILY_CAP, now)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "byo_key"
        assert decision.allowed is True
        assert decision.cap is None
    finally:
        session.close()


def test_inactive_byo_key_falls_through_to_free_tier(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        _seed_byo_key(session, account_id, is_active=False)
        now = datetime.now(timezone.utc)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "free"
        assert decision.allowed is True
    finally:
        session.close()


def test_paid_tier_allows_up_to_cap_minus_one(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(session, account_id, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, account_id, PAID_MONTHLY_CAP - 1, now)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "paid"
        assert decision.allowed is True
        assert decision.questions_used == PAID_MONTHLY_CAP - 1
    finally:
        session.close()


def test_paid_tier_rejects_at_cap(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(session, account_id, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, account_id, PAID_MONTHLY_CAP, now)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "paid"
        assert decision.allowed is False
        assert decision.reject_outcome == "rejected_monthly_cap"
        assert decision.reject_error == "monthly_cap_reached"
        assert decision.prompt_byo_key is True
        assert decision.prompt_upgrade is False
    finally:
        session.close()


def test_past_due_subscription_falls_through_to_free_tier(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        # Way over the monthly cap -- shouldn't matter, since past_due never reaches the
        # monthly-cap check at all.
        _seed_subscription(session, account_id, "past_due", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, account_id, PAID_MONTHLY_CAP + 10, now)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        # Not hard-blocked by the subscription being past_due -- but still subject to the
        # free tier's own (much smaller) cap, which this much usage also exceeds.
        assert decision.tier == "free"
        assert decision.allowed is False
        assert decision.reject_outcome == "rejected_daily_cap"
    finally:
        session.close()


def test_past_due_subscription_allowed_under_free_daily_cap(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(session, account_id, "past_due", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, account_id, FREE_DAILY_CAP - 1, now)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "free"
        assert decision.allowed is True
    finally:
        session.close()


def test_free_tier_allows_up_to_cap_minus_one(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, FREE_DAILY_CAP - 1, now)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "free"
        assert decision.allowed is True
        assert decision.questions_used == FREE_DAILY_CAP - 1
    finally:
        session.close()


def test_free_tier_rejects_at_cap(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, FREE_DAILY_CAP, now)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "free"
        assert decision.allowed is False
        assert decision.reject_outcome == "rejected_daily_cap"
        assert decision.reject_error == "daily_cap_reached"
        assert decision.prompt_upgrade is True
        assert decision.prompt_byo_key is False
    finally:
        session.close()


def test_free_tier_yesterdays_usage_does_not_count(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, FREE_DAILY_CAP, now - timedelta(days=1))
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "free"
        assert decision.allowed is True
        assert decision.questions_used == 0
    finally:
        session.close()


# --- /v1/ask HTTP-level tests ---------------------------------------------------------------


def test_ask_rejects_at_free_daily_cap_without_calling_run_agent(auth_session, monkeypatch):
    account_id, headers = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, FREE_DAILY_CAP, now)
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None, prior_tool_calls=None, excluded_phrases=None: pytest.fail(
            "run_agent must not be called once the gate rejects"
        ),
    )

    resp = client.post("/v1/ask", json={"question": "Anything."}, headers=headers)
    assert resp.status_code == 429, resp.text
    body = resp.json()["detail"]
    assert body["error"] == "daily_cap_reached"
    assert body["prompt_upgrade"] is True
    assert body["prompt_byo_key"] is False
    assert body["resets_at"] is not None

    session = get_session()
    try:
        outcomes = session.execute(
            text("SELECT outcome FROM usage_events WHERE account_id = :id"), {"id": account_id}
        ).scalars().all()
        # FREE_DAILY_CAP seeded "answered" rows plus one new "rejected_daily_cap" row.
        assert outcomes.count("rejected_daily_cap") == 1
    finally:
        session.close()


def test_ask_allow_path_records_answered_usage_event(auth_session, monkeypatch):
    account_id, headers = auth_session()
    monkeypatch.setattr(
        app_main, "run_agent", lambda question, prior_messages=None, prior_tool_calls=None, excluded_phrases=None: _fake_result(question)
    )

    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        row = session.execute(
            text("SELECT outcome, turn_id FROM usage_events WHERE account_id = :id"),
            {"id": account_id},
        ).fetchone()
        assert row is not None
        assert row.outcome == "answered"
        assert row.turn_id is not None
    finally:
        session.close()


# --- GET /v1/usage tests ---------------------------------------------------------------------


def test_usage_endpoint_reports_free_tier(auth_session):
    account_id, headers = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, 2, now)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "free"
    assert body["questions_today"] == 2
    assert body["daily_cap"] == FREE_DAILY_CAP
    assert body["questions_this_period"] is None
    assert body["monthly_cap"] is None
    assert body["period_ends_at"] is None
    assert body["byo_key_required"] is False


def test_usage_endpoint_reports_paid_tier(auth_session):
    account_id, headers = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(session, account_id, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, account_id, 3, now)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "paid"
    assert body["questions_this_period"] == 3
    assert body["monthly_cap"] == PAID_MONTHLY_CAP
    assert body["period_ends_at"] is not None
    assert body["questions_today"] is None
    assert body["daily_cap"] is None
    assert body["byo_key_required"] is False


def test_usage_endpoint_reports_byo_key_tier(auth_session):
    account_id, headers = auth_session()
    session = get_session()
    try:
        _seed_byo_key(session, account_id, is_active=True)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "byo_key"
    assert body["questions_today"] is None
    assert body["daily_cap"] is None
    assert body["questions_this_period"] is None
    assert body["monthly_cap"] is None
    assert body["byo_key_required"] is False


def test_usage_endpoint_paid_tier_at_cap_prompts_byo_key(auth_session):
    account_id, headers = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(session, account_id, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, account_id, PAID_MONTHLY_CAP, now)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "paid"
    assert body["byo_key_required"] is True


# --- Phase D session 6: tier exclusion, the open-ended paid window, new /v1/usage fields -------


def test_paid_usage_after_period_end_still_counts(auth_session):
    """A late renewal webhook: the period has ended but the row still says active. The count
    keeps running from current_period_start, so the cap still applies."""
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(session, account_id, "active", now - timedelta(days=31), now - timedelta(days=1))
        _seed_usage_events(session, account_id, PAID_MONTHLY_CAP, now - timedelta(hours=1))
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "paid"
        assert decision.questions_used == PAID_MONTHLY_CAP
        assert decision.allowed is False
    finally:
        session.close()


def test_byo_key_questions_dont_count_toward_the_free_cap_after_the_key_is_removed(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        _seed_byo_key(session, account_id, is_active=False)  # registered earlier, now removed
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, FREE_DAILY_CAP, now, tier="byo_key")
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "free"
        assert decision.questions_used == 0
        assert decision.allowed is True
    finally:
        session.close()


def test_an_in_progress_byo_question_is_excluded_while_still_running(auth_session):
    """The Postgres twin of tests_unit's compiled-SQL check: an (in_progress, byo_key) row
    doesn't count; an (in_progress, free) row does."""
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, 1, now, outcome="in_progress", tier="byo_key")
        _seed_usage_events(session, account_id, 1, now, outcome="in_progress", tier="free")
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.tier == "free"
        assert decision.questions_used == 1
    finally:
        session.close()


def test_legacy_null_tier_rows_still_count(auth_session):
    account_id, _ = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, account_id, FREE_DAILY_CAP, now, tier=None)
        session.commit()

        decision = evaluate_ask_gate(session, _account(session, account_id), now)
        assert decision.questions_used == FREE_DAILY_CAP
        assert decision.allowed is False
    finally:
        session.close()


def test_usage_reports_a_past_due_subscription_under_the_free_tier(auth_session):
    account_id, headers = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(session, account_id, "past_due", now - timedelta(days=1), now + timedelta(days=29))
        session.commit()
    finally:
        session.close()

    body = client.get("/v1/usage", headers=headers).json()
    assert body["tier"] == "free"
    assert body["subscription"]["status"] == "past_due"
    assert body["subscription"]["cancel_at_period_end"] is False
    assert body["subscription"]["current_period_end"] is not None


def test_usage_reports_a_scheduled_cancellation(auth_session):
    account_id, headers = auth_session()
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        _seed_subscription(
            session, account_id, "active", now - timedelta(days=1), now + timedelta(days=29),
            cancel_at_period_end=True,
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/v1/usage", headers=headers).json()
    assert body["tier"] == "paid"
    assert body["subscription"] == {
        "status": "active",
        "cancel_at_period_end": True,
        "current_period_end": body["period_ends_at"],
    }
    assert body["resets_at"] == body["period_ends_at"]


def test_usage_reports_resets_at_and_has_byo_key(auth_session):
    account_id, headers = auth_session()
    body = client.get("/v1/usage", headers=headers).json()
    assert body["tier"] == "free"
    assert body["has_byo_key"] is False
    assert body["subscription"] is None
    resets_at = datetime.fromisoformat(body["resets_at"])
    now = datetime.now(timezone.utc)
    assert resets_at == now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    session = get_session()
    try:
        _seed_byo_key(session, account_id, is_active=True)
        session.commit()
    finally:
        session.close()

    body = client.get("/v1/usage", headers=headers).json()
    assert body["tier"] == "byo_key"
    assert body["has_byo_key"] is True
    assert body["resets_at"] is None


def test_ask_writes_the_tier_on_the_usage_row(auth_session, monkeypatch):
    account_id, headers = auth_session()
    monkeypatch.setattr(
        app_main, "run_agent", lambda question, prior_messages=None, prior_tool_calls=None, excluded_phrases=None: _fake_result(question)
    )
    resp = client.post("/v1/ask", json={"question": "What was MSFT revenue?"}, headers=headers)
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        row = session.execute(
            text("SELECT outcome, tier FROM usage_events WHERE account_id = :id"), {"id": account_id}
        ).fetchone()
        assert row.outcome == "answered"
        assert row.tier == "free"  # written at insert, unchanged by the completion
    finally:
        session.close()


# --- dropped-column confirmation --------------------------------------------------------------
#
# The Phase B session 7a test that used to live here (test_free_window_started_at_column_is_dropped)
# checked information_schema for a dropped column on the `installs` table. Phase C session 1
# dropped the `installs` table itself, so that check no longer asserts anything meaningful (a
# dropped column on a dropped table is vacuously true) -- removed rather than kept as dead
# weight. No replacement needed: there's no analogous column-drop on `accounts` to confirm.
