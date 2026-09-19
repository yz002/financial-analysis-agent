"""
Three-tier /v1/ask gating logic (design doc SS7.2): BYO-key (unlimited) > active paid
subscription (capped per Stripe billing period) > recurring free daily cap. Past_due/
canceled/unpaid subscriptions fall through to the free tier rather than being hard-blocked
-- SS7.2's stated policy, consistent with this project's existing "never punitively cut off
access as a side effect of a billing state" ethos.

evaluate_ask_gate is a pure function -- no DB writes, no other side effects -- so both
POST /v1/ask (which acts on the decision: inserting a usage_events row, raising on
rejection) and GET /v1/usage (which only reports current status) share one source of
truth instead of two copies of this logic.

FREE_DAILY_CAP and PAID_MONTHLY_CAP are both placeholders to tune from real usage data
once live, not guessed definitively (design doc SS7.2). PAID_MONTHLY_CAP starts at 50,
not the design doc's original 300 placeholder: at $2/mo revenue, 300 implies a
per-question cost budget of under a cent, which a multi-round Claude tool-calling loop
(src/agent/agent.py's DEFAULT_MAX_ITERATIONS=8, with cumulative tool-result context
resent every round) plausibly exceeds several times over -- confirmed with the user this
session rather than carried over from the design doc unexamined.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select

from db.models import ByoKey, Subscription, UsageEvent

FREE_DAILY_CAP = 5
PAID_MONTHLY_CAP = 50

# usage_events outcomes that represent a real attempt and so count toward a cap --
# excludes the rejected_* outcomes, which record a rejection but shouldn't count against
# the cap that rejection just enforced (design doc SS2/SS7.2).
_COUNTED_OUTCOMES = ("answered", "hit_iteration_cap", "error")


@dataclass
class AskGateDecision:
    tier: Literal["byo_key", "paid", "free"]
    allowed: bool
    questions_used: int
    cap: int | None  # None for byo_key -- no cap
    resets_at: datetime | None  # None for byo_key
    reject_outcome: Literal["rejected_daily_cap", "rejected_monthly_cap"] | None
    reject_error: Literal["daily_cap_reached", "monthly_cap_reached"] | None
    prompt_byo_key: bool
    prompt_upgrade: bool


def _count_usage_events(session, account_id, start: datetime, end: datetime | None) -> int:
    query = select(func.count()).select_from(UsageEvent).where(
        UsageEvent.account_id == account_id,
        UsageEvent.occurred_at >= start,
        UsageEvent.outcome.in_(_COUNTED_OUTCOMES),
    )
    if end is not None:
        query = query.where(UsageEvent.occurred_at < end)
    return session.execute(query).scalar_one()


def evaluate_ask_gate(session, account, now: datetime) -> AskGateDecision:
    """
    now must be timezone-aware UTC (datetime.now(timezone.utc)) -- compared directly
    against usage_events.occurred_at/subscriptions.current_period_* (both tz-aware
    DateTime columns) and used to derive the free tier's UTC-calendar-day boundary.
    """
    if account.byo_key_id is not None:
        byo_key = session.get(ByoKey, account.byo_key_id)
        # is_active isn't spelled out verbatim in SS7.2's "account.byo_key_id IS NOT
        # NULL" phrasing, but byo_keys.is_active exists precisely to let a revoked key
        # stop granting access without deleting the audit row -- checking it here closes
        # that gap rather than treating a revoked key as still-unlimited access.
        if byo_key is not None and byo_key.is_active:
            return AskGateDecision(
                tier="byo_key",
                allowed=True,
                questions_used=0,
                cap=None,
                resets_at=None,
                reject_outcome=None,
                reject_error=None,
                prompt_byo_key=False,
                prompt_upgrade=False,
            )

    subscription = session.execute(
        select(Subscription).where(Subscription.account_id == account.id)
    ).scalar_one_or_none()

    if subscription is not None and subscription.status in ("active", "trialing"):
        used = _count_usage_events(
            session,
            account.id,
            subscription.current_period_start,
            subscription.current_period_end,
        )
        allowed = used < PAID_MONTHLY_CAP
        return AskGateDecision(
            tier="paid",
            allowed=allowed,
            questions_used=used,
            cap=PAID_MONTHLY_CAP,
            resets_at=subscription.current_period_end,
            reject_outcome=None if allowed else "rejected_monthly_cap",
            reject_error=None if allowed else "monthly_cap_reached",
            prompt_byo_key=not allowed,
            prompt_upgrade=False,
        )

    # Free tier -- also reached by past_due/canceled/unpaid subscriptions (or no
    # subscription row at all), which fall through here rather than being hard-blocked.
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = today_start + timedelta(days=1)
    used = _count_usage_events(session, account.id, today_start, None)
    allowed = used < FREE_DAILY_CAP
    return AskGateDecision(
        tier="free",
        allowed=allowed,
        questions_used=used,
        cap=FREE_DAILY_CAP,
        resets_at=next_midnight,
        reject_outcome=None if allowed else "rejected_daily_cap",
        reject_error=None if allowed else "daily_cap_reached",
        prompt_byo_key=False,
        prompt_upgrade=not allowed,
    )
