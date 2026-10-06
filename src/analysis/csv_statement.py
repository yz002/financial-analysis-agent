"""
Normalizes a human-confirmed CSV column mapping into a DataFrame matching
get_statement()'s exact output shape (src/analysis/statements.py), so ratios.py/trends.py/
forecast.py can operate on a small business's own uploaded financials with zero changes.

Deliberately a separate module from statements.py rather than branches inside it:
statements.py's derivation machinery (Q4 = FY-(Q1+Q2+Q3) synthesis, the YTD-chain quarter
recovery, total_liabilities's three-tier fallback, sparse-history/successor-registrant
detection) all exist to route around specific, confirmed *EDGAR* data-availability gaps.
None of that applies to a CSV: a small business's spreadsheet either states a period's value
or it doesn't, and there's no multi-vintage filing history to derive or cross-check against.
This module performs zero derivation -- every EDGAR-only column is stubbed to a fixed,
CSV-appropriate default (see normalize()'s docstring), never computed. Every ticker gets the
same fixed 13-concept column schema in get_statement() (a concept with no data still gets its
columns, filled NaN/None/False, so ratios.py never needs hasattr/in-columns guards); a CSV
upload gets the identical treatment for exactly the same reason -- a narrower CSV-specific
schema would force every downstream consumer to special-case "EDGAR statement vs. CSV
statement," defeating the point of normalizing to a shared shape.

Column order matters here, not just column presence: this module builds its output with the
same explicit ordering get_statement() produces (period_end, period_start, then each duration
concept's 7 columns in CONCEPTS' insertion order, then each instant concept's 3 columns, then
total_liabilities's 5 extra columns appended last, mirroring where
statements._derive_total_liabilities appends them after the rest of the statement is already
assembled) -- so a caller can diff .columns against a real get_statement() call and confirm an
exact match, not just a same-set-different-order one.
"""

import re
from decimal import Decimal, InvalidOperation

import pandas as pd

from ..data.concepts import CONCEPTS
from ..data.sheet_ingest import cell_reference, parse_a1_range

DURATION_CONCEPTS = [name for name, spec in CONCEPTS.items() if spec["kind"] == "duration"]
INSTANT_CONCEPTS = [name for name, spec in CONCEPTS.items() if spec["kind"] == "instant"]
ALL_CONCEPTS = DURATION_CONCEPTS + INSTANT_CONCEPTS

PERIOD_ROLE = "period_end"
UNMAPPED_ROLE = "unmapped"
MAPPABLE_ROLES = ALL_CONCEPTS + [PERIOD_ROLE]

# "Recommended, not required" concepts (see the design doc's minimum-viable-CSV section): a
# CSV that leaves one of these unmapped still normalizes successfully, but gets a plain-English
# note -- mirroring src/agent/tools.py's _unavailable_note pattern -- rather than being silently
# incomplete. revenue is the one concept that's a hard requirement (see validate_mapping) and so
# isn't in this list.
RECOMMENDED_CONCEPTS = [
    "net_income",
    "total_assets",
    "total_liabilities",
    "current_assets",
    "current_liabilities",
    "stockholders_equity",
    "operating_cash_flow",
    "capex",
]

# A CSV's rows must cluster into one of two supported cadences -- quarterly or annual -- or
# normalization refuses outright rather than silently producing a statement whose growth ratios
# (ratios.py's QoQ/YoY, both built on periods.py's calendar-tolerance lookups) would all
# silently return None for a cadence they were never designed for (e.g. monthly). Bounds mirror
# this codebase's own existing classification bounds rather than inventing new ones:
# _QUARTER_SPACING_DAYS_MAX=125 matches statements.py's _Q4_SPAN_DAYS_MAX (accounts for a real
# 52/53-week retail fiscal calendar's elongated quarter), and the annual bounds match
# concepts.py's own _ANNUAL_DAYS_MIN/_ANNUAL_DAYS_MAX.
_QUARTER_SPACING_DAYS_MIN, _QUARTER_SPACING_DAYS_MAX = 80, 125
_ANNUAL_SPACING_DAYS_MIN, _ANNUAL_SPACING_DAYS_MAX = 350, 380

_PAREN_NEGATIVE_RE = re.compile(r"^\((.*)\)$")

