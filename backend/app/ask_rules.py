"""
Pure decision rules for POST /v1/ask (EXTENSION_INTEGRATION.md SS6, amended Phase D session 5):
whether a stored statement is usable, which statement a conversation turn may use, and what
state a replayed request_id is in. No DB access and no I/O here -- app/main.py loads the values
and acts on the decisions, so every rule is unit-tested without a database
(backend/tests_unit/).
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

# How long a request_id may stay 'in_progress' before /v1/ask reports it as lost (a deploy or
# restart killed the run; Render kills in-flight requests on deploy). Must stay comfortably
# above src/agent/agent.py's RUN_BUDGET_SECONDS (45 min) -- see the comment there for the
# overshoot that sits between the two.
STALE_IN_PROGRESS_AFTER = timedelta(minutes=60)

IN_PROGRESS = "in_progress"
_DONE_OUTCOMES = ("answered", "hit_iteration_cap")
_FAILED_OUTCOME = "error"

# The scales src/analysis/csv_statement.py's SCALE_FACTORS accepts at confirm time. Listed here
# rather than imported so this module stays importable without the repo root on sys.path;
# tests_unit/test_ask_rules.py asserts the two stay identical.
KNOWN_SCALES = frozenset({"ones", "thousands", "millions", "billions"})


def ask_error(code: str) -> dict:
    """The structured `detail` every session-5 /v1/ask error uses: {"error": "<code>"}."""
    return {"error": code}


# --- statement usability -------------------------------------------------------------------


def statement_problem(status, statement_data, statement_attrs) -> str | None:
    """Why a confirmed statement can't be used by /v1/ask, or None if it can. A statement must
    carry its stored data and, in its attrs, a business name, a known scale and a currency entry
    (null means "not specified" and is fine; a missing key means the statement predates units).
    Statements confirmed before scale/currency existed fail here and are rejected with
    statement_needs_reconfirm -- units are never assumed, so they're never read as "ones"."""
    if status != "confirmed":
        return "not confirmed"
    if not statement_data:
        return "no stored statement data"
    if not isinstance(statement_attrs, dict):
        return "no stored statement attributes"
    if not statement_attrs.get("entity_name"):
        return "no business name"
    source = statement_attrs.get("csv_source")
    if not isinstance(source, dict):
        return "no source attributes"
    if source.get("scale") not in KNOWN_SCALES:
        return "no known scale"
    if "currency" not in source:
        return "no currency entry"
    return None


# --- conversation <-> statement binding ----------------------------------------------------

USE = "use"
UNBOUND = "unbound"
MISMATCH = "statement_mismatch"
NEEDS_RECONFIRM = "statement_needs_reconfirm"


@dataclass(frozen=True)
class BindingDecision:
    kind: str  # USE | UNBOUND | MISMATCH | NEEDS_RECONFIRM
    statement_id: uuid.UUID | None = None


CSV_TOOLS = ("get_csv_statement", "get_csv_ratios")
# The key each CSV tool's successful result carries (src/agent/tools.py get_csv_statement /
# get_csv_ratios); an error result carries "error_type" instead.
_CSV_DATA_KEYS = {"get_csv_statement": "periods", "get_csv_ratios": "ratios"}


def csv_call_returned_data(call) -> bool:
    """Whether one stored tool call (an entry of turns.tool_calls) is a CSV tool that actually
    returned statement data. Not merely a CSV tool *call*: in a ticker-only conversation the
    model can call get_csv_statement and get back data_unavailable ("no CSV has been
    confirmed"), which run_agent stores with is_error false. That result is
    {"business_name": null, "error_type": "data_unavailable", ...}, and a crashed tool is
    is_error true with error_type "source_error". tool_result is stored as a JSON *string*, so
    it's decoded here; anything that isn't a JSON object with the tool's data key and no
    error_type doesn't count."""
    if not isinstance(call, dict) or call.get("is_error"):
        return False
    data_key = _CSV_DATA_KEYS.get(call.get("tool_name"))
    if data_key is None:
        return False
    try:
        payload = json.loads(call.get("tool_result") or "")
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and "error_type" not in payload and data_key in payload


