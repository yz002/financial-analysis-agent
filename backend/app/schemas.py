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
    # Render sets RENDER_GIT_COMMIT automatically; echoing it here lets a
    # deploy be confirmed against a specific pushed commit rather than just
    # "some build is answering" (added for Phase B session 3's Render
    # timeout validation). None when running outside Render (e.g. locally).
    commit: str | None = None


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


class UsageResponse(BaseModel):
    """
    Shape per the monetization amendment's SS7.2 (replaces the original free_window_ends_at
    shape) -- only the fields for the caller's actual tier are populated: free ->
    questions_today/daily_cap; paid -> questions_this_period/monthly_cap/period_ends_at;
    byo_key -> none of those.
    """

    install_id: str
    tier: Literal["byo_key", "paid", "free"]
    questions_today: int | None = None
    daily_cap: int | None = None
    questions_this_period: int | None = None
    monthly_cap: int | None = None
    period_ends_at: datetime | None = None
    byo_key_required: bool


class CheckoutSessionResponse(BaseModel):
    checkout_url: str
