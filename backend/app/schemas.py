"""
Pydantic request/response models for the Sheets Add-on backend's HTTP API,
matching the approved architecture design doc's SS1 field-for-field. This
session (Phase B session 2) only needs the *shape* to be right -- every route
in main.py returns stubbed data validated against these models; real business
logic (run_agent wiring, CSV pipeline, identity/rate-limiting) lands in later
sessions per the design doc's SS6 build plan.

ConfirmResponse covers both of SS1's documented outcomes (success and
failure) as one schema with optional/defaulted fields, since FastAPI's
response_model wants one consistent shape per route.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    db: str


class AskRequest(BaseModel):
    question: str
    csv_context_id: str | None = None
    conversation_id: str | None = None


class AskResponse(BaseModel):
    conversation_id: str
    turn_id: str
    final_answer: str
    hit_iteration_cap: bool
    figure_check: dict
    citations: list
    tool_calls_summary: list


class CsvParseRequest(BaseModel):
    rows: list[list[str]]
    filename: str


class CsvParseResponse(BaseModel):
    csv_context_id: str | None
    columns: list[str]
    sample_rows: list[list[str]]
    parse_error: str | None = None


class MappingProposalEntry(BaseModel):
    csv_column: str
    proposed_role: str
    rationale: str


class ProposeMappingResponse(BaseModel):
    proposal: list[MappingProposalEntry]
    note: str | None = None


class ConfirmRequest(BaseModel):
    mapping: dict[str, str]
    entity_name: str


class ConfirmResponse(BaseModel):
    confirmed: bool
    cadence: str | None = None
    warnings: list[str] = []
    concepts_unavailable: list[str] = []
    errors: list[str] = []


class InstallRequest(BaseModel):
    identity_type: Literal["google_email", "uuid"]
    identity_value: str


class InstallResponse(BaseModel):
    install_id: str
    free_window_started_at: datetime


class UsageResponse(BaseModel):
    install_id: str
    free_window_ends_at: datetime
    questions_today: int
    daily_cap: int
    byo_key_required: bool
