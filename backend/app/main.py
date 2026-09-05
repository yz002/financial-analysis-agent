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

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from fastapi import FastAPI, Header, HTTPException, Response
from sqlalchemy import text

from db.base import get_session
from db.models import Conversation, Install, Turn

# src/agent/agent.py lives one level above backend/ (see repo layout in
# CLAUDE.md), but this module is normally run with backend/ as the working
# directory (see the run instructions above), so `src` isn't importable
# without adding the repo root to sys.path -- mirrors the same bootstrap
# src/app/main.py already uses for the Streamlit app.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.agent import DEFAULT_MODEL, run_agent  # noqa: E402 -- see sys.path note above

from .schemas import (
    AskRequest,
    AskResponse,
    ConfirmRequest,
    ConfirmResponse,
    CsvParseRequest,
    CsvParseResponse,
    HealthResponse,
    InstallRequest,
    InstallResponse,
    MappingProposalEntry,
    ProposeMappingResponse,
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
    return HealthResponse(status="ok", db=db_status)


def _get_or_create_install(session, install_id: uuid.UUID) -> Install:
    """
    TEMPORARY shim for Phase B session 3. Real identity issuance (POST
    /v1/install actually persisting a row, plus the free-tier/rate-limiting
    logic in the design doc's SS2 that assumes a real install already exists)
    is session 7's job. Until then, any X-Install-Id a caller presents is
    accepted at face value and a minimal row is auto-created for it if one
    doesn't exist yet, purely so conversations.install_id's FK can be
    satisfied. Session 7 should replace this with a lookup-or-404.
    """
    install = session.get(Install, install_id)
    now = datetime.now(timezone.utc)
    if install is None:
        install = Install(
            install_id=install_id,
            identity_type="uuid",
            identity_value=str(install_id),
            free_window_started_at=now,
            last_seen_at=now,
        )
        session.add(install)
    else:
        install.last_seen_at = now
    return install


@app.post("/v1/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    x_install_id: uuid.UUID = Header(alias="X-Install-Id"),
) -> AskResponse:
    session = get_session()
    try:
        _get_or_create_install(session, x_install_id)
        session.commit()
    finally:
        session.close()

    # request.conversation_id is accepted for shape-compatibility with the
    # design doc's endpoint contract but ignored this session: continuing an
    # existing conversation and seeding its history into run_agent needs the
    # prior_messages plumbing that's session 6's job. Every call here starts
    # a new conversation.
    try:
        result = run_agent(request.question)
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}") from e
    except Exception as e:  # noqa: BLE001 -- surfaced as a clean 500, not a bare 500 traceback
        raise HTTPException(status_code=500, detail=f"run_agent failed unexpectedly: {e}") from e

    turn_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        conversation = Conversation(
            install_id=x_install_id,
            title=request.question[:200],
            csv_context_id=None,
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
def csv_parse(request: CsvParseRequest) -> CsvParseResponse:
    if not request.rows:
        return CsvParseResponse(
            csv_context_id=None,
            columns=[],
            sample_rows=[],
            parse_error="no rows provided",
        )
    columns = request.rows[0]
    sample_rows = request.rows[1:6]
    return CsvParseResponse(
        csv_context_id=str(uuid.uuid4()),
        columns=columns,
        sample_rows=sample_rows,
        parse_error=None,
    )


@app.post("/v1/csv/{csv_context_id}/propose-mapping", response_model=ProposeMappingResponse)
def propose_mapping(csv_context_id: str) -> ProposeMappingResponse:
    return ProposeMappingResponse(
        proposal=[
            MappingProposalEntry(
                csv_column="(stub)",
                proposed_role="(stub)",
                rationale="(stub) propose_mapping is not wired up yet.",
            )
        ],
        note=None,
    )


@app.post("/v1/csv/{csv_context_id}/confirm", response_model=ConfirmResponse)
def confirm_mapping(csv_context_id: str, request: ConfirmRequest) -> ConfirmResponse:
    return ConfirmResponse(
        confirmed=True,
        cadence=None,
        warnings=[],
        concepts_unavailable=[],
        errors=[],
    )


@app.post("/v1/install", response_model=InstallResponse)
def install(request: InstallRequest) -> InstallResponse:
    return InstallResponse(
        install_id=str(uuid.uuid4()),
        free_window_started_at=datetime.now(timezone.utc),
    )


@app.get("/v1/usage", response_model=UsageResponse)
def usage() -> UsageResponse:
    return UsageResponse(
        install_id=str(uuid.uuid4()),
        free_window_ends_at=datetime.now(timezone.utc),
        questions_today=0,
        daily_cap=20,
        byo_key_required=False,
    )