# The scale a sheet's numbers were typed in (Phase D session 4, open item 8), chosen by the
# person at confirm time. normalize() converts every mapped value to ones with exact Decimal
# arithmetic, so tools and the agent only ever see ones -- the model never has to multiply to
# say "$1.25 million", which would break the no-model-arithmetic rule and leave check_figures
# unable to trace the figure. "ones" is the backward-compatible default for callers that predate
# the setting (the Streamlit app); the Chrome extension always sends a scale explicitly, and
# never defaults it, since a silent "ones" on a sheet typed in thousands is wrong by 1000x.
SCALE_FACTORS = {
    "ones": Decimal(1),
    "thousands": Decimal(1_000),
    "millions": Decimal(1_000_000),
    "billions": Decimal(1_000_000_000),
}
DEFAULT_SCALE = "ones"

# How much of an unparseable cell's text find_unparsed_cells echoes back.
_UNPARSED_VALUE_MAX_CHARS = 50


def validate_mapping(raw, mapping: dict) -> list[str]:
    """
    Check a human-confirmed {csv_column: role} mapping for the minimum-viable-shape gates,
    independent of parsing any actual values. Returns a list of plain-English violation
    reasons (empty list = valid).

    A mapping key that isn't one of `raw`'s columns, or a role outside MAPPABLE_ROLES/
    UNMAPPED_ROLE, is a violation (Phase D session 4). Before that, an unknown column reached
    normalize() and raised a bare KeyError (a 500 from the backend's /confirm), and an unknown
    role (a typo like "reveneu") was silently ignored -- a mapping the person thought they'd
    made, quietly not applied.

    Checks: exactly one column mapped to "period_end" (zero -> no date column identified;
    more than one -> ambiguous, pick one); exactly one column mapped to "revenue" (revenue is
    the one concept load-bearing enough, across all four product modes, to be a hard gate --
    see the design doc); no other role claimed by more than one column (a role mapped twice is
    ambiguous, not a case to silently resolve by picking one). A single CSV column mapping to
    two different roles can't occur by construction -- the UI is one role-selector per column,
    so a column has exactly one role in `mapping` -- so that ambiguity isn't checked here.
    """
    errors = []
    known_columns = set(raw.df.columns)
    unknown_columns = [column for column in mapping if column not in known_columns]
    if unknown_columns:
        errors.append(
            "The mapping names column(s) that aren't in this data: "
            f"{', '.join(repr(c) for c in unknown_columns)}."
        )
    valid_roles = set(MAPPABLE_ROLES) | {UNMAPPED_ROLE}
    unknown_roles = sorted(
        {role for role in mapping.values() if role is not None and role not in valid_roles}
    )
    if unknown_roles:
        errors.append(
            f"Unknown role(s) in the mapping: {', '.join(repr(r) for r in unknown_roles)}. "
            "Use one of the listed concepts, period_end, or unmapped."
        )

    role_columns: dict[str, list[str]] = {}
    for column, role in mapping.items():
        if role in (UNMAPPED_ROLE, None) or role not in valid_roles or column not in known_columns:
            continue
        role_columns.setdefault(role, []).append(column)

    period_cols = role_columns.get(PERIOD_ROLE, [])
    if len(period_cols) == 0:
        errors.append(
            "No column was mapped as the date/period column -- pick one column that "
            "identifies each row's reporting period."
        )
    elif len(period_cols) > 1:
        errors.append(
            f"More than one column is mapped as the date/period column ({', '.join(period_cols)}) "
            "-- pick exactly one."
        )

    revenue_cols = role_columns.get("revenue", [])
    if len(revenue_cols) == 0:
        errors.append(
            "No column was mapped to revenue -- revenue is required to run any analysis on "
            "this file."
        )
    elif len(revenue_cols) > 1:
        errors.append(
            f"revenue is mapped to more than one column ({', '.join(revenue_cols)}) -- pick "
            "exactly one."
        )

    for role, columns in role_columns.items():
        if role in (PERIOD_ROLE, "revenue"):
            continue  # already checked above
        if len(columns) > 1:
            errors.append(
                f"{role} is mapped to more than one column ({', '.join(columns)}) -- pick "
                "exactly one."
            )

    return errors


def _is_blank(v) -> bool:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return True
    return str(v).strip() == ""


def _parse_numeric_cell(v) -> Decimal | None:
    """
    One raw cell -> an exact Decimal, tolerating common small-business bookkeeping formatting:
    a leading "$", thousands commas, and parenthesized negatives (e.g. "(1,234.56)" ->
    -1234.56). None for a blank cell and for anything that still isn't a finite number after
    cleanup ("N/A", "61.5%", "#DIV/0!", "NaN") -- find_unparsed_cells tells those two apart.
    """
    if _is_blank(v):
        return None
    text = str(v).strip()
    m = _PAREN_NEGATIVE_RE.match(text)
    negative = m is not None
    if negative:
        text = m.group(1)
    text = text.replace("$", "").replace(",", "").strip()
    if text == "":
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return -value if negative else value


