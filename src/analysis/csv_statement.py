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

import pandas as pd

from ..data.concepts import CONCEPTS

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


def validate_mapping(raw, mapping: dict) -> list[str]:
    """
    Check a human-confirmed {csv_column: role} mapping for the minimum-viable-shape gates,
    independent of parsing any actual values. Returns a list of plain-English violation
    reasons (empty list = valid). Roles not in MAPPABLE_ROLES/UNMAPPED_ROLE are treated as
    "unmapped" -- callers (the confirmation UI, normalize()) should already restrict widget
    choices to valid roles, so this is a defensive floor, not the primary UI validation.

    Checks: exactly one column mapped to "period_end" (zero -> no date column identified;
    more than one -> ambiguous, pick one); exactly one column mapped to "revenue" (revenue is
    the one concept load-bearing enough, across all four product modes, to be a hard gate --
    see the design doc); no other role claimed by more than one column (a role mapped twice is
    ambiguous, not a case to silently resolve by picking one). A single CSV column mapping to
    two different roles can't occur by construction -- the UI is one role-selector per column,
    so a column has exactly one role in `mapping` -- so that ambiguity isn't checked here.
    """
    errors = []
    role_columns: dict[str, list[str]] = {}
    for column, role in mapping.items():
        if role in (UNMAPPED_ROLE, None):
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


def _clean_numeric_series(s: pd.Series) -> pd.Series:
    """
    Coerce a raw CSV column to numeric, tolerating common small-business bookkeeping
    formatting: a leading "$", thousands commas, and parenthesized negatives (e.g.
    "(1,234.56)" -> -1234.56). A cell that still isn't numeric after cleanup becomes None for
    that cell (not a file-level refusal) -- the same "missing for this period, not fatal"
    treatment get_statement() gives any other absent value.
    """

    def clean_one(v):
        if pd.isna(v):
            return None
        text = str(v).strip()
        if text == "":
            return None
        m = _PAREN_NEGATIVE_RE.match(text)
        negative = m is not None
        if negative:
            text = m.group(1)
        text = text.replace("$", "").replace(",", "").strip()
        if text == "":
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return -value if negative else value

    return s.map(clean_one)


def _detect_cadence(period_ends: list) -> tuple[str | None, str | None]:
    """
    Classify `period_ends` (already unique, sorted ascending) as "quarterly" or "annual" by
    the spacing between consecutive periods. Fewer than 2 periods can't be classified -- not a
    refusal, a single-period CSV is valid (see the design doc) and simply has no cadence to
    check growth ratios against. Returns (cadence, None) on a match, or (None, reason) when
    the spacing doesn't cleanly fit either supported cadence.
    """
    if len(period_ends) < 2:
        return None, None

    gaps = [(b - a).days for a, b in zip(period_ends, period_ends[1:])]
    if all(_QUARTER_SPACING_DAYS_MIN <= g <= _QUARTER_SPACING_DAYS_MAX for g in gaps):
        return "quarterly", None
    if all(_ANNUAL_SPACING_DAYS_MIN <= g <= _ANNUAL_SPACING_DAYS_MAX for g in gaps):
        return "annual", None

    gaps_sorted = sorted(gaps)
    median_gap = gaps_sorted[len(gaps_sorted) // 2]
    return None, (
        f"The spacing between periods (around {median_gap} days between rows) doesn't match "
        "quarterly (~80-125 days) or annual (~350-380 days) spacing. Only quarterly- or "
        "annual-cadence CSVs are supported in this version -- monthly or irregular spacing "
        "is not yet supported."
    )


def normalize(raw, mapping: dict, entity_name: str) -> tuple[pd.DataFrame | None, list, list]:
    """
    Build a get_statement()-shaped DataFrame from `raw` (a csv_ingest.RawCsv) and a
    human-confirmed {csv_column: role} mapping. Returns (df, errors, warnings):
      - On refusal: (None, errors, warnings) -- errors is non-empty, naming every violated
        gate; no partial/guessed DataFrame is ever returned alongside a refusal.
      - On success: (df, [], warnings) -- warnings may be non-empty (dropped bad-date rows,
        unmapped recommended concepts) without blocking normalization.

    Refuses (hard gate, no DataFrame): validate_mapping's violations; zero rows with a
    parseable date in the mapped period column; two rows resolving to the same period_end
    (ambiguous -- which one is authoritative is not this module's call to make); a period
    spacing that isn't quarterly- or annual-cadence (see _detect_cadence).

    Drops (soft, row-level, not a file-level refusal): a row whose mapped date cell doesn't
    parse -- reported by its row number and raw value in `warnings`, the surviving rows still
    normalize.

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
    ({"filename", "uploaded_at", "cadence"}), and csv_provenance
    ({concept: {period_end_iso: {"source_row", "source_column"}}}, entries only for periods
    where that concept has a real value) -- the last two are additive metadata for a future
    CSV-facing agent tool to cite, not part of get_statement()'s own contract, so they don't
    affect a caller diffing .columns against a real get_statement() result.
    sparse_history/sparse_history_note are deliberately omitted -- that signal exists to catch
    SEC's ticker-to-CIK mapping repointing to a newly registered successor entity, which has no
    CSV analog, and reusing its EDGAR-specific wording here would be actively misleading.
    """
    errors = validate_mapping(raw, mapping)
    if errors:
        return None, errors, []

    role_to_column = {role: col for col, role in mapping.items() if role != UNMAPPED_ROLE}
    period_column = role_to_column[PERIOD_ROLE]

    warnings: list[str] = []
    parsed_dates = pd.to_datetime(raw.df[period_column], errors="coerce")
    valid_mask = parsed_dates.notna()
    for idx in raw.df.index[~valid_mask]:
        warnings.append(
            f"Row {idx} was dropped: {period_column!r} value {raw.df.loc[idx, period_column]!r} "
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
            cleaned_columns[column] = _clean_numeric_series(raw.df[column])
        cleaned = cleaned_columns[column]
        result = []
        prov = provenance.setdefault(concept, {})
        for i, row_idx in enumerate(source_rows):
            v = cleaned.loc[row_idx]
            if v is None:
                result.append(float("nan"))
            else:
                result.append(v)
                prov[period_end_iso[i]] = {"source_row": int(row_idx), "source_column": column}
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
    }
    df.attrs["csv_provenance"] = provenance

    return df, [], warnings
