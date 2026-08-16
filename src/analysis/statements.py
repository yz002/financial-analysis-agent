"""
Assembles individual XBRL concepts (concepts.get_concept) into one wide
financial statement per ticker/period, indexed by period_end.

period_end, not fiscal_year, is the join key throughout this module. Per
NOTES.md, EDGAR's fiscal_year/fiscal_period can reflect a later filing's
comparative-column attribution rather than the period's true original
context, so get_statement's output deliberately excludes fiscal_year/
fiscal_period entirely rather than risk a caller grouping on them. A
caller that needs EDGAR's own fy/fp label for one specific number should
call get_concept directly.

The "Q4 problem" (see concepts.py's module docstring) is analysis-layer
work by this project's design, so this is the one place a Q4 value is
synthesized: Q4 = FY - (Q1+Q2+Q3). That arithmetic is definitionally true
once real Q1/Q2/Q3/FY values are in hand -- there's no independent value
to check it against. The actual risk is whether the *periods* genuinely
tile the fiscal year: a missing quarter, a restatement that shifted a
period boundary, or a fiscal-calendar change can all silently break the
subtraction into nonsense. _derive_q4 checks period tiling (not the
arithmetic) and refuses -- leaving that concept/year absent rather than
emitting a wrong number -- if the check fails. Every derived value is
marked via a companion "{concept}_is_derived" boolean column; nothing is
ever inserted unflagged, and real (non-derived) rows are always literal
False there, never NaN.

Every ticker gets the same fixed column schema regardless of what data is
actually available (Ford has no gross_profit at all) -- a concept with
zero usable data still gets its columns, filled with NaN/None/False, so
callers (ratios.py) never need `hasattr`/`in df.columns` guards.
"""

import pandas as pd

from ..data.concepts import (
    CONCEPTS,
    ConceptNotFoundError,
    _QUARTERLY_DAYS_MAX,
    _QUARTERLY_DAYS_MIN,
    get_concept,
)
from ..data.edgar_client import EdgarClient

DURATION_CONCEPTS = [name for name, spec in CONCEPTS.items() if spec["kind"] == "duration"]
INSTANT_CONCEPTS = [name for name, spec in CONCEPTS.items() if spec["kind"] == "instant"]

# Slack allowed when checking that Q1/Q2/Q3 tile a fiscal year contiguously,
# to absorb reporting-calendar conventions (e.g. weekly/monthly fiscal
# calendars whose quarter boundaries don't fall on the exact same day of
# the enclosing FY's start/end).
_TILE_TOLERANCE_DAYS = 3

_ONE_DAY = pd.Timedelta(days=1)


def get_statement(
    ticker: str,
    period_length: str = "quarterly",
    periods: int | None = None,
    client: EdgarClient | None = None,
) -> pd.DataFrame:
    """
    Return one wide financial statement DataFrame for `ticker`, one row per
    period_end, joining all 12 concepts in CONCEPTS.

    Columns: period_end, period_start, and per concept: "{concept}" (value),
    "{concept}_tag" (source XBRL tag, or "derived" for a synthesized Q4
    row), "{concept}_filed" (the filing date backing that value), and --
    for duration concepts only -- "{concept}_is_derived" (bool, always
    literal True/False, never NaN). A concept with no usable data for this
    ticker still gets these columns, filled with NaN/None/False.

    period_length: "quarterly" (default) or "annual". In "quarterly" mode,
    a Q4 row per fiscal year is synthesized for duration concepts where the
    real Q1/Q2/Q3 quarters and the FY total are available and their periods
    reconcile (see module docstring); instant concepts (balance-sheet
    items) need no such synthesis since the 10-K already reports them at
    fiscal-year-end.

    periods: if given, return only the most recent `periods` rows (applied
    after assembling full history, since Q4 derivation for a recent quarter
    can depend on an FY row older than the truncation window).

    Raises ValueError for an unknown period_length. Raises
    ConceptNotFoundError only if literally no concept has any usable data
    for this ticker (there's no statement to build); a real network/auth
    error from the underlying client still propagates unchanged.
    """
    if period_length not in ("quarterly", "annual"):
        raise ValueError('period_length must be "quarterly" or "annual"')
    client = client or EdgarClient()

    duration_frames = {}
    for concept in DURATION_CONCEPTS:
        qtr_df = _try_get_concept(ticker, concept, period_length, client)
        if qtr_df is not None:
            qtr_df = _dedupe_by_period_end(qtr_df)
        if period_length == "quarterly" and qtr_df is not None:
            qtr_df = qtr_df.copy()
            qtr_df["is_derived"] = False
            ann_df = _try_get_concept(ticker, concept, "annual", client)
            if ann_df is not None:
                ann_df = _dedupe_by_period_end(ann_df)
                derived = _derive_q4(qtr_df, ann_df)
                if len(derived):
                    qtr_df = pd.concat([qtr_df, derived], ignore_index=True)
        if qtr_df is not None:
            duration_frames[concept] = _prepare_duration_columns(qtr_df, concept)

    instant_frames = {}
    for concept in INSTANT_CONCEPTS:
        df = _try_get_concept(ticker, concept, None, client)
        if df is not None:
            instant_frames[concept] = _prepare_instant_columns(df, concept)

    if not duration_frames and not instant_frames:
        raise ConceptNotFoundError(f"No usable data for any concept for {ticker!r}")

    backbone_source = duration_frames or instant_frames
    period_ends = sorted(
        set().union(*(frame["period_end"] for frame in backbone_source.values()))
    )
    statement = pd.DataFrame({"period_end": period_ends})

    period_starts = None
    for frame in duration_frames.values():
        starts = frame.set_index("period_end")["period_start"]
        period_starts = starts if period_starts is None else period_starts.combine_first(starts)
    statement["period_start"] = (
        statement["period_end"].map(period_starts) if period_starts is not None else pd.NaT
    )

    for frame in duration_frames.values():
        cols = [c for c in frame.columns if c not in ("period_end", "period_start")]
        statement = statement.merge(frame[["period_end", *cols]], on="period_end", how="left")

    for frame in instant_frames.values():
        cols = [c for c in frame.columns if c != "period_end"]
        statement = statement.merge(frame[["period_end", *cols]], on="period_end", how="left")

    for concept in DURATION_CONCEPTS:
        if concept not in duration_frames:
            statement[concept] = float("nan")
            statement[f"{concept}_tag"] = None
            statement[f"{concept}_filed"] = pd.NaT
            statement[f"{concept}_is_derived"] = False
    for concept in INSTANT_CONCEPTS:
        if concept not in instant_frames:
            statement[concept] = float("nan")
            statement[f"{concept}_tag"] = None
            statement[f"{concept}_filed"] = pd.NaT

    for concept in DURATION_CONCEPTS:
        statement[f"{concept}_is_derived"] = (
            statement[f"{concept}_is_derived"].fillna(False).astype(bool)
        )

    statement = statement.sort_values("period_end").reset_index(drop=True)
    if periods is not None:
        statement = statement.tail(periods).reset_index(drop=True)
    return statement


