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
from db.models import ByoKey, Install, Subscription, UsageEvent

client = TestClient(app)


@pytest.fixture
def install_ids():
    """Tracks install_ids created by a test; deletes them (cascading to byo_keys/
    subscriptions/usage_events) after."""
    ids: list[str] = []
    yield ids
    if not ids:
        return
    session = get_session()
    try:
        session.execute(
            text("DELETE FROM installs WHERE install_id = ANY(:ids)"),
            {"ids": ids},
        )
        session.commit()
    finally:
        session.close()


def _new_install_id(install_ids: list[str]) -> str:
    install_id = str(uuid.uuid4())
    install_ids.append(install_id)
    return install_id


def _seed_install(session, install_id: str) -> Install:
    install = Install(
        install_id=uuid.UUID(install_id),
        identity_type="uuid",
        identity_value=install_id,
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add(install)
    session.flush()
    return install


def _seed_byo_key(session, install: Install, is_active: bool) -> ByoKey:
    byo_key = ByoKey(install_id=install.install_id, encrypted_key=b"fake-key", is_active=is_active)
    session.add(byo_key)
    session.flush()
    install.byo_key_id = byo_key.id
    session.flush()
    return byo_key


def _seed_subscription(
    session, install: Install, status: str, current_period_start: datetime, current_period_end: datetime
) -> Subscription:
    subscription = Subscription(
        install_id=install.install_id,
        stripe_customer_id="cus_test",
        stripe_subscription_id=f"sub_test_{uuid.uuid4()}",
        status=status,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
    )
    session.add(subscription)
    session.flush()
    return subscription


def _seed_usage_events(session, install_id: uuid.UUID, count: int, occurred_at: datetime) -> None:
    for _ in range(count):
        session.add(UsageEvent(install_id=install_id, occurred_at=occurred_at, outcome="answered"))
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


def test_byo_key_bypasses_free_tier_cap(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        _seed_byo_key(session, install, is_active=True)
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, install.install_id, FREE_DAILY_CAP, now)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "byo_key"
        assert decision.allowed is True
        assert decision.cap is None
    finally:
        session.close()


def test_inactive_byo_key_falls_through_to_free_tier(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        _seed_byo_key(session, install, is_active=False)
        now = datetime.now(timezone.utc)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "free"
        assert decision.allowed is True
    finally:
        session.close()


def test_paid_tier_allows_up_to_cap_minus_one(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_subscription(session, install, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, install.install_id, PAID_MONTHLY_CAP - 1, now)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "paid"
        assert decision.allowed is True
        assert decision.questions_used == PAID_MONTHLY_CAP - 1
    finally:
        session.close()


def test_paid_tier_rejects_at_cap(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_subscription(session, install, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, install.install_id, PAID_MONTHLY_CAP, now)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "paid"
        assert decision.allowed is False
        assert decision.reject_outcome == "rejected_monthly_cap"
        assert decision.reject_error == "monthly_cap_reached"
        assert decision.prompt_byo_key is True
        assert decision.prompt_upgrade is False
    finally:
        session.close()


def test_past_due_subscription_falls_through_to_free_tier(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        # Way over the monthly cap -- shouldn't matter, since past_due never reaches the
        # monthly-cap check at all.
        _seed_subscription(session, install, "past_due", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, install.install_id, PAID_MONTHLY_CAP + 10, now)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        # Not hard-blocked by the subscription being past_due -- but still subject to the
        # free tier's own (much smaller) cap, which this much usage also exceeds.
        assert decision.tier == "free"
        assert decision.allowed is False
        assert decision.reject_outcome == "rejected_daily_cap"
    finally:
        session.close()


def test_past_due_subscription_allowed_under_free_daily_cap(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_subscription(session, install, "past_due", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, install.install_id, FREE_DAILY_CAP - 1, now)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "free"
        assert decision.allowed is True
    finally:
        session.close()


def test_free_tier_allows_up_to_cap_minus_one(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, install.install_id, FREE_DAILY_CAP - 1, now)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "free"
        assert decision.allowed is True
        assert decision.questions_used == FREE_DAILY_CAP - 1
    finally:
        session.close()


def test_free_tier_rejects_at_cap(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, install.install_id, FREE_DAILY_CAP, now)
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "free"
        assert decision.allowed is False
        assert decision.reject_outcome == "rejected_daily_cap"
        assert decision.reject_error == "daily_cap_reached"
        assert decision.prompt_upgrade is True
        assert decision.prompt_byo_key is False
    finally:
        session.close()


def test_free_tier_yesterdays_usage_does_not_count(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, install.install_id, FREE_DAILY_CAP, now - timedelta(days=1))
        session.commit()

        decision = evaluate_ask_gate(session, install, now)
        assert decision.tier == "free"
        assert decision.allowed is True
        assert decision.questions_used == 0
    finally:
        session.close()


# --- /v1/ask HTTP-level tests ---------------------------------------------------------------


def test_ask_rejects_at_free_daily_cap_without_calling_run_agent(install_ids, monkeypatch):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, install.install_id, FREE_DAILY_CAP, now)
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(
        app_main,
        "run_agent",
        lambda question, prior_messages=None: pytest.fail(
            "run_agent must not be called once the gate rejects"
        ),
    )

    resp = client.post("/v1/ask", json={"question": "Anything."}, headers={"X-Install-Id": install_id})
    assert resp.status_code == 429, resp.text
    body = resp.json()["detail"]
    assert body["error"] == "daily_cap_reached"
    assert body["prompt_upgrade"] is True
    assert body["prompt_byo_key"] is False
    assert body["resets_at"] is not None

    session = get_session()
    try:
        outcomes = session.execute(
            text("SELECT outcome FROM usage_events WHERE install_id = :id"), {"id": install_id}
        ).scalars().all()
        # FREE_DAILY_CAP seeded "answered" rows plus one new "rejected_daily_cap" row.
        assert outcomes.count("rejected_daily_cap") == 1
    finally:
        session.close()


def test_ask_allow_path_records_answered_usage_event(install_ids, monkeypatch):
    install_id = _new_install_id(install_ids)
    monkeypatch.setattr(
        app_main, "run_agent", lambda question, prior_messages=None: _fake_result(question)
    )

    resp = client.post(
        "/v1/ask", json={"question": "What was MSFT revenue?"}, headers={"X-Install-Id": install_id}
    )
    assert resp.status_code == 200, resp.text

    session = get_session()
    try:
        row = session.execute(
            text("SELECT outcome, turn_id FROM usage_events WHERE install_id = :id"),
            {"id": install_id},
        ).fetchone()
        assert row is not None
        assert row.outcome == "answered"
        assert row.turn_id is not None
    finally:
        session.close()


# --- GET /v1/usage tests ---------------------------------------------------------------------


def test_usage_endpoint_reports_free_tier(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_usage_events(session, install.install_id, 2, now)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers={"X-Install-Id": install_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "free"
    assert body["questions_today"] == 2
    assert body["daily_cap"] == FREE_DAILY_CAP
    assert body["questions_this_period"] is None
    assert body["monthly_cap"] is None
    assert body["period_ends_at"] is None
    assert body["byo_key_required"] is False


def test_usage_endpoint_reports_paid_tier(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_subscription(session, install, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, install.install_id, 3, now)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers={"X-Install-Id": install_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "paid"
    assert body["questions_this_period"] == 3
    assert body["monthly_cap"] == PAID_MONTHLY_CAP
    assert body["period_ends_at"] is not None
    assert body["questions_today"] is None
    assert body["daily_cap"] is None
    assert body["byo_key_required"] is False


def test_usage_endpoint_reports_byo_key_tier(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        _seed_byo_key(session, install, is_active=True)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers={"X-Install-Id": install_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "byo_key"
    assert body["questions_today"] is None
    assert body["daily_cap"] is None
    assert body["questions_this_period"] is None
    assert body["monthly_cap"] is None
    assert body["byo_key_required"] is False


def test_usage_endpoint_paid_tier_at_cap_prompts_byo_key(install_ids):
    install_id = _new_install_id(install_ids)
    session = get_session()
    try:
        install = _seed_install(session, install_id)
        now = datetime.now(timezone.utc)
        _seed_subscription(session, install, "active", now - timedelta(days=1), now + timedelta(days=29))
        _seed_usage_events(session, install.install_id, PAID_MONTHLY_CAP, now)
        session.commit()
    finally:
        session.close()

    resp = client.get("/v1/usage", headers={"X-Install-Id": install_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "paid"
    assert body["byo_key_required"] is True


# --- dropped-column confirmation --------------------------------------------------------------


def test_free_window_started_at_column_is_dropped():
    session = get_session()
    try:
        exists = session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'installs' AND column_name = 'free_window_started_at'"
            )
        ).first()
        assert exists is None
    finally:
        session.close()