def _clean_numeric_series(s: pd.Series, factor: Decimal = SCALE_FACTORS[DEFAULT_SCALE]) -> pd.Series:
    """
    Coerce a raw CSV column to numeric floats via _parse_numeric_cell, multiplied by `factor`
    (a SCALE_FACTORS value) in exact Decimal arithmetic before the single conversion to float
    -- so "0.1" in thousands is exactly 100.0, never 100.00000000000001. A cell that still
    isn't numeric after cleanup becomes None for that cell (not a file-level refusal) -- the
    same "missing for this period, not fatal" treatment get_statement() gives any other absent
    value. find_unparsed_cells is what reports a non-blank one of those to the person.
    """

    def clean_one(v):
        value = _parse_numeric_cell(v)
        return None if value is None else float(value * factor)

    return s.map(clean_one)


def _source_cell_for(raw, column: str, row_idx: int) -> str | None:
    """The sheet-qualified cell address (e.g. 'P&L'!B4) of data row `row_idx` in `column`, or
    None for a plain uploaded file with no spreadsheet `source`. Data row i sits one row below
    the header, i.e. sheet row start_row + 1 + i."""
    source = raw.source
    origin = parse_a1_range(source["range"]) if source else None
    if origin is None:
        return None
    start_row, start_col = origin[0], origin[1]
    return cell_reference(
        source["sheet_name"], start_row + 1 + row_idx, start_col + raw.df.columns.get_loc(column)
    )


def find_unparsed_cells(raw, mapping: dict) -> list[dict]:
    """
    Every non-blank cell in a column mapped to a concept that normalize() would turn into "no
    value" because it doesn't parse as a number -- "61.5%" typed as text, "#DIV/0!", "TRUE",
    "€1,250", "1 250 000" (Phase D session 4, chrome-extension-design.md SS10 open item 6).
    Before this, those cells became None silently. Only rows whose period cell parses as a date
    are checked: a row with a bad date is dropped whole, and normalize() already warns about
    it. Blank cells aren't listed -- a blank means "not reported", not a parse failure.

    Each entry: {"cell" (e.g. "'P&L'!B7", or None without a spreadsheet source),
    "source_row" (0-based data-row index, as in csv_provenance), "column", "role",
    "period_end" (ISO date), "value" (the raw text, capped at 50 characters)}, in concept then
    row order. Returns [] for a mapping that fails validate_mapping.
    """
    if validate_mapping(raw, mapping):
        return []
    role_to_column = {role: col for col, role in mapping.items() if role not in (UNMAPPED_ROLE, None)}
    parsed_dates = pd.to_datetime(raw.df[role_to_column[PERIOD_ROLE]], errors="coerce")

    unparsed = []
    for concept in ALL_CONCEPTS:
        column = role_to_column.get(concept)
        if column is None:
            continue
        for row_idx in raw.df.index:
            if pd.isna(parsed_dates.loc[row_idx]):
                continue
            v = raw.df.loc[row_idx, column]
            if _is_blank(v) or _parse_numeric_cell(v) is not None:
                continue
            unparsed.append(
                {
                    "cell": _source_cell_for(raw, column, int(row_idx)),
                    "source_row": int(row_idx),
                    "column": column,
                    "role": concept,
                    "period_end": parsed_dates.loc[row_idx].strftime("%Y-%m-%d"),
                    "value": str(v)[:_UNPARSED_VALUE_MAX_CHARS],
                }
            )
    return unparsed


def _fits_step_or_one_missing(gap: int, lo: int, hi: int) -> bool:
    """A gap fits this cadence's normal step, or exactly one period's worth of it is missing
    (2x the step) -- the latter is what a single dropped bad-date row leaves behind, and is
    indistinguishable from a filer that genuinely skipped reporting one period. Capped at
    exactly one missing period (not 3x/unbounded) so a genuinely annual file's ~365-day gaps
    can't spuriously satisfy a widened quarterly tolerance band -- see _detect_cadence."""
    return (lo <= gap <= hi) or (2 * lo <= gap <= 2 * hi)


