"""
Structural CSV ingestion and LLM-proposed column mapping for small-business CSV uploads.

This module does two, deliberately separate, things:

1. `parse_csv` -- structural validation only (does this parse as a table at all: valid
   encoding, a header row, at least one data row). It never interprets what any column
   *means* -- that's the mapping step below and, ultimately, the human confirming it.
2. `propose_mapping` -- a single bounded, one-shot Claude call that proposes which CSV
   column corresponds to which of this project's standard concepts (revenue, net_income,
   etc.) or the period/date role, with a short rationale per column. Per this project's
   core design principle (see CLAUDE.md), the model only ever *proposes* a mapping here --
   it never touches, transforms, or computes a value. The proposal is not authoritative;
   src/analysis/csv_statement.py's validate_mapping/normalize are what a human's confirmed
   mapping actually runs through, and a human can override every proposed role before
   anything is normalized.

Both steps report refusal by returning an explicit reason (never raising for an expected,
data-shaped problem, and never silently producing partial/guessed output) -- the same
"refuse rather than guess" discipline src/analysis/statements.py uses for Q4 derivation.
Genuine Anthropic API errors (auth, connection, rate limit) are deliberately *not* caught
here and propagate to the caller, exactly like src/agent/agent.py's run_agent -- the UI
layer is responsible for translating those into a plain-English message, the same way
src/app/main.py's _run_agent_or_error already does for run_agent.
"""

import csv
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import anthropic
import pandas as pd

DEFAULT_MODEL = "claude-opus-5"

# Bounds how much of the CSV is ever sent to the model -- mirrors src/agent/tools.py's own
# MAX_PERIODS/MAX_DAILY_PRICE_ROWS payload-size bounding. The model only needs enough sample
# rows to judge what a column *is* (a date? a dollar figure? which one?), not the whole file.
MAX_SAMPLE_ROWS = 5

# The mapping roles a CSV column can be proposed/confirmed as: the 13 concepts
# src/analysis/statements.py's get_statement() tracks, plus the one special "this is the
# period/date column" role. Not imported from statements.py (out of scope this session) --
# imported from concepts.py, the actual source of truth for concept names, by
# src/analysis/csv_statement.py; duplicated here only as the small, stable set of strings the
# LLM prompt needs to name, not as a second source of truth for what the 13 concepts are.
PERIOD_ROLE = "period_end"
UNMAPPED_ROLE = "unmapped"


@dataclass
class RawCsv:
    """A structurally-valid, uninterpreted CSV upload. `df`'s columns/values are exactly what
    pandas parsed -- no concept mapping, no numeric cleanup, no date parsing has happened yet."""

    df: pd.DataFrame
    filename: str
    uploaded_at: datetime


@dataclass
class ColumnProposal:
    csv_column: str
    proposed_role: str  # one of the 13 concept names, PERIOD_ROLE, or UNMAPPED_ROLE
    rationale: str


@dataclass
class MappingProposal:
    columns: list[ColumnProposal]
    note: str | None = field(default=None)  # set when the LLM call didn't produce usable JSON


def parse_csv(
    file, filename: str, uploaded_at: datetime | None = None
) -> tuple[RawCsv | None, str | None]:
    """
    Structurally parse `file` (a path, file-like object, or raw bytes) as a CSV. Returns
    (RawCsv, None) on success, or (None, reason) if it doesn't even parse as a table --
    never a partially-parsed result. `uploaded_at` defaults to now(); passing it explicitly
    is for deterministic tests.

    Refuses (reason named) for: undecodable bytes (bad encoding), no header row detected, a
    header row but zero data rows, or any other pandas parse failure (bad delimiter,
    malformed quoting, etc.).
    """
    uploaded_at = uploaded_at or datetime.now()

    if isinstance(file, (bytes, bytearray)):
        raw_bytes = bytes(file)
    elif hasattr(file, "read"):
        raw_bytes = file.read()
        if isinstance(raw_bytes, str):
            raw_bytes = raw_bytes.encode("utf-8")
    else:
        try:
            with open(file, "rb") as f:
                raw_bytes = f.read()
        except OSError as e:
            return None, f"Could not read {filename!r}: {e}"

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, (
            f"{filename!r} could not be decoded as UTF-8 text. Re-save the file with UTF-8 "
            "encoding and try again."
        )

    try:
        header_row = next(csv.reader(io.StringIO(text)))
    except StopIteration:
        header_row = []
    header_counts = Counter(header_row)
    dupes = sorted(h for h, count in header_counts.items() if count > 1)
    if dupes:
        return None, (
            f"{filename!r} has duplicate column headers ({', '.join(dupes)}) -- each column "
            "needs a distinct header so it can be mapped unambiguously."
        )

    try:
        df = pd.read_csv(io.StringIO(text))
    except pd.errors.EmptyDataError:
        return None, f"{filename!r} is empty -- no header row or data found."
    except pd.errors.ParserError as e:
        return None, f"{filename!r} could not be parsed as a CSV: {e}"

    if len(df.columns) == 0:
        return None, f"{filename!r} has no columns -- no header row was found."
    if len(df) == 0:
        return None, f"{filename!r} has a header row but no data rows."

    return RawCsv(df=df, filename=filename, uploaded_at=uploaded_at), None


