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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import stripe
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text, update
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
    Turn,
    UsageEvent,
)

# No logging framework/handler config exists yet in this project -- this module-level
# logger relies on Python's logging "handler of last resort" (WARNING+ to stderr with no
# other setup) until a real one is added. logger.exception(...) below is still the right
# call now, not a premature abstraction: it's the stdlib's own idiom for "log this with a
# traceback," and switching to a configured handler later is a config change, not a
# call-site change.
logger = logging.getLogger(__name__)

# src/agent/agent.py lives one level above backend/ (see repo layout in
# CLAUDE.md), but this module is normally run with backend/ as the working
# directory (see the run instructions above), so `src` isn't importable
# without adding the repo root to sys.path -- mirrors the same bootstrap
# src/app/main.py already uses for the Streamlit app.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import csv_session  # noqa: E402 -- see sys.path note above
from src.agent.agent import DEFAULT_MODEL, run_agent  # noqa: E402 -- see sys.path note above
from src.analysis.csv_statement import (  # noqa: E402 -- see sys.path note above
    MAPPABLE_ROLES,
    RECOMMENDED_CONCEPTS,
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
from .crypto import decrypt_byo_key, encrypt_byo_key, is_valid_byo_key_format
from .gating import evaluate_ask_gate
from .history import MAX_PRIOR_TURNS, build_prior_messages
from .schemas import (
    AskRequest,
    AskResponse,
    AuthExchangeRequest,
    AuthExchangeResponse,
    ByoKeyRequest,
    ByoKeyResponse,
    CheckoutSessionResponse,
    ConfirmRequest,
    ConfirmResponse,
    CsvParseRequest,
    CsvParseResponse,
    HealthResponse,
    LogoutResponse,
    MappingProposalEntry,
    ProposeMappingResponse,
    RevokeAllSessionsResponse,
    UsageResponse,
)

app = FastAPI(title="Sheets Add-on Backend")


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
    request: Request, authorization: str = Header(alias="Authorization")
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
    """
    if not authorization.startswith("Bearer "):
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
    Design doc SS3's 3-step resolution order. Returns the Account to issue a session
    against, with last_seen_at already set to `now` on every branch. Only adds/mutates
    ORM objects -- the caller owns the transaction (commit/flush).
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

    linked_by_email = session.execute(
        select(LinkedIdentity).where(LinkedIdentity.provider_email == identity.email)
    ).scalar_one_or_none()
    if linked_by_email is not None:
        account = session.get(Account, linked_by_email.account_id)
        account.last_seen_at = now
        session.add(LinkedIdentity(
            account_id=account.id,
            provider=identity.provider,
            provider_subject=identity.subject,
            provider_email=identity.email,
        ))
        return account

    # Explicit id (not a flush-to-learn-the-default) so this branch behaves identically to
    # the other two: account.id is a concrete value immediately, before LinkedIdentity's FK
    # needs it. Don't "simplify" this back to relying on Account.id's column default --
    # LinkedIdentity is added in the same call with no intervening flush.
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


def _update_usage_event_outcome(usage_event_id: int, turn_id: uuid.UUID | None, outcome: str) -> None:
    """
    Updates a usage_events row's turn_id/outcome after run_agent resolves (success,
    hit_iteration_cap, or a handled failure). The row itself is inserted with a
    placeholder outcome before run_agent is even called (see ask() below), matching
    design doc SS7.2's "counts against the cap even on a mid-run crash" rationale -- a
    crash this update never runs for simply leaves that placeholder in place, still
    correctly counted toward the cap (mislabeled, not miscounted).
    """
    session = get_session()
    try:
        usage_event = session.get(UsageEvent, usage_event_id)
        if usage_event is not None:
            usage_event.turn_id = turn_id
            usage_event.outcome = outcome
            session.commit()
    finally:
        session.close()


@app.post("/v1/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    account: Account = Depends(get_current_account),
) -> AskResponse:
    gate_now = datetime.now(timezone.utc)
    session = get_session()
    try:
        decision = evaluate_ask_gate(session, account, gate_now)
        if not decision.allowed:
            session.add(
                UsageEvent(
                    account_id=account.id,
                    occurred_at=gate_now,
                    turn_id=None,
                    outcome=decision.reject_outcome,
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
            try:
                raw_key = decrypt_byo_key(byo_key.encrypted_key)
            except Exception as e:  # noqa: BLE001 -- never leak key material via a raised exception
                # A decrypt failure here almost always means BYO_KEY_ENCRYPTION_KEY was
                # rotated out from under this ciphertext (backend/SECURITY.md's Fernet
                # rotation runbook is a hard cutover -- old ciphertext is unrecoverable by
                # design). Deactivate the row so this doesn't repeat on every subsequent
                # request: the next /v1/ask for this install falls through
                # gating.evaluate_ask_gate's byo_key.is_active check straight to the
                # paid/free tier instead of hitting this same dead end again.
                deactivate_session = get_session()
                try:
                    row = deactivate_session.get(ByoKey, byo_key.id)
                    if row is not None:
                        row.is_active = False
                        deactivate_session.commit()
                finally:
                    deactivate_session.close()
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Stored BYO key could not be decrypted and has been deactivated; "
                        "please re-register it."
                    ),
                ) from e
            byo_client = anthropic.Anthropic(api_key=raw_key)
            byo_key.last_used_at = gate_now

        # Placeholder row, inserted before run_agent runs -- see _update_usage_event_outcome.
        usage_event = UsageEvent(
            account_id=account.id, occurred_at=gate_now, turn_id=None, outcome="answered"
        )
        session.add(usage_event)
        session.commit()
        usage_event_id = usage_event.id
    finally:
        session.close()

    conversation_uuid: uuid.UUID | None = None
    prior_messages: list[dict] | None = None
    if request.conversation_id is not None:
        try:
            conversation_uuid = uuid.UUID(request.conversation_id)
        except ValueError as e:
            # Same "fail clearly, don't silently proceed" contract as csv_context_id below.
            raise HTTPException(status_code=404, detail="conversation not found") from e
        session = get_session()
        try:
            _get_owned_conversation(session, conversation_uuid, account.id)
            recent_turns = (
                session.query(Turn)
                .filter(Turn.conversation_id == conversation_uuid)
                .order_by(Turn.created_at.desc())
                .limit(MAX_PRIOR_TURNS)
                .all()
            )
            recent_turns.reverse()  # oldest to newest, for seeding order
            prior_messages = build_prior_messages(recent_turns)
        finally:
            session.close()

    csv_context_uuid: uuid.UUID | None = None
    csv_token = None
    if request.csv_context_id is not None:
        try:
            csv_context_uuid = uuid.UUID(request.csv_context_id)
        except ValueError as e:
            # Malformed by construction can't match any row's PK -- same "fail clearly, don't
            # silently proceed with no active CSV" contract as a well-formed but nonexistent id.
            raise HTTPException(status_code=404, detail="csv context not found") from e
        session = get_session()
        try:
            csv_row = _load_confirmed_csv_statement(session, csv_context_uuid, account.id)
            df = statement_from_records(csv_row.statement_data, csv_row.statement_attrs)
        finally:
            session.close()
        csv_token = csv_session.set_active_csv_with_token(df)

    try:
        try:
            # client is only passed when a BYO key applies -- omitting the kwarg entirely
            # otherwise (rather than passing client=None) keeps run_agent's own default
            # (anthropic.Anthropic() against the master ANTHROPIC_API_KEY) in charge of
            # client construction for the free/paid tiers, unchanged from before this
            # session.
            run_agent_kwargs = {"prior_messages": prior_messages}
            if byo_client is not None:
                run_agent_kwargs["client"] = byo_client
            result = run_agent(request.question, **run_agent_kwargs)
        except anthropic.AuthenticationError as e:
            # Caught ahead of the broader anthropic.APIError handler below (it's a
            # subclass -- order matters). A key-rejection error is the one failure mode
            # this session's BYO-key work makes concretely dangerous: the request that
            # failed just carried either this caller's own BYO key or this server's
            # master key, so str(e)/e.args must never reach the HTTP response even
            # though, empirically, the Anthropic SDK's own message here is built from the
            # API's JSON error body, not an echo of the request -- a defensive posture
            # against a future SDK/proxy/network-layer change, not a reaction to an
            # observed leak. The real exception (with traceback) is still logged
            # server-side, so debuggability isn't lost, only what reaches the caller.
            _update_usage_event_outcome(usage_event_id, None, "error")
            logger.exception("Anthropic authentication error in /v1/ask (tier=%s)", decision.tier)
            if byo_client is not None:
                detail = "Your Anthropic API key was rejected. Please re-register a valid key."
            else:
                detail = "Anthropic API authentication failed."
            raise HTTPException(status_code=502, detail=detail) from e
        except anthropic.APIError as e:
            # str(e) is deliberately kept out of the response (session 10's security
            # hardening pass) -- the full exception, including any embedded request/
            # response detail, is already captured server-side by logger.exception below.
            _update_usage_event_outcome(usage_event_id, None, "error")
            logger.exception("Anthropic API error in /v1/ask")
            raise HTTPException(status_code=502, detail="Anthropic API error.") from e
        except Exception as e:  # noqa: BLE001 -- surfaced as a clean 500, not a bare 500 traceback
            # str(e) used to reach this response; session 10's security hardening pass
            # closed that gap for every handler in this route, not just the
            # authentication-specific one -- a tool-execution bug, a pandas/EDGAR error,
            # etc. inside run_agent could in principle embed request detail, and the full
            # exception is already captured server-side by logger.exception below.
            _update_usage_event_outcome(usage_event_id, None, "error")
            logger.exception("run_agent failed unexpectedly in /v1/ask")
            raise HTTPException(status_code=500, detail="run_agent failed unexpectedly.") from e
    finally:
        if csv_token is not None:
            csv_session.reset_active_csv(csv_token)

    turn_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        if conversation_uuid is not None:
            # Ownership was already verified above; re-fetch in this block's own session
            # rather than reusing the earlier (closed) session's now-detached instance.
            conversation = session.get(Conversation, conversation_uuid)
            conversation.last_turn_at = now
        else:
            conversation = Conversation(
                account_id=account.id,
                title=request.question[:200],
                csv_context_id=csv_context_uuid,
                last_turn_at=now,
            )
            session.add(conversation)
        session.flush()  # populate conversation.id before the Turn below references it

        session.add(
            Turn(
                id=turn_id,
                conversation_id=conversation.id,
                question=result["question"],
                final_answer=result["final_answer"],
                hit_iteration_cap=result["hit_iteration_cap"],
                iterations_used=result["iterations_used"],
                stop_reason=result["stop_reason"],
                figure_check=result["figure_check"],
                tool_calls=result["tool_calls"],
                model=DEFAULT_MODEL,
            )
        )
        usage_event = session.get(UsageEvent, usage_event_id)
        usage_event.turn_id = turn_id
        usage_event.outcome = "hit_iteration_cap" if result["hit_iteration_cap"] else "answered"
        session.commit()
        conversation_id = conversation.id
    finally:
        session.close()

    tool_calls_summary = [
        {"tool_name": call["tool_name"], "is_error": call["is_error"]} for call in result["tool_calls"]
    ]

    return AskResponse(
        conversation_id=str(conversation_id),
        turn_id=str(turn_id),
        final_answer=result["final_answer"],
        hit_iteration_cap=result["hit_iteration_cap"],
        figure_check=result["figure_check"],
        citations=[],  # deferred -- provenance-derived citations are new parsing logic, not minimal wiring
        tool_calls_summary=tool_calls_summary,
    )


@app.post("/v1/csv/parse", response_model=CsvParseResponse)
def csv_parse(
    request: CsvParseRequest,
    account: Account = Depends(get_current_account),
) -> CsvParseResponse:
    raw, error = rows_to_raw_csv(request.rows, request.filename)
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
    session = get_session()
    try:
        row = _get_owned_csv_statement(session, csv_context_id, account.id)
        raw = raw_csv_from_json(row.raw_columns, row.filename, row.uploaded_at)

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

        row.proposed_mapping = [
            {"csv_column": c.csv_column, "proposed_role": c.proposed_role, "rationale": c.rationale}
            for c in result.columns
        ]
        session.commit()

        return ProposeMappingResponse(
            proposal=[
                MappingProposalEntry(
                    csv_column=c.csv_column, proposed_role=c.proposed_role, rationale=c.rationale
                )
                for c in result.columns
            ],
            note=result.note,
        )
    finally:
        session.close()


@app.post("/v1/csv/{csv_context_id}/confirm", response_model=ConfirmResponse)
def confirm_mapping(
    csv_context_id: uuid.UUID,
    request: ConfirmRequest,
    account: Account = Depends(get_current_account),
) -> ConfirmResponse:
    session = get_session()
    try:
        row = _get_owned_csv_statement(session, csv_context_id, account.id)
        raw = raw_csv_from_json(row.raw_columns, row.filename, row.uploaded_at)

        serial_reason = find_period_serial_number_value(raw, request.mapping)
        if serial_reason is not None:
            return ConfirmResponse(confirmed=False, errors=[serial_reason], warnings=[])

        errors = validate_mapping(raw, request.mapping)
        if errors:
            return ConfirmResponse(confirmed=False, errors=errors, warnings=[])

        df, errors, warnings = normalize(raw, request.mapping, request.entity_name)
        if errors:
            return ConfirmResponse(confirmed=False, errors=errors, warnings=warnings)

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
    verify = (
        oauth_providers.verify_google_token
        if request.provider == "google"
        else oauth_providers.verify_microsoft_token
    )
    try:
        identity = verify(request.oauth_token)
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
    session = get_session()
    try:
        decision = evaluate_ask_gate(session, account, datetime.now(timezone.utc))
    finally:
        session.close()

    response = UsageResponse(
        account_id=str(account.id),
        tier=decision.tier,
        byo_key_required=decision.prompt_byo_key,
    )
    if decision.tier == "free":
        response.questions_today = decision.questions_used
        response.daily_cap = decision.cap
    elif decision.tier == "paid":
        response.questions_this_period = decision.questions_used
        response.monthly_cap = decision.cap
        response.period_ends_at = decision.resets_at
    return response


@app.post("/v1/byo-key", response_model=ByoKeyResponse)
def register_byo_key(
    request: ByoKeyRequest, account: Account = Depends(get_current_account)
) -> ByoKeyResponse:
    """
    Registers (or rotates) the caller's own Anthropic API key. A new key always replaces
    any currently active one for this account -- soft-deactivating the old byo_keys row
    rather than rejecting the request -- matching is_active's stated audit-trail purpose
    (design doc SS4) and giving a caller a one-call way to rotate a leaked/expired key,
    since there's no separate removal endpoint (out of scope for Phase B session 8; see
    NOTES.md).
    """
    if not is_valid_byo_key_format(request.api_key):
        raise HTTPException(
            status_code=422, detail="That doesn't look like a valid Anthropic API key."
        )
    encrypted = encrypt_byo_key(request.api_key)

    session = get_session()
    try:
        session.execute(
            update(ByoKey)
            .where(ByoKey.account_id == account.id, ByoKey.is_active.is_(True))
            .values(is_active=False)
        )
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


_BILLING_SUCCESS_HTML = (
    "<!doctype html><html><head><title>Subscription active</title></head>"
    "<body><h1>Subscription active</h1>"
    "<p>You can close this tab and return to your Google Sheet.</p></body></html>"
)
_BILLING_CANCEL_HTML = (
    "<!doctype html><html><head><title>Checkout canceled</title></head>"
    "<body><h1>Checkout canceled</h1>"
    "<p>No changes were made. You can close this tab and return to your Google Sheet.</p>"
    "</body></html>"
)


@app.get("/v1/billing/success", response_class=HTMLResponse)
def billing_success() -> HTMLResponse:
    return HTMLResponse(_BILLING_SUCCESS_HTML)


@app.get("/v1/billing/cancel", response_class=HTMLResponse)
def billing_cancel() -> HTMLResponse:
    return HTMLResponse(_BILLING_CANCEL_HTML)


@app.post("/v1/billing/checkout-session", response_model=CheckoutSessionResponse)
def create_checkout_session_route(
    request: Request,
    account: Account = Depends(get_current_account),
) -> CheckoutSessionResponse:
    price_id = os.environ.get("STRIPE_PRICE_ID")
    if not price_id:
        raise HTTPException(status_code=500, detail="STRIPE_PRICE_ID is not configured")

    success_url = str(request.base_url) + "v1/billing/success"
    cancel_url = str(request.base_url) + "v1/billing/cancel"
    checkout_url = billing.create_checkout_session(
        account.id, account.primary_email, price_id, success_url, cancel_url
    )
    return CheckoutSessionResponse(checkout_url=checkout_url)


@app.post("/v1/billing/webhook")
async def stripe_webhook(request: Request) -> dict:
    """
    One of the few endpoints (alongside /v1/health and /v1/auth/exchange) that does NOT
    require an Authorization: Bearer session token -- Stripe calls this directly and cannot
    carry one (design doc SS1's explicitly-stated exception). Deliberately async def, the
    one exception to this file's otherwise
    consistent sync def (threadpool-dispatched) route convention: it needs await
    request.body() to read the raw, unparsed body before FastAPI's normal body-parsing
    machinery touches it -- re-serializing a parsed body breaks Stripe's signature check.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET is not configured")

    try:
        event = billing.verify_webhook_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.SignatureVerificationError):
        # Rejected before any DB access at all -- an unverified webhook is a real attack
        # surface (design doc SS7.4).
        raise HTTPException(status_code=400, detail="invalid Stripe webhook signature")

    session = get_session()
    try:
        # Insert-first idempotency guard: the stripe_webhook_events PK is the source of
        # truth under concurrent/duplicate delivery, not a check-then-insert (which has a
        # race window). flush() (not commit()) sends the INSERT now, still inside this same
        # transaction as the mutation below -- so if handle_stripe_event raises, rolling
        # back undoes BOTH the marker and any partial mutation together, and a Stripe retry
        # correctly reprocesses from scratch rather than silently no-oping forever against a
        # marker row that was committed but never actually followed by its mutation.
        session.add(StripeWebhookEvent(stripe_event_id=event["id"], event_type=event["type"]))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return {"received": True}
        billing.handle_stripe_event(session, event)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return {"received": True}