def _try_get_concept(
    ticker: str, concept: str, period_length: str | None, client: EdgarClient
) -> pd.DataFrame | None:
    """get_concept, returning None instead of raising when this ticker has no usable data."""
    try:
        return get_concept(ticker, concept, client=client, period_length=period_length)
    except ConceptNotFoundError:
        return None


def _dedupe_by_period_end(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse rows sharing the same period_end to one row -- this module's
    join grain -- even though concepts.py's own dedup keys duration facts
    on the finer (period_start, period_end) pair. That finer key can leave
    two rows for the same period_end differing only by a one-day
    period_start reporting inconsistency (confirmed in real MSFT data:
    2016-07-01 vs 2016-07-02 both reported as the start of the quarter
    ending 2016-09-30), which would otherwise make this module's per-
    period_end grouping ambiguous. The latest `filed` wins, mirroring
    concepts.py's own tie-break rule.
    """
    return (
        df.sort_values("filed")
        .drop_duplicates("period_end", keep="last")
        .sort_values("period_end")
        .reset_index(drop=True)
    )


def _prepare_duration_columns(df: pd.DataFrame, concept: str) -> pd.DataFrame:
    is_derived = df["is_derived"] if "is_derived" in df.columns else False
    return pd.DataFrame(
        {
            "period_end": df["period_end"],
            "period_start": df["period_start"],
            concept: df["value"],
            f"{concept}_tag": df["tag"],
            f"{concept}_filed": df["filed"],
            f"{concept}_is_derived": is_derived,
        }
    )


def _prepare_instant_columns(df: pd.DataFrame, concept: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "period_end": df["period_end"],
            concept: df["value"],
            f"{concept}_tag": df["tag"],
            f"{concept}_filed": df["filed"],
        }
    )


def _derive_q4(qtr_df: pd.DataFrame, ann_df: pd.DataFrame) -> pd.DataFrame:
    """
    For each fiscal year in `ann_df` (period_length="annual" rows), derive
    a Q4 row from the real quarterly rows in `qtr_df` when they reconcile
    (see module docstring). Returns only the successfully-derived rows, in
    the same shape as a get_concept duration result plus "is_derived".
    """
    rows = []
    for fy in ann_df.itertuples():
        candidates = qtr_df[
            (qtr_df["period_start"] >= fy.period_start) & (qtr_df["period_end"] <= fy.period_end)
        ].sort_values("period_start")

        if len(candidates) != 3:
            continue
        q1, q2, q3 = candidates.itertuples()

        if not _quarters_tile_fiscal_year(q1, q2, q3, fy.period_start, fy.period_end):
            continue

        rows.append(
            {
                "period_end": fy.period_end,
                "period_start": q3.period_end + _ONE_DAY,
                "value": fy.value - (q1.value + q2.value + q3.value),
                "fiscal_year": fy.fiscal_year,
                "fiscal_period": "Q4",
                "form": fy.form,
                "filed": fy.filed,
                "tag": "derived",
                "period_length": "quarterly",
                "is_derived": True,
            }
        )
    return pd.DataFrame(rows)


def _quarters_tile_fiscal_year(q1, q2, q3, fy_start, fy_end) -> bool:
    """
    True if q1/q2/q3 (sorted by period_start) are contiguous, non-
    overlapping, and start at fy_start with no gaps beyond
    _TILE_TOLERANCE_DAYS -- and the remaining implied Q4 span
    (q3.period_end+1 to fy_end) is itself a plausible quarter length.
    Refusing here is what prevents a missing quarter, a restatement that
    shifted a period boundary, or a fiscal-calendar change from silently
    producing a wrong derived Q4 value.
    """
    tol = pd.Timedelta(days=_TILE_TOLERANCE_DAYS)

    if abs(q1.period_start - fy_start) > tol:
        return False
    if abs(q2.period_start - (q1.period_end + _ONE_DAY)) > tol:
        return False
    if abs(q3.period_start - (q2.period_end + _ONE_DAY)) > tol:
        return False

    implied_q4_days = (fy_end - (q3.period_end + _ONE_DAY)).days
    return _QUARTERLY_DAYS_MIN <= implied_q4_days <= _QUARTERLY_DAYS_MAX
