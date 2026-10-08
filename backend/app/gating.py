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

Phase D session 6 (EXTENSION_INTEGRATION.md SS6 /v1/usage, amended session 6):
- Questions asked on a BYO key (usage_events.tier = 'byo_key', migration 0006) never count
  toward the free or paid caps.
- The paid window counts from current_period_start with no upper bound. Rows can't exist in
  the future, so the old `< current_period_end` bound only mattered once the period had ended
  without Stripe's renewal webhook arriving -- and then it froze the count, leaving the
  account uncapped. Now the count keeps running against the cap until the renewal arrives.
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
# the cap that rejection just enforced (design doc SS2/SS7.2). Also excludes
# "mapping_proposal" (Phase D session 4): proposing a column mapping is setup, not a
# question, so it never counts toward the question caps -- only toward its own cap below.
# "in_progress" (Phase D session 5) counts too: a question that's still running is a real
# attempt, so a burst of concurrent or long-running questions can't slip past the cap.
_COUNTED_OUTCOMES = ("answered", "hit_iteration_cap", "error", "in_progress")

# usage_events.tier of a question asked on the caller's own Anthropic key (migration 0006).
# Those questions cost this service nothing, so they never count toward the free or paid caps
# -- including while still in_progress, since tier is set when the row is inserted. NULL-tier
# rows (before 0006) keep counting, as they always did.
BYO_KEY_TIER = "byo_key"

# Subscriptions in these states get the paid tier; every other state falls through to free.
_PAID_STATUSES = ("active", "trialing")

# Fresh model calls from POST /v1/csv/{id}/propose-mapping, per account, in a rolling 24h
# window, regardless of tier (a proposal always uses the master key). Counted from
# usage_events rather than csv_statements: the retention cron deletes expired unconfirmed
# csv_statements rows about an hour after parse, which would silently reset the count. A
# placeholder to tune from real usage, like FREE_DAILY_CAP.
MAPPING_PROPOSAL_DAILY_CAP = 30
MAPPING_PROPOSAL_WINDOW = timedelta(hours=24)
MAPPING_PROPOSAL_OUTCOME = "mapping_proposal"


@dataclass
class MappingProposalGateDecision:
    allowed: bool
    proposals_used: int
    resets_at: datetime | None  # when the oldest proposal in the window ages out; None if allowed


def evaluate_mapping_proposal_gate(session, account_id, now: datetime) -> MappingProposalGateDecision:
    """Whether this account may make another fresh mapping proposal now. Pure, like
    evaluate_ask_gate -- the route inserts the usage_events row itself when allowed."""
    used, oldest = session.execute(
        select(func.count(), func.min(UsageEvent.occurred_at)).where(
            UsageEvent.account_id == account_id,
            UsageEvent.occurred_at >= now - MAPPING_PROPOSAL_WINDOW,
            UsageEvent.outcome == MAPPING_PROPOSAL_OUTCOME,
        )
    ).one()
    if used < MAPPING_PROPOSAL_DAILY_CAP:
        return MappingProposalGateDecision(allowed=True, proposals_used=used, resets_at=None)
    return MappingProposalGateDecision(
        allowed=False, proposals_used=used, resets_at=oldest + MAPPING_PROPOSAL_WINDOW
    )


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


def _count_usage_events(session, account_id, since: datetime) -> int:
    """Counted question rows since `since`, excluding questions asked on a BYO key."""
    query = select(func.count()).select_from(UsageEvent).where(
        UsageEvent.account_id == account_id,
        UsageEvent.occurred_at >= since,
        UsageEvent.outcome.in_(_COUNTED_OUTCOMES),
        UsageEvent.tier.is_distinct_from(BYO_KEY_TIER),
    )
    return session.execute(query).scalar_one()


def _active_byo_key(session, account) -> ByoKey | None:
    if account.byo_key_id is None:
        return None
    byo_key = session.get(ByoKey, account.byo_key_id)
    # is_active isn't spelled out verbatim in SS7.2's "account.byo_key_id IS NOT NULL"
    # phrasing, but byo_keys.is_active exists precisely to let a revoked key stop granting
    # access without deleting the audit row -- checking it here closes that gap rather than
    # treating a revoked key as still-unlimited access.
    if byo_key is not None and byo_key.is_active:
        return byo_key
    return None


def _subscription(session, account_id) -> Subscription | None:
    return session.execute(
        select(Subscription).where(Subscription.account_id == account_id)
    ).scalar_one_or_none()


def evaluate_ask_gate(session, account, now: datetime) -> AskGateDecision:
    """
    now must be timezone-aware UTC (datetime.now(timezone.utc)) -- compared directly
    against usage_events.occurred_at/subscriptions.current_period_* (both tz-aware
    DateTime columns) and used to derive the free tier's UTC-calendar-day boundary.
    """
    if _active_byo_key(session, account) is not None:
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

    subscription = _subscription(session, account.id)

    if subscription is not None and subscription.status in _PAID_STATUSES:
        used = _count_usage_events(session, account.id, subscription.current_period_start)
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
    used = _count_usage_events(session, account.id, today_start)
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


@dataclass
class SubscriptionSummary:
    status: str  # Stripe's value verbatim
    cancel_at_period_end: bool
    current_period_end: datetime


@dataclass
class AccountBillingStatus:
    """What GET /v1/usage reports beyond the gate decision: whether a BYO key is active, and
    the subscription's own state whatever the tier -- so a past_due subscriber (tier free) or a
    subscriber with a BYO key (tier byo_key) still sees their subscription."""

    has_byo_key: bool
    subscription: SubscriptionSummary | None


def account_billing_status(session, account) -> AccountBillingStatus:
    subscription = _subscription(session, account.id)
    return AccountBillingStatus(
        has_byo_key=_active_byo_key(session, account) is not None,
        subscription=(
            SubscriptionSummary(
                status=subscription.status,
                cancel_at_period_end=subscription.cancel_at_period_end,
                current_period_end=subscription.current_period_end,
            )
            if subscription is not None
            else None
        ),
    )