def _detect_cadence(period_ends: list) -> tuple[str | None, str | None]:
    """
    Classify `period_ends` (already unique, sorted ascending) as "quarterly" or "annual" by
    the spacing between consecutive periods. Fewer than 2 periods can't be classified -- not a
    refusal, a single-period CSV is valid (see the design doc) and simply has no cadence to
    check growth ratios against. Returns (cadence, None) on a match, or (None, reason) when
    the spacing doesn't cleanly fit either supported cadence, tolerating at most one missing
    period per gap (see _fits_step_or_one_missing) so that dropping a single bad-date row (see
    normalize()) doesn't turn a real quarterly/annual file into a whole-file refusal.
    """
    if len(period_ends) < 2:
        return None, None

    gaps = [(b - a).days for a, b in zip(period_ends, period_ends[1:])]
    if all(_fits_step_or_one_missing(g, _QUARTER_SPACING_DAYS_MIN, _QUARTER_SPACING_DAYS_MAX) for g in gaps):
        return "quarterly", None
    if all(_fits_step_or_one_missing(g, _ANNUAL_SPACING_DAYS_MIN, _ANNUAL_SPACING_DAYS_MAX) for g in gaps):
        return "annual", None

    # Name the actual offending gap(s) rather than an uninformative global median -- score
    # against whichever cadence has fewer violations, since that's the more useful read to hand
    # back to the user.
    pairs = list(zip(period_ends, period_ends[1:], gaps))
    offenders_by_cadence = {
        "quarterly": [
            p for p in pairs
            if not _fits_step_or_one_missing(p[2], _QUARTER_SPACING_DAYS_MIN, _QUARTER_SPACING_DAYS_MAX)
        ],
        "annual": [
            p for p in pairs
            if not _fits_step_or_one_missing(p[2], _ANNUAL_SPACING_DAYS_MIN, _ANNUAL_SPACING_DAYS_MAX)
        ],
    }
    _, offenders = min(offenders_by_cadence.items(), key=lambda kv: len(kv[1]))
    named = "; ".join(f"{a.date()} to {b.date()} is {g} days" for a, b, g in offenders[:3])
    more = f" (and {len(offenders) - 3} more)" if len(offenders) > 3 else ""
    return None, (
        f"The gap {named}{more} doesn't fit quarterly spacing (~80-125 days, or ~160-250 days "
        "if exactly one period is missing) or annual spacing (~350-380 days, or ~700-760 days "
        "if one period is missing). Only quarterly- or annual-cadence CSVs, allowing for at "
        "most one missing period, are supported in this version -- monthly or irregular "
        "spacing is not yet supported."
    )


def statement_from_records(records: list[dict], attrs: dict) -> pd.DataFrame:
    """
    Rebuild a normalize()-shaped DataFrame from its JSON round-trip: records is
    df.to_json(orient="records", date_format="iso") already decoded back into a list of dicts
    (as the backend's csv_statements.statement_data column stores it), attrs is the
    corresponding df.attrs dict (csv_statements.statement_attrs). Used by the Sheets Add-on
    backend to reload a previously confirmed CSV statement before wiring it into
    src/agent/csv_session.py ahead of a run_agent call -- see backend/app/main.py.

    The JSON round-trip turns period_end/period_start and every {concept}_filed column into
    ISO-string (or None) values; this re-parses exactly those columns back to datetime64,
    mirroring normalize()'s own final dtype-coercion block, so the result is dtype-identical to
    what normalize() itself returns, not just value-identical.
    """
    df = pd.DataFrame.from_records(records)
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["period_start"] = pd.to_datetime(df["period_start"])
    for concept in ALL_CONCEPTS:
        df[f"{concept}_filed"] = pd.to_datetime(df[f"{concept}_filed"])
    df.attrs = dict(attrs)
    return df