_MAPPING_SYSTEM_PROMPT = """You propose a column mapping for a small business's uploaded \
financial CSV. You are given the CSV's column headers and a few sample rows. For each column, \
propose which of these roles it plays:

{roles}

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{"mappings": [{{"csv_column": "<header>", "proposed_role": "<role>", "rationale": "<one short \
sentence>"}}, ...]}} -- one entry per CSV column, in the order given. Use "unmapped" for a \
column that doesn't correspond to any listed role (e.g. an internal ID, a comment column). Use \
"period_end" for whichever single column identifies the reporting period/date. You are only \
proposing which column is which -- never state, compute, or alter any value."""


def _mapping_prompt(raw: RawCsv, roles: list[str]) -> str:
    sample = raw.df.head(MAX_SAMPLE_ROWS)
    lines = [f"Columns: {', '.join(raw.df.columns)}", "", "Sample rows:"]
    for _, row in sample.iterrows():
        lines.append(json.dumps({col: str(row[col]) for col in raw.df.columns}))
    return "\n".join(lines)


def propose_mapping(
    raw: RawCsv,
    roles: list[str],
    client: anthropic.Anthropic | None = None,
    model: str = DEFAULT_MODEL,
) -> MappingProposal:
    """
    Propose a column -> role mapping for `raw` via a single, bounded Claude call (headers plus
    up to MAX_SAMPLE_ROWS sample rows -- never the whole file). `roles` is the full list of
    valid role strings (the 13 concept names plus "period_end"); "unmapped" is always
    implicitly valid and doesn't need to be included.

    This is not authoritative -- it's a starting point a human confirms or overrides (see
    src/analysis/csv_statement.py's validate_mapping/normalize). If the model's response isn't
    valid JSON, or names a role outside `roles`/"unmapped"/"period_end", every affected column
    falls back to "unmapped" and the returned MappingProposal.note explains why -- ingestion
    still proceeds to manual confirmation rather than failing outright. A genuine API error
    (auth, connection, rate limit) is not caught here and propagates to the caller.
    """
    client = client or anthropic.Anthropic()
    valid_roles = set(roles) | {PERIOD_ROLE, UNMAPPED_ROLE}

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_MAPPING_SYSTEM_PROMPT.format(roles="\n".join(f"- {r}" for r in roles)),
        messages=[{"role": "user", "content": _mapping_prompt(raw, roles)}],
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()

    try:
        parsed = json.loads(text)
        raw_mappings = parsed["mappings"]
        if not isinstance(raw_mappings, list):
            raise ValueError("mappings is not a list")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return MappingProposal(
            columns=[
                ColumnProposal(csv_column=c, proposed_role=UNMAPPED_ROLE, rationale="")
                for c in raw.df.columns
            ],
            note=(
                "The mapping proposal step did not return usable output; every column defaults "
                "to unmapped. Map each column manually below."
            ),
        )

    by_column = {}
    for entry in raw_mappings:
        if not isinstance(entry, dict) or "csv_column" not in entry:
            continue
        role = entry.get("proposed_role", UNMAPPED_ROLE)
        if role not in valid_roles:
            role = UNMAPPED_ROLE
        by_column[entry["csv_column"]] = ColumnProposal(
            csv_column=entry["csv_column"],
            proposed_role=role,
            rationale=str(entry.get("rationale", "")),
        )

    columns = [
        by_column.get(c, ColumnProposal(csv_column=c, proposed_role=UNMAPPED_ROLE, rationale=""))
        for c in raw.df.columns
    ]
    return MappingProposal(columns=columns, note=None)
