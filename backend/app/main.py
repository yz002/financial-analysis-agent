"""
FastAPI skeleton for the Sheets Add-on backend (Phase B session 2 shape;
session 3 wires /v1/ask to run_agent for real). Every route other than
/v1/health and /v1/ask still returns stubbed data -- no CSV pipeline, no
identity/rate-limiting, no DB reads/writes beyond health/ask. Later sessions
(design doc SS6 steps 4-8) fill in real logic behind this same contract.

Run locally from inside backend/ (matching db/smoke_test.py's cwd convention):

    backend/.venv/Scripts/uvicorn.exe app.main:app --reload

with DATABASE_URL, ANTHROPIC_API_KEY, and SEC_USER_AGENT set (backend/.env)
for /v1/health and /v1/ask to reach Postgres/Anthropic/EDGAR respectively.
"""

import hashlib
import json
import logging
import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import anthropic
import stripe
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError

from db.base import get_session
from db.models import (
    Account,
    ByoKey,
    Conversation,
    CsvStatement,
    LinkedIdentity,
    Session as SessionModel,
    StripeWebhookEvent,
    Subscription,
    Turn,
    UsageEvent,
)

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_stderr_logger(name: str, level: int) -> logging.Logger:
    """
    Gives a logger its own stderr handler, which Render's service logs capture. Nothing else
    configures logging for this app: uvicorn's default config covers only its own uvicorn.*
    loggers, so without this an app warning reaches stderr only through Python's last-resort
    handler -- a bare, unlabeled line, which stops entirely the moment anything configures the
    root logger (Phase D session 6 live test: the billing guard's WARNING never showed).
    propagate=False keeps lines from appearing twice if uvicorn or Render configures the root
    logger; the handler check keeps a --reload re-import from stacking handlers.
    """
    configured = logging.getLogger(name)
    if not configured.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        configured.addHandler(handler)
    configured.setLevel(level)
    configured.propagate = False
    return configured


# Every app.* logger (app.main, app.billing, app.gating, ...): WARNING and up. Every app warning
# is metadata only -- ids, class names, outcome labels, never a key, email, question or answer
# (backend/SECURITY.md SS4/SS8).
APP_LOGGER_NAME = "app"
configure_stderr_logger(APP_LOGGER_NAME, logging.WARNING)

# src/agent/agent.py's per-model-call INFO line (iteration, duration_ms, stop reason, model --
# metadata only, never question/answer text: backend/SECURITY.md SS4).
AGENT_LOGGER_NAME = "src.agent.agent"
configure_stderr_logger(AGENT_LOGGER_NAME, logging.INFO)

# src/agent/agent.py lives one level above backend/ (see repo layout in
# CLAUDE.md), but this module is normally run with backend/ as the working
# directory (see the run instructions above), so `src` isn't importable
# without adding the repo root to sys.path -- mirrors the same bootstrap
# src/app/main.py already uses for the Streamlit app.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import csv_session  # noqa: E402 -- see sys.path note above
from src.agent.agent import (  # noqa: E402 -- see sys.path note above
    DEFAULT_MODEL,
    MODEL_CALL_TIMEOUT_SECONDS,
    AgentTimeBudgetExceeded,
    run_agent,
)
from src.agent.citations import build_citations  # noqa: E402 -- see sys.path note above
from src.analysis.csv_statement import (  # noqa: E402 -- see sys.path note above
    MAPPABLE_ROLES,
    RECOMMENDED_CONCEPTS,
    find_unparsed_cells,
    normalize,
    statement_from_records,
    validate_mapping,
)
from src.data.csv_ingest import MAX_SAMPLE_ROWS  # noqa: E402 -- see sys.path note above
from src.data.csv_ingest import propose_mapping as generate_mapping_proposal  # noqa: E402
from src.data.sheet_ingest import (  # noqa: E402 -- see sys.path note above
    find_period_serial_number_value,
    raw_csv_from_json,
    raw_csv_to_json,
    rows_to_raw_csv,
)

from . import billing, oauth_providers
from .ask_rules import (
    DONE,
    FAILED,
    IN_PROGRESS,
    LOST,
    MISMATCH,
    NEEDS_RECONFIRM,
    REUSED,
    RUNNING,
    ask_error,
    binding_decision,
    replay_state,
    request_fingerprint,
    statement_problem,
    turns_returned_statement_data,
    CSV_TOOLS,
)
from .crypto import (
    ByoUnavailable,
    decrypt_byo_key,
    encrypt_byo_key,
    ensure_byo_encryption_configured,
    is_valid_byo_key_format,
)
from .gating import (
    MAPPING_PROPOSAL_OUTCOME,
    account_billing_status,
    evaluate_ask_gate,
    evaluate_mapping_proposal_gate,
)
from .history import MAX_PRIOR_TURNS, build_prior_messages
from .schemas import (
    AskRequest,
    AskResponse,
    AuthExchangeRequest,
    AuthExchangeResponse,
    ByoKeyRemoveResponse,
    ByoKeyRequest,
    ByoKeyResponse,
    CheckoutSessionResponse,
    ConfirmRequest,
    ConfirmResponse,
    CsvParseRequest,
    CsvParseResponse,
    GoogleDataTokenRequest,
    GoogleDataTokenResponse,
    HealthResponse,
    LogoutResponse,
    MappingProposalEntry,
    PortalSessionResponse,
    ProposeMappingResponse,
    RevokeAllSessionsResponse,
    UsageResponse,
    UsageSubscription,
)

app = FastAPI(title="Sheets Add-on Backend")

# /v1/csv/parse's request-body cap (Phase D session 3b contract amendment). Sits above
# src/data/sheet_ingest.py's cell-count caps: 2 MB is ~50k cells at ~40 bytes each, so the two
# limits agree, but this one is checked before any JSON is parsed.
MAX_CSV_PARSE_BODY_BYTES = 2 * 1024 * 1024


