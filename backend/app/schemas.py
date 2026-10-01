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


class UsageResponse(BaseModel):
    """
    Shape per the monetization amendment's SS7.2 (replaces the original free_window_ends_at
    shape) -- only the fields for the caller's actual tier are populated: free ->
    questions_today/daily_cap; paid -> questions_this_period/monthly_cap/period_ends_at;
    byo_key -> none of those.
    """

    account_id: str
    tier: Literal["byo_key", "paid", "free"]
    questions_today: int | None = None
    daily_cap: int | None = None
    questions_this_period: int | None = None
    monthly_cap: int | None = None
    period_ends_at: datetime | None = None
    byo_key_required: bool


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


class ByoKeyRequest(BaseModel):
    api_key: str


class ByoKeyResponse(BaseModel):
    # Deliberately no key material or byo_keys.id echoed back -- the caller already has
    # the plaintext key it just sent, and the row's id has no use on the client side.
    registered: bool


class AuthExchangeRequest(BaseModel):
    provider: Literal["google", "microsoft"]
    # Microsoft: a pre-exchanged OAuth access token -- Azure's "Single-page application"
    # platform type is a genuine no-secret public client, so the extension completes the
    # code-for-token exchange itself and sends the result straight here.
    oauth_token: str | None = None
    # Google: the raw authorization code + PKCE verifier + redirect_uri instead of a
    # pre-exchanged token. Google's OAuth client types compatible with
    # launchWebAuthFlow's https redirect requirement ("Web application") are documented
    # by Google itself as confidential clients -- PKCE does not substitute for
    # client_secret at their token endpoint, unlike Microsoft's platform type above. So
    # this backend performs Google's code-for-token exchange server-side (Phase D
    # session 2 amendment; see app/oauth_providers.py's exchange_google_code_for_token).
    code: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None


class AuthExchangeResponse(BaseModel):
    # session_token is the plaintext opaque bearer token, returned exactly once at exchange
    # time and never re-derivable server-side afterward -- only its SHA-256 hash is
    # persisted, in sessions.token_hash. Same "shown once" discipline as ByoKeyResponse.
    session_token: str
    account_id: str
    expires_at: datetime


class GoogleDataTokenRequest(BaseModel):
    # All optional at the schema level and checked by hand in the route, same as
    # AuthExchangeRequest, so a missing field gets EXTENSION_INTEGRATION.md SS1a's named 422
    # detail rather than FastAPI's generic validation array.
    code: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None


class GoogleDataTokenResponse(BaseModel):
    # A short-lived Google access token for the extension's own Sheets API calls, plus the
    # data account's email (its login_hint for later silent re-auth). Never stored or
    # logged by this backend -- see the route's docstring.
    access_token: str
    expires_in: int
    scope: str
    email: str


class LogoutResponse(BaseModel):
    revoked: bool


class RevokeAllSessionsResponse(BaseModel):
    revoked: bool