def turns_returned_statement_data(tool_calls_per_turn) -> bool:
    """Whether any turn's tool calls include a CSV tool that returned statement data -- i.e.
    the conversation was answering from a statement. `tool_calls_per_turn` is an iterable of
    turns.tool_calls values (lists of call dicts, or None)."""
    return any(
        csv_call_returned_data(call)
        for tool_calls in tool_calls_per_turn
        for call in (tool_calls or [])
    )


def binding_decision(
    bound_csv_context_id: uuid.UUID | None,
    csv_context_id: uuid.UUID | None,
    requested_csv_context_id: uuid.UUID | None,
    had_statement_data,
) -> BindingDecision:
    """Which statement a later turn of an existing conversation may use.

    `bound_csv_context_id` is the statement recorded at creation (no FK, never nulled; NULL for
    conversations created before migration 0005, and for unbound ticker-only conversations).
    `csv_context_id` is the FK column, set to NULL if that statement row is deleted.
    `had_statement_data` is a zero-argument callable, called only when both are NULL: whether
    any turn of the conversation -- all of them, not just the replayed ones -- got statement
    data back from a CSV tool (see turns_returned_statement_data). That's the pre-0005 way to
    tell "bound to a statement that's since gone" from "never bound". A CSV tool call that
    returned data_unavailable doesn't count, so a new ticker-only conversation where the model
    once looked for a statement stays usable.

    The statement is never switched silently: a different requested id is MISMATCH. A bound
    statement that's gone is NEEDS_RECONFIRM whatever was requested. Loading the statement
    (and checking ownership and usability) is the caller's job, through the account-scoped
    loader -- a decision of USE is not permission to read the row."""
    if bound_csv_context_id is not None:
        if csv_context_id is None:
            return BindingDecision(NEEDS_RECONFIRM)  # the bound row was deleted
        bound = bound_csv_context_id
    elif csv_context_id is not None:
        bound = csv_context_id  # created before 0005, statement still there
    elif had_statement_data():
        return BindingDecision(NEEDS_RECONFIRM)  # created before 0005, statement since gone
    else:
        bound = None

    if bound is None:
        if requested_csv_context_id is None:
            return BindingDecision(UNBOUND)
        return BindingDecision(MISMATCH)
    if requested_csv_context_id is None or requested_csv_context_id == bound:
        return BindingDecision(USE, bound)
    return BindingDecision(MISMATCH)


# --- request_id replay ---------------------------------------------------------------------

DONE = "done"
RUNNING = "answer_in_progress"
LOST = "answer_lost"
FAILED = "answer_failed"
REUSED = "request_id_reused"
UNEXPECTED = "unexpected"


def request_fingerprint(
    question: str, csv_context_id: str | None, conversation_id: str | None
) -> str:
    """SHA-256 of the canonical JSON of the whole request, so a request_id resent with a
    different question, statement or conversation is refused rather than answered with another
    request's stored result."""
    canonical = json.dumps(
        {"question": question, "csv_context_id": csv_context_id, "conversation_id": conversation_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_state(
    outcome: str,
    turn_id,
    occurred_at: datetime,
    stored_fingerprint: str | None,
    fingerprint: str,
    now: datetime,
) -> str:
    """The state of a usage_events row found by (account_id, request_id), from its actual
    columns -- never from an implicit combination:

      REUSED     the stored fingerprint differs from this request's (checked first)
      RUNNING    outcome 'in_progress', younger than STALE_IN_PROGRESS_AFTER
      LOST       outcome 'in_progress', older than that
      DONE       outcome 'answered'/'hit_iteration_cap' with a turn_id
      FAILED     outcome 'error'
      UNEXPECTED anything else -- an unknown outcome, a done outcome with no turn, or a rejected
                 (429) outcome carrying a request_id, which /v1/ask never writes. The caller
                 logs it (metadata only) and answers answer_failed: never a 500, never a
                 fresh run."""
    if stored_fingerprint != fingerprint:
        return REUSED
    if outcome == IN_PROGRESS:
        return LOST if now - occurred_at > STALE_IN_PROGRESS_AFTER else RUNNING
    if outcome in _DONE_OUTCOMES:
        return DONE if turn_id is not None else UNEXPECTED
    if outcome == _FAILED_OUTCOME:
        return FAILED
    return UNEXPECTED