@app.get("/v1/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    try:
        session = get_session()
        try:
            session.execute(text("SELECT 1"))
            db_status = "ok"
        finally:
            session.close()
    except Exception:
        db_status = "error"
        response.status_code = 503
    return HealthResponse(status="ok", db=db_status, commit=os.environ.get("RENDER_GIT_COMMIT"))


def get_current_account(
    request: Request, authorization: str | None = Header(default=None, alias="Authorization")
) -> Account:
    """
    Design doc SS2: parses "Bearer <token>" from Authorization, hashes it (SHA-256, matching
    /v1/auth/exchange's own hashing), and looks up a live (not revoked, not expired) sessions
    row. Every miss -- unknown, expired, or revoked -- 401s with the identical detail string,
    matching this project's anti-enumeration convention for csv_context_id/conversation_id
    lookups. On a hit, extends expires_at to now + 90 days again (sliding window, not an
    absolute cap) and returns the associated Account, replacing _get_or_create_install's
    auto-create-any-UUID shim outright -- an unrecognized token is now refused, not
    autovivified.

    Also stashes the resolved session row's id on request.state.session_id (design doc SS2's
    "Revocation" subsection) so /v1/auth/logout can revoke the exact row this request's token
    validated against without re-deriving the token hash or re-querying sessions a second time.
    Only the plain UUID is stashed, never the ORM row itself -- session_row would be detached/
    expired the moment this function's own short-lived session closes below.

    The header is optional at the FastAPI level on purpose: a required Header() makes a
    request with no Authorization header at all fail FastAPI's own validation with a 422,
    breaking EXTENSION_INTEGRATION.md SS3's rule that a missing header 401s identically to
    every other credential failure.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid or expired session token.")
    raw_token = authorization.removeprefix("Bearer ")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        session_row = session.execute(
            select(SessionModel).where(
                SessionModel.token_hash == token_hash,
                SessionModel.revoked_at.is_(None),
                SessionModel.expires_at > now,
            )
        ).scalar_one_or_none()
        if session_row is None:
            raise HTTPException(status_code=401, detail="Invalid or expired session token.")
        session_id = session_row.id
        session_row.last_used_at = now
        session_row.expires_at = now + timedelta(days=90)
        account = session.get(Account, session_row.account_id)
        session.commit()
        session.refresh(account)  # reload attributes expired by the commit above
        session.expunge(account)  # detach so callers can read it after session.close()
        request.state.session_id = session_id
        return account
    finally:
        session.close()


def _resolve_account_for_identity(session, identity: oauth_providers.ProviderIdentity, now: datetime) -> Account:
    """
    Design doc SS3's resolution order (amended 2026-09-29): (a) an exact
    (provider, provider_subject) match resolves to its existing account; otherwise (c) a new
    account is created. Email never selects an account -- the former step (b), which
    attached a new identity to whichever account already had a matching provider_email,
    was an nOAuth-class takeover: Microsoft's `mail`/`userPrincipalName` are tenant-
    controlled and unverified, so anyone could create a tenant user carrying a victim's
    address. See SECURITY.md SS7. Email is still stored, for display and billing only.

    Returns the Account to issue a session against, with last_seen_at already set to `now`
    on both branches. Only adds/mutates ORM objects -- the caller owns the transaction
    (commit/flush).
    """
    linked = session.execute(
        select(LinkedIdentity).where(
            LinkedIdentity.provider == identity.provider,
            LinkedIdentity.provider_subject == identity.subject,
        )
    ).scalar_one_or_none()
    if linked is not None:
        account = session.get(Account, linked.account_id)
        account.last_seen_at = now
        return account

    # Explicit id (not a flush-to-learn-the-default) so account.id is a concrete value
    # immediately, before LinkedIdentity's FK needs it. Don't "simplify" this back to
    # relying on Account.id's column default -- LinkedIdentity is added in the same call
    # with no intervening flush.
    account = Account(id=uuid.uuid4(), primary_email=identity.email, last_seen_at=now)
    session.add(account)
    session.add(LinkedIdentity(
        account_id=account.id,
        provider=identity.provider,
        provider_subject=identity.subject,
        provider_email=identity.email,
    ))
    return account


def _get_owned_csv_statement(session, csv_context_id: uuid.UUID, account_id: uuid.UUID) -> CsvStatement:
    """
    Load a csv_statements row scoped to the authenticated caller's account_id -- the design
    doc's stated highest-value security control. A row that doesn't exist and a row that
    exists but belongs to a different account are indistinguishable to the caller (both 404),
    so a non-owner can't even confirm a csv_context_id exists.
    """
    row = session.get(CsvStatement, csv_context_id)
    if row is None or row.account_id != account_id:
        raise HTTPException(status_code=404, detail="csv context not found")
    # An unconfirmed context past its ~1-hour expires_at is gone as far as any route is
    # concerned, even before the retention cron physically deletes it (Phase D session 4) --
    # the same 404, so expiry isn't distinguishable from never-existed either.
    if (
        row.status == "unconfirmed"
        and row.expires_at is not None
        and row.expires_at <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=404, detail="csv context not found")
    return row


def _load_confirmed_csv_statement(session, csv_context_id: uuid.UUID, account_id: uuid.UUID) -> CsvStatement:
    """
    Like _get_owned_csv_statement, but also requires the row to be confirmed -- an unconfirmed
    or still-in-progress csv_context_id is just as unusable to /v1/ask as one that doesn't exist
    or belongs to someone else, so it gets the same 404 rather than a distinct status a caller
    could use to fish for a context's existence/ownership.
    """
    row = _get_owned_csv_statement(session, csv_context_id, account_id)
    if row.status != "confirmed":
        raise HTTPException(status_code=404, detail="csv context not found")
    return row


def _get_owned_conversation(session, conversation_id: uuid.UUID, account_id: uuid.UUID) -> Conversation:
    """
    Load a conversations row scoped to the authenticated caller's account_id --
    symmetric to _get_owned_csv_statement above, for the same anti-enumeration reason: a
    nonexistent conversation_id and one owned by a different account are indistinguishable
    to the caller (both 404).
    """
    row = session.get(Conversation, conversation_id)
    if row is None or row.account_id != account_id:
        raise HTTPException(status_code=404, detail="conversation not found")
    return row


def _failed_transition(usage_event_id: int):
    """IN_PROGRESS -> FAILED, conditionally (app/ask_rules.py's request state machine): a row
    that already left 'in_progress' -- answered, or marked lost by a replay -- is left alone."""
    return (
        update(UsageEvent)
        .where(UsageEvent.id == usage_event_id, UsageEvent.outcome == IN_PROGRESS)
        .values(outcome="error")
    )


def _completion_transition(usage_event_id: int, turn_id: uuid.UUID, outcome: str):
    """IN_PROGRESS -> DONE, conditionally, in the same transaction as the Turn insert. Zero rows
    updated means a replay already marked this request lost (then failed) while the run was
    still going: the caller rolls the whole transaction back, so no orphan Turn is left."""
    return (
        update(UsageEvent)
        .where(UsageEvent.id == usage_event_id, UsageEvent.outcome == IN_PROGRESS)
        .values(turn_id=turn_id, outcome=outcome)
    )


def _mark_failed(usage_event_id: int) -> None:
    """Records a failed attempt: still counted toward the cap ('error' is a counted outcome),
    exactly once, and never over a row that already finished or was marked lost."""
    session = get_session()
    try:
        session.execute(_failed_transition(usage_event_id))
        session.commit()
    finally:
        session.close()


def _tool_calls_summary(tool_calls: list[dict]) -> list[dict]:
    return [{"tool_name": call["tool_name"], "is_error": call["is_error"]} for call in tool_calls]


def _find_request(session, account_id: uuid.UUID, request_id: uuid.UUID) -> UsageEvent | None:
    """The usage_events row for (account_id, request_id) -- the only replay lookup key, always
    scoped by account, so one account can never see or replay another's request."""
    return session.execute(
        select(UsageEvent).where(
            UsageEvent.account_id == account_id, UsageEvent.request_id == request_id
        )
    ).scalar_one_or_none()


def _replay_response(
    session, row: UsageEvent, fingerprint: str | None, account_id: uuid.UUID, now: datetime
) -> AskResponse:
    """The response for a request_id that already has a row, by app/ask_rules.replay_state:
    the stored answer, exactly as it was returned, for a finished request; the structured
    409/422 otherwise. A LOST row is moved to 'error' in `session`; the caller commits."""
    state = replay_state(
        row.outcome, row.turn_id, row.occurred_at, row.request_fingerprint, fingerprint, now
    )
    if state == REUSED:
        raise HTTPException(status_code=422, detail=ask_error(REUSED))
    if state == RUNNING:
        raise HTTPException(status_code=409, detail=ask_error(RUNNING))
    if state == LOST:
        session.execute(_failed_transition(row.id))
        raise HTTPException(status_code=409, detail=ask_error(LOST))
    if state == FAILED:
        raise HTTPException(status_code=409, detail=ask_error(FAILED))
    if state == DONE:
        turn = session.get(Turn, row.turn_id)
        conversation = session.get(Conversation, turn.conversation_id) if turn is not None else None
        if conversation is not None and conversation.account_id == account_id:
            return AskResponse(
                conversation_id=str(conversation.id),
                turn_id=str(turn.id),
                final_answer=turn.final_answer,
                hit_iteration_cap=turn.hit_iteration_cap,
                figure_check=turn.figure_check,
                citations=turn.citations or [],
                tool_calls_summary=_tool_calls_summary(turn.tool_calls or []),
            )
    logger.warning(
        "unexpected /v1/ask request state usage_event_id=%s outcome=%s has_turn_id=%s",
        row.id,
        row.outcome,
        row.turn_id is not None,
    )
    raise HTTPException(status_code=409, detail=ask_error(FAILED))


def _replay_if_known(
    account_id: uuid.UUID, request_id: uuid.UUID, fingerprint: str | None
) -> AskResponse | None:
    """None when this request_id is new; otherwise its replay response (or raised error). Never
    takes the account lock, so a replay never waits on a question being charged."""
    session = get_session()
    try:
        row = _find_request(session, account_id, request_id)
        if row is None:
            return None
        try:
            return _replay_response(session, row, fingerprint, account_id, datetime.now(timezone.utc))
        finally:
            session.commit()  # persists a LOST row's move to 'error'; otherwise a no-op
    finally:
        session.close()


@dataclass
class _AskContext:
    """Everything /v1/ask needs from the database before it charges and runs, copied into plain
    values while the loading session is open, so no session stays open across run_agent."""

    conversation_id: uuid.UUID | None
    prior_messages: list[dict] | None
    prior_tool_calls: list[dict] | None
    statement_id: uuid.UUID | None  # used this turn; a new conversation is bound to it
    statement_data: list | None
    statement_attrs: dict | None
    statement_raw: dict | None


def _statement_names(ctx: "_AskContext") -> list[str]:
    """The bound statement's sheet, file and business names, for the figure check: digits in
    a name like "P&L (000s)" or "Studio 54" aren't figures (src/agent/guardrails.py, which also
    ignores any name too short or purely numeric to exclude safely). Empty without a
    statement."""
    if ctx.statement_id is None:
        return []
    source = (ctx.statement_raw or {}).get("source") or {}
    names = [source.get("sheet_name"), source.get("file_name"), (ctx.statement_attrs or {}).get("entity_name")]
    return [name for name in names if isinstance(name, str) and name.strip()]


def _parse_uuid(value: str | None, not_found_detail: str) -> uuid.UUID | None:
    """A request id as a UUID. Malformed by construction can't match any row, so it gets the
    same 404 as a well-formed id that doesn't exist -- never a silent fallback."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=not_found_detail) from e


def _recent_turns(session, conversation_id: uuid.UUID) -> list[Turn]:
    """The last MAX_PRIOR_TURNS turns, oldest first. Only called right after
    _get_owned_conversation has checked the conversation belongs to the caller."""
    turns = (
        session.query(Turn)
        .filter(Turn.conversation_id == conversation_id)
        .order_by(Turn.created_at.desc())
        .limit(MAX_PRIOR_TURNS)
        .all()
    )
    turns.reverse()
    return turns


def _csv_tool_turns(conversation_id: uuid.UUID):
    """The tool_calls of every turn of this conversation -- all of them, not only the replayed
    ones -- that called a CSV tool. Array containment (@>) on the JSONB list run_agent returns
    is only a prefilter: whether a call actually got statement data back is decided in Python
    (ask_rules.turns_returned_statement_data), because each call's tool_result is stored as a
    JSON *string* that SQL can't inspect without a cast that fails on non-JSON text. Used only
    when a conversation has no recorded binding, to tell "bound to a statement that's since
    gone" (created before migration 0005) from "never bound"."""
    return select(Turn.tool_calls).where(
        Turn.conversation_id == conversation_id,
        or_(*(Turn.tool_calls.contains([{"tool_name": name}]) for name in CSV_TOOLS)),
    )


def _load_ask_context(
    account_id: uuid.UUID,
    conversation_uuid: uuid.UUID | None,
    requested_csv_uuid: uuid.UUID | None,
) -> _AskContext:
    """Conversation history, the statement binding (app/ask_rules.binding_decision) and the
    statement itself -- all before anything is charged, so a refusal here costs nothing. The
    statement is only ever loaded through the account-scoped, confirmed-only loader: for a
    continuing conversation, a statement it refuses (gone, or not this account's) is
    statement_needs_reconfirm, never another account's data."""
    session = get_session()
    try:
        prior_messages = prior_tool_calls = None
        statement_id = requested_csv_uuid
        continuing = conversation_uuid is not None
        if continuing:
            conversation = _get_owned_conversation(session, conversation_uuid, account_id)
            turns = _recent_turns(session, conversation_uuid)
            prior_messages = build_prior_messages(turns)
            # Exactly the replayed turns' calls, tagged with their turn, for the figure check
            # and citations -- never a wider window.
            prior_tool_calls = [
                {**call, "turn_id": str(turn.id)} for turn in turns for call in (turn.tool_calls or [])
            ]
            decision = binding_decision(
                conversation.bound_csv_context_id,
                conversation.csv_context_id,
                requested_csv_uuid,
                lambda: turns_returned_statement_data(
                    session.execute(_csv_tool_turns(conversation_uuid)).scalars()
                ),
            )
            if decision.kind in (MISMATCH, NEEDS_RECONFIRM):
                raise HTTPException(status_code=409, detail=ask_error(decision.kind))
            statement_id = decision.statement_id

        statement_data = statement_attrs = statement_raw = None
        if statement_id is not None:
            try:
                row = _load_confirmed_csv_statement(session, statement_id, account_id)
            except HTTPException:
                if continuing:
                    raise HTTPException(
                        status_code=409, detail=ask_error(NEEDS_RECONFIRM)
                    ) from None
                raise
            if statement_problem(row.status, row.statement_data, row.statement_attrs) is not None:
                raise HTTPException(status_code=409, detail=ask_error(NEEDS_RECONFIRM))
            statement_data = row.statement_data
            statement_attrs = row.statement_attrs
            statement_raw = row.raw_columns

        return _AskContext(
            conversation_id=conversation_uuid,
            prior_messages=prior_messages,
            prior_tool_calls=prior_tool_calls,
            statement_id=statement_id,
            statement_data=statement_data,
            statement_attrs=statement_attrs,
            statement_raw=statement_raw,
        )
    finally:
        session.close()


@dataclass
class _Charge:
    """The outcome of the gate: either a replay response (the request_id appeared while
    waiting for the lock) or a charged, in-progress question."""

    response: AskResponse | None = None
    usage_event_id: int | None = None
    byo_client: anthropic.Anthropic | None = None
    tier: str | None = None


def _byo_unavailable(e: ByoUnavailable) -> HTTPException:
    # str(e) names the failed check ("... is not set"), never a value.
    logger.warning("BYO keys unavailable: %s", e)
    return HTTPException(status_code=503, detail={"error": "byo_unavailable"})


def _lock_account(session, account_id: uuid.UUID) -> None:
    """Row-locks the account until the caller's transaction commits, so the gate check and the
    usage insert are serialized per account: two simultaneous questions can't both pass a cap
    with one question left. Held for milliseconds -- _charge commits before run_agent -- and
    never taken by a replay lookup."""
    session.execute(select(Account.id).where(Account.id == account_id).with_for_update())


def _charge(
    account: Account, request_id: uuid.UUID | None, fingerprint: str | None
) -> _Charge:
    """In one short, locked transaction: re-check the request_id (a resend may have arrived
    first), run the gate, then insert the 'in_progress' row that counts this question. Commits
    -- releasing the lock -- before returning, and copies the row id before the commit so
    nothing reads an expired attribute afterwards."""
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        _lock_account(session, account.id)

        if request_id is not None:
            existing = _find_request(session, account.id, request_id)
            if existing is not None:
                try:
                    return _Charge(
                        response=_replay_response(session, existing, fingerprint, account.id, now)
                    )
                finally:
                    session.commit()  # releases the lock (and persists a LOST row's move)

        decision = evaluate_ask_gate(session, account, now)
        if not decision.allowed:
            # Never tied to the request: a 429 isn't a replayable attempt, and the same
            # request_id must work again once the cap resets.
            session.add(
                UsageEvent(
                    account_id=account.id,
                    occurred_at=now,
                    turn_id=None,
                    outcome=decision.reject_outcome,
                    tier=decision.tier,
                )
            )
            session.commit()
            raise HTTPException(
                status_code=429,
                detail={
                    "error": decision.reject_error,
                    "prompt_byo_key": decision.prompt_byo_key,
                    "prompt_upgrade": decision.prompt_upgrade,
                    "resets_at": decision.resets_at.isoformat() if decision.resets_at else None,
                },
            )

        byo_client: anthropic.Anthropic | None = None
        if decision.tier == "byo_key":
            byo_key = session.get(ByoKey, account.byo_key_id)
            if byo_key is None:
                # evaluate_ask_gate just confirmed an active row exists -- same session,
                # no intervening commit could have removed it. Treat as an internal
                # inconsistency rather than silently falling back to the master key.
                raise HTTPException(status_code=500, detail="BYO key lookup failed unexpectedly.")
            # Encryption not configured: a server problem, not this person's key. Nothing is
            # touched and nothing is counted -- the session closes (rolling back, releasing the
            # lock) before any usage row exists (backend/SECURITY.md SS8).
            try:
                ensure_byo_encryption_configured()
            except ByoUnavailable as e:
                raise _byo_unavailable(e) from None
            try:
                raw_key = decrypt_byo_key(byo_key.encrypted_key)
            except InvalidToken:
                # A valid Fernet key that can't read this ciphertext: a rotation, or a
                # valid-but-different BYO_KEY_ENCRYPTION_KEY (a local .env differing from Render
                # on the shared database) -- indistinguishable here. So the key is deactivated
                # and unlinked, but its ciphertext is KEPT: erasing it would let one
                # misconfigured process destroy every BYO user's key (backend/SECURITY.md SS8).
                # Done in this session: a second session couldn't update the account row this
                # one holds locked. Committed (releasing the lock) before the question counts.
                byo_key_id = byo_key.id  # copied before commit: reading it after would autobegin
                byo_key.is_active = False
                session.execute(
                    update(Account).where(Account.id == account.id).values(byo_key_id=None)
                )
                session.commit()
                logger.warning(
                    "stored BYO key could not be decrypted and was deactivated byo_key_id=%s (%s)",
                    byo_key_id, InvalidToken.__name__,
                )
                raise HTTPException(
                    status_code=409, detail={"error": "byo_key_needs_readd"}
                ) from None
            byo_client = anthropic.Anthropic(api_key=raw_key, timeout=MODEL_CALL_TIMEOUT_SECONDS)
            byo_key.last_used_at = now

        # Counted from here, even if the run crashes later (it then moves to 'error') -- unless
        # tier is 'byo_key': set here, at insert, and never changed by the later transitions,
        # so a BYO question is excluded from the caps even while it's still running.
        usage_event = UsageEvent(
            account_id=account.id,
            occurred_at=now,
            turn_id=None,
            outcome=IN_PROGRESS,
            request_id=request_id,
            request_fingerprint=fingerprint,
            tier=decision.tier,
        )
        session.add(usage_event)
        try:
            session.flush()
        except IntegrityError:
            # The backstop for a resend that slipped in despite the in-lock re-check.
            session.rollback()
            raise HTTPException(status_code=409, detail=ask_error(RUNNING)) from None
        usage_event_id = usage_event.id  # copied before commit: reading it after would autobegin
        session.commit()
        return _Charge(usage_event_id=usage_event_id, byo_client=byo_client, tier=decision.tier)
    finally:
        session.close()


def _persist_turn(
    account_id: uuid.UUID,
    ctx: _AskContext,
    question: str,
    result: dict,
    citations: list[dict],
    usage_event_id: int,
    started: float,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Stores the conversation (a new one is bound to this turn's statement), the turn with its
    citations, and moves the usage row IN_PROGRESS -> DONE -- all in one transaction. If the
    conditional move finds the row already marked failed, everything rolls back and the
    caller gets answer_failed."""
    turn_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        if ctx.conversation_id is not None:
            conversation = session.get(Conversation, ctx.conversation_id)
            # Ownership re-checked here rather than trusted from the earlier, closed session
            # (backend/SECURITY.md SS1).
            if conversation is None or conversation.account_id != account_id:
                raise HTTPException(status_code=404, detail="conversation not found")
            conversation.last_turn_at = now
            conversation_id = conversation.id
        else:
            conversation_id = uuid.uuid4()
            session.add(
                Conversation(
                    id=conversation_id,
                    account_id=account_id,
                    title=question[:200],
                    csv_context_id=ctx.statement_id,
                    bound_csv_context_id=ctx.statement_id,
                    last_turn_at=now,
                )
            )
        session.flush()  # the conversation row exists before the Turn references it

        session.add(
            Turn(
                id=turn_id,
                conversation_id=conversation_id,
                question=result["question"],
                final_answer=result["final_answer"],
                hit_iteration_cap=result["hit_iteration_cap"],
                iterations_used=result["iterations_used"],
                stop_reason=result["stop_reason"],
                figure_check=result["figure_check"],
                tool_calls=result["tool_calls"],
                citations=citations,
                model=DEFAULT_MODEL,
            )
        )
        session.flush()
        final_outcome = "hit_iteration_cap" if result["hit_iteration_cap"] else "answered"
        updated = session.execute(_completion_transition(usage_event_id, turn_id, final_outcome))
        if updated.rowcount != 1:
            session.rollback()
            logger.warning(
                "/v1/ask run finished after its request left in_progress usage_event_id=%s elapsed_ms=%d",
                usage_event_id,
                int((time.monotonic() - started) * 1000),
            )
            raise HTTPException(status_code=409, detail=ask_error(FAILED))
        session.commit()
        return conversation_id, turn_id
    finally:
        session.close()


@app.post("/v1/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    account: Account = Depends(get_current_account),
) -> AskResponse:
    """
    EXTENSION_INTEGRATION.md SS6 /v1/ask (amended Phase D session 5). In order:
      1. replay: a known request_id gets its stored answer or its state, free (no lock);
      2. load: conversation, binding and statement -- refusals here cost nothing;
      3. charge: lock the account, re-check the request_id, gate, insert 'in_progress', commit;
      4. run_agent, with no DB session open;
      5. persist the turn and its citations, moving the row to done conditionally.
    """
    started = time.monotonic()
    account_id = account.id
    fingerprint = (
        request_fingerprint(request.question, request.csv_context_id, request.conversation_id)
        if request.request_id is not None
        else None
    )

    if request.request_id is not None:
        stored = _replay_if_known(account_id, request.request_id, fingerprint)
        if stored is not None:
            return stored

    conversation_uuid = _parse_uuid(request.conversation_id, "conversation not found")
    csv_context_uuid = _parse_uuid(request.csv_context_id, "csv context not found")
    ctx = _load_ask_context(account_id, conversation_uuid, csv_context_uuid)
    df = (
        statement_from_records(ctx.statement_data, ctx.statement_attrs)
        if ctx.statement_id is not None
        else None
    )

    charge = _charge(account, request.request_id, fingerprint)
    if charge.response is not None:
        return charge.response
    usage_event_id = charge.usage_event_id

    csv_token = csv_session.set_active_csv_with_token(df) if df is not None else None
    try:
        try:
            # client is only passed when a BYO key applies -- omitting the kwarg otherwise keeps
            # run_agent's own default client (the master key, with the same per-call timeout)
            # in charge for the free/paid tiers.
            run_agent_kwargs = {
                "prior_messages": ctx.prior_messages,
                "prior_tool_calls": ctx.prior_tool_calls,
                "excluded_phrases": _statement_names(ctx),
            }
            if charge.byo_client is not None:
                run_agent_kwargs["client"] = charge.byo_client
            result = run_agent(request.question, **run_agent_kwargs)
        except AgentTimeBudgetExceeded as e:
            _mark_failed(usage_event_id)
            raise HTTPException(
                status_code=504, detail=ask_error("answer_time_budget_exceeded")
            ) from e
        except anthropic.AuthenticationError as e:
            # Caught ahead of the broader anthropic.APIError handler below (it's a
            # subclass -- order matters). A key-rejection error is the one failure mode
            # the BYO-key work makes concretely dangerous: the request that failed just
            # carried either this caller's own BYO key or this server's master key, so
            # str(e)/e.args must never reach the HTTP response. The real exception (with
            # traceback) is still logged server-side, so debuggability isn't lost.
            _mark_failed(usage_event_id)
            logger.exception("Anthropic authentication error in /v1/ask (tier=%s)", charge.tier)
            if charge.byo_client is not None:
                detail = "Your Anthropic API key was rejected. Please re-register a valid key."
            else:
                detail = "Anthropic API authentication failed."
            raise HTTPException(status_code=502, detail=detail) from e
        except anthropic.APIError as e:
            # str(e) is deliberately kept out of the response (session 10's security
            # hardening pass) -- the full exception is captured by logger.exception below.
            _mark_failed(usage_event_id)
            logger.exception("Anthropic API error in /v1/ask")
            raise HTTPException(status_code=502, detail="Anthropic API error.") from e
        except Exception as e:  # noqa: BLE001 -- surfaced as a clean 500, not a bare 500 traceback
            # A tool-execution bug, a pandas/EDGAR error, etc. inside run_agent could in
            # principle embed request detail, so str(e) never reaches the response; the full
            # exception is captured server-side by logger.exception below.
            _mark_failed(usage_event_id)
            logger.exception("run_agent failed unexpectedly in /v1/ask")
            raise HTTPException(status_code=500, detail="run_agent failed unexpectedly.") from e
    finally:
        if csv_token is not None:
            csv_session.reset_active_csv(csv_token)

    citations = build_citations(
        result["figure_check"], result["tool_calls"], ctx.prior_tool_calls, ctx.statement_raw
    )
    try:
        conversation_id, turn_id = _persist_turn(
            account_id, ctx, request.question, result, citations, usage_event_id, started
        )
    except HTTPException:
        _mark_failed(usage_event_id)  # conditional: a no-op if the row already left in_progress
        raise

    return AskResponse(
        conversation_id=str(conversation_id),
        turn_id=str(turn_id),
        final_answer=result["final_answer"],
        hit_iteration_cap=result["hit_iteration_cap"],
        figure_check=result["figure_check"],
        citations=citations,
        tool_calls_summary=_tool_calls_summary(result["tool_calls"]),
    )


async def _read_csv_parse_body(request: Request) -> CsvParseRequest | None:
    """
    /v1/csv/parse's body, read with a MAX_CSV_PARSE_BODY_BYTES cap before any JSON parsing.
    Returns None when the body is over the cap (the route then refuses in-band, keeping the
    contract's "always 200" rule), and raises RequestValidationError -- FastAPI's ordinary 422
    -- for malformed JSON or a body that doesn't match CsvParseRequest. A dependency rather
    than a body parameter so it runs *after* get_current_account: an unauthenticated caller
    gets its 401 before a single body byte is read. Checks Content-Length first, then counts
    streamed bytes, so a chunked body without a Content-Length can't slip past the cap.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_CSV_PARSE_BODY_BYTES:
        return None
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_CSV_PARSE_BODY_BYTES:
            return None
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RequestValidationError(
            [{"type": "json_invalid", "loc": ("body",), "msg": "Request body is not valid JSON."}]
        ) from e
    try:
        return CsvParseRequest.model_validate(payload)
    except ValidationError as e:
        raise RequestValidationError(
            [{**err, "loc": ("body", *err["loc"])} for err in e.errors(include_url=False)]
        ) from e


@app.post("/v1/csv/parse", response_model=CsvParseResponse)
def csv_parse(
    account: Account = Depends(get_current_account),
    request: CsvParseRequest | None = Depends(_read_csv_parse_body),
) -> CsvParseResponse:
    if request is None:
        return CsvParseResponse(
            csv_context_id=None,
            columns=[],
            sample_rows=[],
            parse_error=(
                f"This selection is too large to send (over "
                f"{MAX_CSV_PARSE_BODY_BYTES // (1024 * 1024)} MB). Select a smaller range."
            ),
        )
    source = request.source.model_dump(mode="json") if request.source else None
    raw, error = rows_to_raw_csv(request.rows, request.filename, source=source)
    if error is not None:
        return CsvParseResponse(csv_context_id=None, columns=[], sample_rows=[], parse_error=error)

    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        row = CsvStatement(
            account_id=account.id,
            status="unconfirmed",
            filename=raw.filename,
            uploaded_at=now,
            raw_columns=raw_csv_to_json(raw),
            expires_at=now + timedelta(hours=1),
        )
        session.add(row)
        session.commit()
        csv_context_id = row.id
    finally:
        session.close()

    return CsvParseResponse(
        csv_context_id=str(csv_context_id),
        columns=raw.df.columns.tolist(),
        sample_rows=raw.df.head(MAX_SAMPLE_ROWS).values.tolist(),
        parse_error=None,
    )


@app.post("/v1/csv/{csv_context_id}/propose-mapping", response_model=ProposeMappingResponse)
def propose_mapping(
    csv_context_id: uuid.UUID,
    account: Account = Depends(get_current_account),
) -> ProposeMappingResponse:
    """
    Phase D session 4: idempotent per context -- a stored proposal is returned without a second
    model call. A fresh model call is gated by a per-account daily cap counted from usage_events
    ('mapping_proposal', never part of the question caps), and the row is written before the
    call so a failed call still counts. A confirmed context can't be re-proposed (409).
    """
    session = get_session()
    try:
        row = _get_owned_csv_statement(session, csv_context_id, account.id)
        if row.status == "confirmed":
            raise HTTPException(status_code=409, detail="csv context already confirmed")

        stored = _stored_proposal(row.proposed_mapping)
        if stored is not None:
            return stored

        raw = raw_csv_from_json(row.raw_columns, row.filename, row.uploaded_at)

        now = datetime.now(timezone.utc)
        decision = evaluate_mapping_proposal_gate(session, account.id, now)
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "mapping_cap_reached",
                    "resets_at": decision.resets_at.isoformat() if decision.resets_at else None,
                },
            )
        session.add(
            UsageEvent(
                account_id=account.id, occurred_at=now, turn_id=None, outcome=MAPPING_PROPOSAL_OUTCOME
            )
        )
        session.commit()

        try:
            result = generate_mapping_proposal(raw, roles=MAPPABLE_ROLES)
        except anthropic.APIError as e:
            # str(e) deliberately kept out of the response -- same session-10 hardening
            # rule already applied to /v1/ask's equivalent handler below. The full
            # exception, including any embedded request/response detail, is still
            # captured server-side via logger.exception.
            logger.exception("Anthropic API error in propose_mapping")
            raise HTTPException(status_code=502, detail="Anthropic API error.") from e
        except Exception as e:  # noqa: BLE001 -- surfaced as a clean 500, not a bare traceback
            logger.exception("generate_mapping_proposal failed unexpectedly")
            raise HTTPException(status_code=500, detail="propose_mapping failed unexpectedly.") from e

        proposal = [
            {"csv_column": c.csv_column, "proposed_role": c.proposed_role, "rationale": c.rationale}
            for c in result.columns
        ]
        # Only a usable proposal is kept for reuse. When the model's output couldn't be used
        # (result.note set, every column "unmapped"), nothing is stored, so asking again makes
        # a fresh -- counted -- attempt rather than replaying the failure forever.
        if result.note is None:
            row.proposed_mapping = {"proposal": proposal, "note": None}
            session.commit()

        return ProposeMappingResponse(
            proposal=[MappingProposalEntry(**entry) for entry in proposal], note=result.note
        )
    finally:
        session.close()


def _stored_proposal(stored) -> ProposeMappingResponse | None:
    """A csv_statements.proposed_mapping value as a response, or None if nothing reusable is
    stored. Reads both shapes: {"proposal": [...], "note": ...} (Phase D session 4 on) and the
    bare list rows written before it."""
    if not stored:
        return None
    if isinstance(stored, list):
        entries, note = stored, None
    else:
        entries, note = stored.get("proposal") or [], stored.get("note")
    if not entries:
        return None
    return ProposeMappingResponse(
        proposal=[MappingProposalEntry(**entry) for entry in entries], note=note
    )


def _ack_fingerprint(mapping: dict, scale: str, unparsed_cells: list[dict]) -> str:
    """Binds an acknowledgement of unparsed cells to exactly what the person saw (Phase D
    session 4): SHA-256 of the canonical JSON of the mapping, the scale and the sorted unparsed-
    cell list. A confirm whose mapping, scale or underlying cells changed since then gets a
    different fingerprint, so an old acknowledgement can't carry over to a new list."""
    canonical = json.dumps(
        {
            "mapping": mapping,
            "scale": scale,
            "unparsed_cells": sorted(unparsed_cells, key=lambda c: (c["role"], c["source_row"])),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.post("/v1/csv/{csv_context_id}/confirm", response_model=ConfirmResponse)
def confirm_mapping(
    csv_context_id: uuid.UUID,
    request: ConfirmRequest,
    account: Account = Depends(get_current_account),
) -> ConfirmResponse:
    """
    Phase D session 4: a confirmed context is immutable (409) -- re-mapping means a new
    /csv/parse and a new id, so conversations that used the old statement keep citing what
    they saw. Non-blank mapped cells that don't parse as numbers block confirmation until the
    person acknowledges that exact list (accept_unparsed_cells plus the ack_fingerprint this
    route returned for it).
    """
    session = get_session()
    try:
        row = _get_owned_csv_statement(session, csv_context_id, account.id)
        if row.status == "confirmed":
            raise HTTPException(status_code=409, detail="csv context already confirmed")
        raw = raw_csv_from_json(row.raw_columns, row.filename, row.uploaded_at)
        units = {"scale": request.scale, "currency": request.currency}

        errors = validate_mapping(raw, request.mapping)
        if errors:
            return ConfirmResponse(confirmed=False, errors=errors, warnings=[], **units)

        serial_reason = find_period_serial_number_value(raw, request.mapping)
        if serial_reason is not None:
            return ConfirmResponse(confirmed=False, errors=[serial_reason], warnings=[], **units)

        df, errors, warnings = normalize(
            raw, request.mapping, request.entity_name, scale=request.scale, currency=request.currency
        )
        if errors:
            return ConfirmResponse(confirmed=False, errors=errors, warnings=warnings, **units)

        unparsed = find_unparsed_cells(raw, request.mapping)
        if unparsed:
            fingerprint = _ack_fingerprint(request.mapping, request.scale, unparsed)
            if not (request.accept_unparsed_cells and request.ack_fingerprint == fingerprint):
                return ConfirmResponse(
                    confirmed=False,
                    requires_acknowledgement=True,
                    unparsed_cells=unparsed,
                    ack_fingerprint=fingerprint,
                    warnings=warnings,
                    **units,
                )

        concepts_unavailable = [
            concept for concept in RECOMMENDED_CONCEPTS if concept not in request.mapping.values()
        ]

        row.confirmed_mapping = request.mapping
        row.entity_name = request.entity_name
        row.cadence = df.attrs["csv_source"]["cadence"]
        row.statement_data = json.loads(df.to_json(orient="records", date_format="iso"))
        row.statement_attrs = df.attrs
        row.status = "confirmed"
        row.confirmed_at = datetime.now(timezone.utc)
        row.expires_at = None
        session.commit()

        return ConfirmResponse(
            confirmed=True,
            cadence=row.cadence,
            warnings=warnings,
            concepts_unavailable=concepts_unavailable,
            errors=[],
            unparsed_cells=unparsed,
            period_count=len(row.statement_data),
            **units,
        )
    finally:
        session.close()


@app.post("/v1/auth/exchange", response_model=AuthExchangeResponse)
def auth_exchange(request: AuthExchangeRequest) -> AuthExchangeResponse:
    """
    Intentionally unauthenticated, like /v1/health and /v1/billing/webhook -- this route
    has no way to require a session token, since proving identity to obtain one is exactly
    what it's for. Verifies the caller's OAuth token directly against the provider
    (app.oauth_providers), resolves it to an account per design doc SS3, and issues a new
    opaque session token.
    """
    if request.provider == "google":
        # Google's "Web application" client type is a confidential client -- PKCE alone
        # doesn't satisfy its token endpoint, unlike Microsoft's Azure SPA platform type
        # below -- so the extension sends the raw code/verifier/redirect_uri instead of
        # a pre-exchanged token, and this exchange happens here (Phase D session 2
        # amendment; see app/oauth_providers.py's exchange_google_code_for_token).
        if not (request.code and request.code_verifier and request.redirect_uri):
            raise HTTPException(
                status_code=422,
                detail="Google sign-in requires code, code_verifier, and redirect_uri.",
            )
        try:
            oauth_token = oauth_providers.exchange_google_code_for_token(
                request.code, request.code_verifier, request.redirect_uri
            )
        except oauth_providers.InvalidProviderTokenError as e:
            raise HTTPException(status_code=401, detail="OAuth token could not be verified.") from e
        except oauth_providers.OAuthProviderConfigError as e:
            raise HTTPException(status_code=500, detail="Google OAuth is not configured.") from e
        verify = oauth_providers.verify_google_token
    else:
        if not request.oauth_token:
            raise HTTPException(status_code=422, detail="Microsoft sign-in requires oauth_token.")
        oauth_token = request.oauth_token
        verify = oauth_providers.verify_microsoft_token

    try:
        identity = verify(oauth_token)
    except oauth_providers.InvalidProviderTokenError as e:
        raise HTTPException(status_code=401, detail="OAuth token could not be verified.") from e
    except oauth_providers.ProviderEmailUnavailableError as e:
        raise HTTPException(
            status_code=422, detail="No usable email address is available for this account."
        ) from e

    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        account = _resolve_account_for_identity(session, identity, now)
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = now + timedelta(days=90)
        session.add(SessionModel(
            account_id=account.id,
            token_hash=token_hash,
            created_via_provider=identity.provider,
            last_used_at=now,
            expires_at=expires_at,
        ))
        session.commit()
        account_id = account.id
    finally:
        session.close()

    return AuthExchangeResponse(
        session_token=raw_token, account_id=str(account_id), expires_at=expires_at
    )


@app.post("/v1/google/data-token", response_model=GoogleDataTokenResponse)
def google_data_token(
    request: GoogleDataTokenRequest,
    response: Response,
    account: Account = Depends(get_current_account),
) -> GoogleDataTokenResponse:
    """
    EXTENSION_INTEGRATION.md SS1a (Phase D session 3a): exchanges a Google data-grant code
    for a short-lived access token the extension uses to call the Sheets API itself. Google's
    "Web application" client is confidential, so this exchange needs GOOGLE_CLIENT_SECRET and
    can't happen in the extension; this backend never reads spreadsheet data with the token.

    The session requirement (get_current_account, the only source of a 401 here) is for
    authentication and abuse control, so this confidential client isn't a free anonymous
    code-exchange service. `account` is otherwise unused on purpose: the Google account the
    code was granted for is NOT compared against the caller's linked identities, because the
    data account may legitimately differ from the sign-in identity
    (docs/chrome-extension-design.md SS2, "Data account vs sign-in identity"). The code is
    PKCE-bound and came through Google's own consent, so a stolen session can only mint
    tokens for Google accounts its holder already controls.

    A failed exchange or userinfo call is a 400, deliberately not a 401: on an authenticated
    route a 401 means "discard the session", which a failed data grant shouldn't trigger.
    Nothing is stored and nothing is logged -- no DB session is opened, no logger call is
    made -- and the response is marked Cache-Control: no-store.
    """
    if not (request.code and request.code_verifier and request.redirect_uri):
        raise HTTPException(
            status_code=422, detail="code, code_verifier, and redirect_uri are required."
        )
    try:
        data_token = oauth_providers.exchange_google_data_code(
            request.code, request.code_verifier, request.redirect_uri
        )
        # Reuses sign-in's userinfo call only to read the data account's email (its
        # login_hint for later silent re-auth); its `subject` is ignored, per the docstring.
        email = oauth_providers.verify_google_token(data_token.access_token).email
    except oauth_providers.InvalidProviderTokenError as e:
        raise HTTPException(
            status_code=400, detail="Google authorization could not be exchanged."
        ) from e
    except oauth_providers.OAuthProviderConfigError as e:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured.") from e

    response.headers["Cache-Control"] = "no-store"
    return GoogleDataTokenResponse(
        access_token=data_token.access_token,
        expires_in=data_token.expires_in,
        scope=data_token.scope,
        email=email,
    )


@app.post("/v1/auth/logout", response_model=LogoutResponse)
def logout(
    request: Request, account: Account = Depends(get_current_account)
) -> LogoutResponse:
    """
    Design doc SS2's "Revocation" subsection: revokes only the sessions row the presented
    token validated against (request.state.session_id, set by get_current_account) -- other
    devices' sessions for the same account are left untouched, so logging out on one device
    never signs the account out everywhere.
    """
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        session.execute(
            update(SessionModel)
            .where(
                SessionModel.id == request.state.session_id,
                SessionModel.account_id == account.id,
            )
            .values(revoked_at=now)
        )
        session.commit()
    finally:
        session.close()
    return LogoutResponse(revoked=True)


@app.post("/v1/auth/sessions/revoke-all", response_model=RevokeAllSessionsResponse)
def revoke_all_sessions(
    account: Account = Depends(get_current_account),
) -> RevokeAllSessionsResponse:
    """
    Design doc SS2's compromised-token path: revokes every sessions row for the caller's
    account_id, including the one making this very request, forcing a fresh
    /v1/auth/exchange on every device.
    """
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        session.execute(
            update(SessionModel)
            .where(
                SessionModel.account_id == account.id,
                SessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        session.commit()
    finally:
        session.close()
    return RevokeAllSessionsResponse(revoked=True)


@app.get("/v1/usage", response_model=UsageResponse)
def usage(account: Account = Depends(get_current_account)) -> UsageResponse:
    """EXTENSION_INTEGRATION.md SS6 /v1/usage (amended Phase D session 6): the gate's view
    plus resets_at, has_byo_key and the subscription's own state, whatever the tier."""
    session = get_session()
    try:
        decision = evaluate_ask_gate(session, account, datetime.now(timezone.utc))
        status = account_billing_status(session, account)
    finally:
        session.close()

    response = UsageResponse(
        account_id=str(account.id),
        tier=decision.tier,
        byo_key_required=decision.prompt_byo_key,
        resets_at=decision.resets_at,
        has_byo_key=status.has_byo_key,
        subscription=(
            UsageSubscription(
                status=status.subscription.status,
                cancel_at_period_end=status.subscription.cancel_at_period_end,
                current_period_end=status.subscription.current_period_end,
            )
            if status.subscription is not None
            else None
        ),
    )
    if decision.tier == "free":
        response.questions_today = decision.questions_used
        response.daily_cap = decision.cap
    elif decision.tier == "paid":
        response.questions_this_period = decision.questions_used
        response.monthly_cap = decision.cap
        response.period_ends_at = decision.resets_at
    return response


# The registration check's own short timeout, separate from MODEL_CALL_TIMEOUT_SECONDS: listing
# models is a single fast metadata call, and the person is waiting on the form.
BYO_KEY_CHECK_TIMEOUT_SECONDS = 10


def _check_byo_key(raw_key: str) -> Literal["ok", "rejected", "unavailable"]:
    """
    Asks Anthropic whether the key works by listing models: a metadata call that generates no
    tokens, so it spends nothing on the caller's account. Runs with no DB session open. Only
    the exception's class name is logged -- never str(e), and never the key.
    """
    client = anthropic.Anthropic(
        api_key=raw_key, timeout=BYO_KEY_CHECK_TIMEOUT_SECONDS, max_retries=0
    )
    try:
        client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        logger.warning("BYO key check: Anthropic refused the key (%s)", type(e).__name__)
        return "rejected"
    except anthropic.APIError as e:
        logger.warning("BYO key check couldn't reach a verdict (%s)", type(e).__name__)
        return "unavailable"
    return "ok"


def _retire_byo_keys(session, account_id: uuid.UUID) -> None:
    """Deactivates every BYO key row this account has and erases its ciphertext. The rows stay
    for audit (created_at, last_used_at); the old key can't be recovered from them, even with
    BYO_KEY_ENCRYPTION_KEY (backend/SECURITY.md SS8)."""
    session.execute(
        update(ByoKey)
        .where(ByoKey.account_id == account_id)
        .values(is_active=False, encrypted_key=b"")
    )


@app.post("/v1/byo-key", response_model=ByoKeyResponse)
def register_byo_key(
    request: ByoKeyRequest, account: Account = Depends(get_current_account)
) -> ByoKeyResponse:
    """
    Registers (or rotates) the caller's own Anthropic API key (EXTENSION_INTEGRATION.md SS6,
    amended Phase D session 6). In order, with nothing stored unless every step passes:
      1. strip whitespace, then the format check (422, the original string detail);
      2. check that stored keys can be encrypted at all -- before anything is sent to
         Anthropic (503 byo_unavailable; amended after the session 6 local live test, which
         hit an unhandled 500 here after the Anthropic call when the key was missing);
      3. ask Anthropic whether the key works -- no tokens, no DB session open (422
         byo_key_rejected, or 502 byo_key_check_failed);
      4. encrypt, retire every earlier key (ciphertext erased), store the new one and point
         the account at it, in one transaction.
    """
    raw_key = request.api_key.strip()
    if not is_valid_byo_key_format(raw_key):
        raise HTTPException(
            status_code=422, detail="That doesn't look like a valid Anthropic API key."
        )

    try:
        ensure_byo_encryption_configured()
    except ByoUnavailable as e:
        raise _byo_unavailable(e) from None

    verdict = _check_byo_key(raw_key)
    if verdict == "rejected":
        raise HTTPException(status_code=422, detail={"error": "byo_key_rejected"})
    if verdict == "unavailable":
        raise HTTPException(status_code=502, detail={"error": "byo_key_check_failed"})

    encrypted = encrypt_byo_key(raw_key)

    session = get_session()
    try:
        _retire_byo_keys(session, account.id)
        new_key = ByoKey(account_id=account.id, encrypted_key=encrypted, is_active=True)
        session.add(new_key)
        session.flush()  # populate new_key.id before repointing the FK below
        session.execute(
            update(Account).where(Account.id == account.id).values(byo_key_id=new_key.id)
        )
        session.commit()
    finally:
        session.close()

    return ByoKeyResponse(registered=True)


@app.delete("/v1/byo-key", response_model=ByoKeyRemoveResponse)
def remove_byo_key(account: Account = Depends(get_current_account)) -> ByoKeyRemoveResponse:
    """
    Removes the caller's BYO key (EXTENSION_INTEGRATION.md SS6, new Phase D session 6): every
    key row is deactivated with its ciphertext erased, and the account stops pointing at one,
    so the next question uses the free or paid tier. Idempotent -- the same 200 with no key.
    """
    session = get_session()
    try:
        session.execute(update(Account).where(Account.id == account.id).values(byo_key_id=None))
        _retire_byo_keys(session, account.id)
        session.commit()
    finally:
        session.close()
    return ByoKeyRemoveResponse(removed=True)


def _billing_page(title: str, message: str) -> str:
    return (
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1><p>{message}</p></body></html>"
    )


_BILLING_SUCCESS_HTML = _billing_page(
    "Subscription started",
    "You can close this tab and return to the extension. Your plan updates there once "
    "Stripe confirms the payment, usually within a few seconds.",
)
_BILLING_CANCEL_HTML = _billing_page(
    "Checkout canceled",
    "No changes were made. You can close this tab and return to the extension.",
)
_BILLING_RETURN_HTML = _billing_page(
    "Done",
    "You can close this tab and return to the extension. Any change to your subscription "
    "shows there once Stripe confirms it.",
)


@app.get("/v1/billing/success", response_class=HTMLResponse)
def billing_success() -> HTMLResponse:
    return HTMLResponse(_BILLING_SUCCESS_HTML)


@app.get("/v1/billing/cancel", response_class=HTMLResponse)
def billing_cancel() -> HTMLResponse:
    return HTMLResponse(_BILLING_CANCEL_HTML)


@app.get("/v1/billing/return", response_class=HTMLResponse)
def billing_return() -> HTMLResponse:
    """Where the Stripe customer portal's return link lands (Phase D session 6)."""
    return HTMLResponse(_BILLING_RETURN_HTML)


def _billing_error(status_code: int, error: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": error})


def _billing_unavailable(e: billing.BillingUnavailable) -> HTTPException:
    # str(e) names the failed check ("... is not a test-mode key"), never a value.
    logger.warning("billing unavailable: %s", e)
    return _billing_error(503, "billing_unavailable")


def _billing_provider_error(operation: str, e: stripe.StripeError) -> HTTPException:
    # The class name only: a Stripe error's message can echo request parameters.
    logger.warning("Stripe %s failed: %s", operation, type(e).__name__)
    return _billing_error(502, "billing_provider_error")


@dataclass(frozen=True)
class _SubscriptionSnapshot:
    status: str
    stripe_customer_id: str


def _subscription_snapshot(account_id: uuid.UUID) -> _SubscriptionSnapshot | None:
    """The caller's own subscriptions row as plain values, read in a short session that's
    closed before any Stripe call."""
    session = get_session()
    try:
        row = session.execute(
            select(Subscription.status, Subscription.stripe_customer_id).where(
                Subscription.account_id == account_id
            )
        ).one_or_none()
    finally:
        session.close()
    if row is None:
        return None
    return _SubscriptionSnapshot(status=row.status, stripe_customer_id=row.stripe_customer_id)


@app.post("/v1/billing/checkout-session", response_model=CheckoutSessionResponse)
def create_checkout_session_route(
    request: Request,
    account: Account = Depends(get_current_account),
) -> CheckoutSessionResponse:
    """
    EXTENSION_INTEGRATION.md SS6 (amended Phase D session 6). Config is checked before any
    DB access; an account whose subscription is still live gets 409 subscription_exists
    (manage it through the portal instead); a returning customer is checked out as the same
    Stripe customer.
    """
    try:
        billing.ensure_configured()
        price_id = billing.checkout_price_id()
    except billing.BillingUnavailable as e:
        raise _billing_unavailable(e) from None

    existing = _subscription_snapshot(account.id)
    if existing is not None and existing.status in billing.LIVE_SUBSCRIPTION_STATUSES:
        raise _billing_error(409, "subscription_exists")

    success_url = str(request.base_url) + "v1/billing/success"
    cancel_url = str(request.base_url) + "v1/billing/cancel"
    try:
        checkout_url = billing.create_checkout_session(
            account.id,
            account.primary_email,
            price_id,
            success_url,
            cancel_url,
            existing_customer_id=existing.stripe_customer_id if existing is not None else None,
        )
    except billing.BillingUnavailable as e:
        raise _billing_unavailable(e) from None
    except stripe.StripeError as e:
        raise _billing_provider_error("checkout session creation", e) from None
    return CheckoutSessionResponse(checkout_url=checkout_url)


@app.post("/v1/billing/portal-session", response_model=PortalSessionResponse)
def create_portal_session_route(
    request: Request,
    account: Account = Depends(get_current_account),
) -> PortalSessionResponse:
    """
    EXTENSION_INTEGRATION.md SS6 (new, Phase D session 6): Stripe's hosted customer portal
    for the caller's own subscription. The Stripe customer comes only from the caller's own
    subscriptions row -- the route takes no body, so no request field can name a customer.
    """
    try:
        billing.ensure_configured()
    except billing.BillingUnavailable as e:
        raise _billing_unavailable(e) from None

    existing = _subscription_snapshot(account.id)
    if existing is None:
        raise _billing_error(404, "no_billing_account")

    return_url = str(request.base_url) + "v1/billing/return"
    try:
        portal_url = billing.create_portal_session(existing.stripe_customer_id, return_url)
    except billing.BillingUnavailable as e:
        raise _billing_unavailable(e) from None
    except stripe.StripeError as e:
        raise _billing_provider_error("portal session creation", e) from None
    return PortalSessionResponse(portal_url=portal_url)


def _stripe_event_seen(event_id: str) -> bool:
    session = get_session()
    try:
        return session.get(StripeWebhookEvent, event_id) is not None
    finally:
        session.close()


def _process_stripe_event(event) -> None:
    """
    Runs in the threadpool (it blocks on Stripe and the DB). Order matters:
      1. a cheap read of the idempotency marker, so a redelivery never calls Stripe;
      2. prepare_event's Stripe call, with no DB session open;
      3. one transaction: insert the marker, apply the change, commit.
    """
    if _stripe_event_seen(event["id"]):
        return

    try:
        prepared = billing.prepare_event(event)
    except billing.BillingUnavailable as e:
        raise _billing_unavailable(e) from None
    except stripe.StripeError as e:
        # No marker written, so Stripe retries the event later.
        logger.warning(
            "Stripe webhook event %s (%s) couldn't be prepared: %s",
            event["id"], event["type"], type(e).__name__,
        )
        raise HTTPException(status_code=500, detail="Stripe webhook processing failed.") from None

    session = get_session()
    try:
        # Insert-first idempotency guard: the stripe_webhook_events PK is the source of
        # truth under concurrent/duplicate delivery (the read in step 1 only saves a Stripe
        # call; it has a race window). flush() (not commit()) sends the INSERT now, still
        # inside this same transaction as the mutation below -- so if apply_event raises,
        # rolling back undoes BOTH the marker and any partial mutation together, and a Stripe
        # retry correctly reprocesses from scratch.
        session.add(StripeWebhookEvent(stripe_event_id=event["id"], event_type=event["type"]))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return
        result = billing.apply_event(session, prepared, datetime.now(timezone.utc))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Ids only -- never an email or any other payload field (backend/SECURITY.md SS5/SS8).
    if result.outcome == "duplicate_subscription_ignored":
        logger.warning(
            "duplicate subscription ignored existing=%s new=%s event=%s",
            result.existing_subscription_id, result.new_subscription_id, event["id"],
        )
    elif result.outcome == "unknown_account":
        logger.warning(
            "Stripe checkout for an unknown account ignored subscription=%s event=%s",
            result.new_subscription_id, event["id"],
        )


@app.post("/v1/billing/webhook")
async def stripe_webhook(request: Request) -> dict:
    """
    One of the few endpoints (alongside /v1/health and /v1/auth/exchange) that does NOT
    require an Authorization: Bearer session token -- Stripe calls this directly and cannot
    carry one (design doc SS1's explicitly-stated exception). Deliberately async def, the
    one exception to this file's otherwise consistent sync def (threadpool-dispatched) route
    convention: it needs await request.body() to read the raw, unparsed body before FastAPI's
    normal body-parsing machinery touches it -- re-serializing a parsed body breaks Stripe's
    signature check. Everything blocking (Stripe, the DB) then runs in the threadpool, so a
    slow Stripe call doesn't stall the event loop.

    Everything before _process_stripe_event touches neither the DB nor Stripe: an unverified
    webhook is a real attack surface (design doc SS7.4).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        secret = billing.webhook_secret()
    except billing.BillingUnavailable as e:
        raise _billing_unavailable(e) from None

    try:
        event = billing.verify_webhook_event(payload, sig_header, secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="invalid Stripe webhook signature")

    if billing.is_livemode(event):
        # Test-mode phase (backend/SECURITY.md SS8): a live event shouldn't reach a test-mode
        # endpoint at all. 400 rather than an ignored 200, so it shows up as failing in the
        # Stripe dashboard.
        logger.warning("Stripe livemode event rejected id=%s type=%s", event["id"], event["type"])
        raise _billing_error(400, "livemode_event_rejected")

    await run_in_threadpool(_process_stripe_event, event)
    return {"received": True}
