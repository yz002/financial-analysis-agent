"""
FastAPI skeleton for the Sheets Add-on backend (Phase B session 2). Stands up
the route + Pydantic-model shape for every endpoint the architecture design
doc's SS1 specifies, all returning stubbed data -- no run_agent call, no CSV
pipeline, no identity/rate-limiting, no DB reads/writes beyond /v1/health.
Later sessions (SS6 steps 3-8) fill in real logic behind this same contract.

Run locally from inside backend/ (matching db/smoke_test.py's cwd convention):

    backend/.venv/Scripts/uvicorn.exe app.main:app --reload

with DATABASE_URL set (backend/.env) for /v1/health to reach Postgres.
"""

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Response
from sqlalchemy import text

from db.base import get_session

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


@app.post("/v1/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    conversation_id = request.conversation_id or str(uuid.uuid4())
    return AskResponse(
        conversation_id=conversation_id,
        turn_id=str(uuid.uuid4()),
        final_answer="(stub) this endpoint does not call run_agent yet.",
        hit_iteration_cap=False,
        figure_check={},
        citations=[],
        tool_calls_summary=[],
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