def normalize(
    raw,
    mapping: dict,
    entity_name: str,
    scale: str = DEFAULT_SCALE,
    currency: str | None = None,
) -> tuple[pd.DataFrame | None, list, list]:
    """
    Build a get_statement()-shaped DataFrame from `raw` (a csv_ingest.RawCsv) and a
    human-confirmed {csv_column: role} mapping. `scale` (a SCALE_FACTORS key) is the unit the
    sheet's numbers were typed in: every mapped value is converted to ones exactly (see
    SCALE_FACTORS). `currency` is an ISO-4217 label, or None when the person didn't state one --
    a label only, never a conversion. Both are recorded in df.attrs["csv_source"].
    Returns (df, errors, warnings):
      - On refusal: (None, errors, warnings) -- errors is non-empty, naming every violated
        gate; no partial/guessed DataFrame is ever returned alongside a refusal.
      - On success: (df, [], warnings) -- warnings may be non-empty (dropped bad-date rows,
        unmapped recommended concepts) without blocking normalization.

    Refuses (hard gate, no DataFrame): validate_mapping's violations; zero rows with a
    parseable date in the mapped period column; two rows resolving to the same period_end
    (ambiguous -- which one is authoritative is not this module's call to make); a period
    spacing that isn't quarterly- or annual-cadence (see _detect_cadence).

    Drops (soft, row-level, not a file-level refusal): a row whose mapped date cell doesn't
    parse -- reported by its cell address (with a spreadsheet `source`) or row number, and its
    raw value, in `warnings`; the surviving rows still normalize. Blank cells in a mapped
    concept column are summarized per concept in `warnings`; non-blank cells that don't parse
    are reported separately by find_unparsed_cells, which the backend's /confirm requires the
    person to acknowledge.

    Every EDGAR-only column is stubbed to a fixed default, never computed: {concept}_is_derived
    is always False, {concept}_derivation_method is always None, {concept}_q4_subtraction_value
    is always NaN, {concept}_q4_diverges_from_subtraction is always False,
    total_liabilities_derivation_method is "direct_tag" for a period where a value is present
    (a real, directly-supplied CSV cell -- not a redefinition of is_derived, mirroring
    statements.py's own "direct_tag can pair with is_derived=False" convention) and None
    otherwise, and total_liabilities_alt_value/_alt_method/_diverges_from_alt are always
    NaN/None/False -- the fallback-derivation and cross-check machinery in
    statements._derive_total_liabilities is EDGAR-tag-availability-specific and deliberately
    not reproduced here (see module docstring).

    df.attrs carries entity_name (as given), cik=None (never a fabricated placeholder),
    periods_available (the row count after date-parsing/dedup), csv_source
    ({"filename", "uploaded_at", "cadence", "scale", "currency"}), and csv_provenance
    ({concept: {period_end_iso: {"source_row", "source_column"}}}, entries only for periods
    where that concept has a real value, plus "source_cell" -- a sheet-qualified address like
    'P&L'!B4 -- when `raw.source` says which spreadsheet range the rows came from; source_row
    stays the 0-based data-row index either way) -- the last two are additive metadata for a future
    CSV-facing agent tool to cite, not part of get_statement()'s own contract, so they don't
    affect a caller diffing .columns against a real get_statement() result.
    sparse_history/sparse_history_note are deliberately omitted -- that signal exists to catch
    SEC's ticker-to-CIK mapping repointing to a newly registered successor entity, which has no
    CSV analog, and reusing its EDGAR-specific wording here would be actively misleading.
    """
    errors = validate_mapping(raw, mapping)
    if scale not in SCALE_FACTORS:
        errors.append(f"Unknown scale {scale!r}; use one of {', '.join(SCALE_FACTORS)}.")
    if errors:
        return None, errors, []
    factor = SCALE_FACTORS[scale]

    role_to_column = {role: col for col, role in mapping.items() if role not in (UNMAPPED_ROLE, None)}
    period_column = role_to_column[PERIOD_ROLE]

    warnings: list[str] = []
    parsed_dates = pd.to_datetime(raw.df[period_column], errors="coerce")
    valid_mask = parsed_dates.notna()
    for idx in raw.df.index[~valid_mask]:
        cell = _source_cell_for(raw, period_column, int(idx))
        where = f"Row {idx} ({cell})" if cell else f"Row {idx}"
        warnings.append(
            f"{where} was dropped: {period_column!r} value {raw.df.loc[idx, period_column]!r} "
            "could not be parsed as a date."
        )
    if not valid_mask.any():
        return None, [
            f"No row had a parseable date in the {period_column!r} column -- check the date "
            "format and try again."
        ], warnings

    work = pd.DataFrame({"_period_end": parsed_dates[valid_mask]}, index=raw.df.index[valid_mask])
    work = work.sort_values("_period_end")

    dup_mask = work["_period_end"].duplicated(keep=False)
    if dup_mask.any():
        dup_dates = sorted(work.loc[dup_mask, "_period_end"].dt.strftime("%Y-%m-%d").unique())
        return None, [
            f"Period {d} appears in more than one row -- remove or merge the duplicate rows "
            "before uploading." for d in dup_dates
        ], warnings

    period_ends = work["_period_end"].tolist()
    source_rows = work.index.tolist()
    n = len(period_ends)

    cadence, cadence_error = _detect_cadence(period_ends)
    if cadence_error:
        return None, [cadence_error], warnings

    uploaded_at_ts = pd.Timestamp(raw.uploaded_at)
    period_end_iso = [d.strftime("%Y-%m-%d") for d in period_ends]

    cleaned_columns: dict[str, pd.Series] = {}
    provenance: dict[str, dict] = {}

    def values_for(concept: str) -> list:
        column = role_to_column.get(concept)
        if column is None:
            return [float("nan")] * n
        if column not in cleaned_columns:
            cleaned_columns[column] = _clean_numeric_series(raw.df[column], factor)
        cleaned = cleaned_columns[column]
        result = []
        blanks = 0
        prov = provenance.setdefault(concept, {})
        for i, row_idx in enumerate(source_rows):
            v = cleaned.loc[row_idx]
            if v is None:
                result.append(float("nan"))
                blanks += _is_blank(raw.df.loc[row_idx, column])
            else:
                result.append(v)
                # A spreadsheet-sourced upload (sheet_ingest's `source`) also cites the cell.
                entry = {"source_row": int(row_idx), "source_column": column}
                cell = _source_cell_for(raw, column, int(row_idx))
                if cell is not None:
                    entry["source_cell"] = cell
                prov[period_end_iso[i]] = entry
        if blanks:
            warnings.append(
                f"{concept} ({column!r}) is blank for {blanks} of {n} period(s); those periods "
                "have no value for it."
            )
        return result

    out: dict[str, list] = {
        "period_end": period_ends,
        "period_start": [pd.NaT] * n,
    }

    for concept in DURATION_CONCEPTS:
        column = role_to_column.get(concept)
        values = values_for(concept)
        has_value = [v == v for v in values]  # NaN != NaN
        out[concept] = values
        out[f"{concept}_tag"] = [column if hv else None for hv in has_value]
        out[f"{concept}_filed"] = [uploaded_at_ts if hv else None for hv in has_value]
        out[f"{concept}_is_derived"] = [False] * n
        out[f"{concept}_derivation_method"] = [None] * n
        out[f"{concept}_q4_subtraction_value"] = [float("nan")] * n
        out[f"{concept}_q4_diverges_from_subtraction"] = [False] * n
        if column is None and concept in RECOMMENDED_CONCEPTS:
            warnings.append(
                f"No column was mapped to {concept}; every period's value for it will be "
                "unavailable."
            )

    for concept in INSTANT_CONCEPTS:
        column = role_to_column.get(concept)
        values = values_for(concept)
        has_value = [v == v for v in values]
        out[concept] = values
        out[f"{concept}_tag"] = [column if hv else None for hv in has_value]
        out[f"{concept}_filed"] = [uploaded_at_ts if hv else None for hv in has_value]
        if column is None and concept in RECOMMENDED_CONCEPTS:
            warnings.append(
                f"No column was mapped to {concept}; every period's value for it will be "
                "unavailable."
            )

    tl_has_value = [v == v for v in out["total_liabilities"]]
    out["total_liabilities_is_derived"] = [False] * n
    out["total_liabilities_derivation_method"] = ["direct_tag" if hv else None for hv in tl_has_value]
    out["total_liabilities_alt_value"] = [float("nan")] * n
    out["total_liabilities_alt_method"] = [None] * n
    out["total_liabilities_diverges_from_alt"] = [False] * n

    df = pd.DataFrame(out)
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["period_start"] = pd.to_datetime(df["period_start"])
    for concept in ALL_CONCEPTS:
        df[f"{concept}_filed"] = pd.to_datetime(df[f"{concept}_filed"])

    df.attrs["entity_name"] = entity_name
    df.attrs["cik"] = None
    df.attrs["periods_available"] = n
    df.attrs["csv_source"] = {
        "filename": raw.filename,
        "uploaded_at": uploaded_at_ts.strftime("%Y-%m-%d %H:%M:%S"),
        "cadence": cadence,
        "scale": scale,
        "currency": currency,
    }
    # Where a spreadsheet-sourced statement came from (Phase D session 5), so the agent can call
    # it "your sheet" with its sheet and range rather than an "uploaded CSV". Absent for a plain
    # file upload.
    if raw.source:
        df.attrs["csv_source"].update(
            {
                "sheet_name": raw.source.get("sheet_name"),
                "range": raw.source.get("range"),
                "file_name": raw.source.get("file_name"),
                "platform": raw.source.get("platform"),
            }
        )
    df.attrs["csv_provenance"] = provenance

    return df, [], warnings
