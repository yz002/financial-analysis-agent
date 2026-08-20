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

Sometimes a real, separately filed Q4 fact already exists instead of
needing to be derived -- 4 real quarterly candidates tiling the fiscal
year, not 3 -- common for large-caps whose pre-~2021 10-Ks tagged a
discrete Q4 via the now-discontinued Item 302 "selected quarterly
financial data" footnote. That fact is used as-is ("is_derived" stays
False, nothing is synthesized); _derive_q4 instead cross-checks it
against what FY-(Q1+Q2+Q3) subtraction would have given, recording
"{concept}_q4_subtraction_value"/"{concept}_q4_diverges_from_subtraction".
The two numbers can genuinely disagree -- confirmed on Walmart, Duke
Energy, and this project's own MSFT test fixture, by up to ~6-8% of the
FY total -- because the FY total's own "filed"/"tag" can come from a
later, differently-tagged restated comparative filing than the quarters,
which are filed together and never get that refresh. See NOTES.md.

Every ticker gets the same fixed column schema regardless of what data is
actually available (Ford has no gross_profit at all) -- a concept with
zero usable data still gets its columns, filled with NaN/None/False, so
callers (ratios.py) never need `hasattr`/`in df.columns` guards.
"""

import pandas as pd

from ..data.concepts import CONCEPTS, ConceptNotFoundError, get_concept
from ..data.edgar_client import EdgarClient

DURATION_CONCEPTS = [name for name, spec in CONCEPTS.items() if spec["kind"] == "duration"]
INSTANT_CONCEPTS = [name for name, spec in CONCEPTS.items() if spec["kind"] == "instant"]

# Slack allowed when checking that Q1/Q2/Q3 tile a fiscal year contiguously,
# to absorb reporting-calendar conventions (e.g. weekly/monthly fiscal
# calendars whose quarter boundaries don't fall on the exact same day of
# the enclosing FY's start/end).
_TILE_TOLERANCE_DAYS = 3

# Bounds for the *implied* Q4 span (fy_end - q3_end) that _quarters_tile_fiscal_year accepts as a
# plausible quarter -- deliberately separate from concepts._QUARTERLY_DAYS_MIN/_QUARTERLY_DAYS_MAX
# (80-100), which classify a *reported* duration fact as quarterly-vs-YTD-vs-other and must stay
# tight to avoid mistaking a 6-/9-month YTD cash-flow fact for a real quarter (see concepts.py's
# module docstring and NOTES.md's cash-flow-YTD note). An implied Q4 is never itself a reported
# fact to misclassify, so it can tolerate a wider range: 52/53-week retail fiscal calendars
# (Costco confirmed; Walmart/Target/Kroger use the same convention) run Q1-Q3 at ~12 weeks each
# but let Q4 absorb the leftover week(s), landing Q4 at ~16-17 weeks (111-118 days observed for
# Costco) -- a real, correct quarter length that the classification-only bounds would wrongly
# reject.
_Q4_SPAN_DAYS_MIN, _Q4_SPAN_DAYS_MAX = 80, 125

# Relative tolerance (as a fraction of the FY total) for flagging a divergence between a real
# filed Q4 fact and what FY-(Q1+Q2+Q3) subtraction would give, when all 4 candidates are real
# filed quarters (see _four_quarters_tile_fiscal_year). Not a data-quality bug in either number:
# confirmed on WMT (FY2010 diff -$2.95B/-0.7% of FY total, FY2011 -$2.90B, FY2017 +$4.56B), DUK
# (FY2012 +$1.71B, FY2014 +$1.42B), and this project's own MSFT test fixture (FY2016, -$5.83B,
# ~6.4% of FY total) -- root cause is _dedupe_by_period_end independently picking the
# latest-filed appearance of the FY total and of the quarters, which can land on different
# filings (the FY total sourced from a later, differently-tagged restated comparative column
# that the already-filed quarters never get refreshed with). 0.5% clears every confirmed real
# case (smallest: WMT FY2011 at 0.7%) while not firing on near-exact agreement (NVDA's smallest
# real diff: 0.004%). See NOTES.md.
_Q4_RECONCILIATION_TOLERANCE = 0.005

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
    literal True/False, never NaN), "{concept}_q4_subtraction_value"
    (float, NaN unless this row is a real filed Q4 fact being
    cross-checked against subtraction -- see module docstring), and
    "{concept}_q4_diverges_from_subtraction" (bool, always literal
    True/False, never NaN). A concept with no usable data for this ticker
    still gets these columns, filled with NaN/None/False.

    period_length: "quarterly" (default) or "annual". In "quarterly" mode,
    a Q4 row per fiscal year is synthesized for duration concepts where the
    real Q1/Q2/Q3 quarters and the FY total are available and their periods
    reconcile (see module docstring); instant concepts (balance-sheet
    items) need no such synthesis since the 10-K already reports them at
    fiscal-year-end. When a real Q4 fact already exists instead, it's used
    directly and cross-checked against subtraction (see module docstring)
    rather than synthesized.

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
                derived, reconciliation = _derive_q4(qtr_df, ann_df)
                if len(derived):
                    qtr_df = pd.concat([qtr_df, derived], ignore_index=True)
                # Merge runs after the concat, so a newly-added derived row's period_end (never
                # present in `reconciliation`, which only covers the disjoint 4-candidate set)
                # correctly falls back to NaN/False below -- "not applicable", same convention as
                # is_derived. q4_subtraction_value is deliberately not filled -- NaN means "not
                # applicable" here, same as the concept's own raw value column would use NaN.
                qtr_df = qtr_df.merge(reconciliation, on="period_end", how="left")
                qtr_df["q4_diverges_from_subtraction"] = (
                    qtr_df["q4_diverges_from_subtraction"].fillna(False).astype(bool)
                )
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
            statement[f"{concept}_q4_subtraction_value"] = float("nan")
            statement[f"{concept}_q4_diverges_from_subtraction"] = False
    for concept in INSTANT_CONCEPTS:
        if concept not in instant_frames:
            statement[concept] = float("nan")
            statement[f"{concept}_tag"] = None
            statement[f"{concept}_filed"] = pd.NaT

    for concept in DURATION_CONCEPTS:
        statement[f"{concept}_is_derived"] = (
            statement[f"{concept}_is_derived"].fillna(False).astype(bool)
        )
        statement[f"{concept}_q4_diverges_from_subtraction"] = (
            statement[f"{concept}_q4_diverges_from_subtraction"].fillna(False).astype(bool)
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
    q4_subtraction_value = (
        df["q4_subtraction_value"] if "q4_subtraction_value" in df.columns else float("nan")
    )
    q4_diverges = (
        df["q4_diverges_from_subtraction"] if "q4_diverges_from_subtraction" in df.columns else False
    )
    return pd.DataFrame(
        {
            "period_end": df["period_end"],
            "period_start": df["period_start"],
            concept: df["value"],
            f"{concept}_tag": df["tag"],
            f"{concept}_filed": df["filed"],
            f"{concept}_is_derived": is_derived,
            f"{concept}_q4_subtraction_value": q4_subtraction_value,
            f"{concept}_q4_diverges_from_subtraction": q4_diverges,
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


def _derive_q4(qtr_df: pd.DataFrame, ann_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    For each fiscal year in `ann_df` (period_length="annual" rows), resolve
    Q4 from the real quarterly rows in `qtr_df`:

      - Exactly 3 candidates tiling the fiscal year
        (_quarters_tile_fiscal_year): synthesize Q4 = FY - (Q1+Q2+Q3) as a
        new row, is_derived=True, tag="derived" -- unchanged from before.
      - Exactly 4 candidates tiling the fiscal year
        (_four_quarters_tile_fiscal_year): the 4th candidate *is* Q4 --
        already a real, filed fact sitting in `qtr_df` with
        is_derived=False -- so no row is added here. Instead, records a
        reconciliation entry: what FY-(Q1+Q2+Q3) subtraction would have
        given for this year, and whether that diverges from the real
        filed Q4 by more than _Q4_RECONCILIATION_TOLERANCE of the FY
        total. This is a genuinely new signal, not a correction -- the
        real filed value was already in use before this function ever
        distinguished the 4-candidate case (see NOTES.md).
      - Any other candidate count, or a 4th candidate that doesn't tile
        cleanly to fy_end: refuses for that fiscal year, exactly as
        before -- no row, no reconciliation entry.

    Returns (derived, reconciliation):
      - derived: rows to concat onto qtr_df, same shape as a get_concept
        duration result plus "is_derived" -- unchanged from before this
        function gained the second path.
      - reconciliation: one row per fiscal year that took the 4-candidate
        path (populated whether or not it diverges, so a caller can see
        both numbers, not just a flag), columns "period_end" (the real Q4
        row's own period_end), "q4_subtraction_value" (float),
        "q4_diverges_from_subtraction" (bool). Always has these three
        columns with explicit dtypes (datetime64/float64/bool), even when
        empty -- an implicitly-typed empty DataFrame would merge as
        `object` dtype instead, which would then fail the bool/NaN
        normalization callers rely on.
    """
    tol = pd.Timedelta(days=_TILE_TOLERANCE_DAYS)
    derived_rows = []
    reconciliation_rows = []
    for fy in ann_df.itertuples():
        # Same tolerance _quarters_tile_fiscal_year uses on q1.period_start vs fy_start below --
        # a strict >= here would silently drop a Q1 that starts a day or two before fy_start
        # (a real EDGAR reporting quirk; see _dedupe_by_period_end's docstring) before the tiling
        # check ever runs, undercounting candidates and skipping a derivation it would have accepted.
        candidates = qtr_df[
            (qtr_df["period_start"] >= fy.period_start - tol) & (qtr_df["period_end"] <= fy.period_end)
        ].sort_values("period_start")

        if len(candidates) == 3:
            q1, q2, q3 = candidates.itertuples()
            if not _quarters_tile_fiscal_year(q1, q2, q3, fy.period_start, fy.period_end):
                continue
            derived_rows.append(
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
        elif len(candidates) == 4:
            q1, q2, q3, q4 = candidates.itertuples()
            if not _four_quarters_tile_fiscal_year(q1, q2, q3, q4, fy.period_start, fy.period_end):
                continue
            subtraction_value = fy.value - (q1.value + q2.value + q3.value)
            # A zero FY total can't produce a meaningful relative divergence -- default to "not
            # diverging" rather than raise or guess, matching is_derived's own default-False bias.
            diverges = fy.value != 0 and (
                abs(q4.value - subtraction_value) / abs(fy.value) > _Q4_RECONCILIATION_TOLERANCE
            )
            reconciliation_rows.append(
                {
                    "period_end": q4.period_end,
                    "q4_subtraction_value": float(subtraction_value),
                    "q4_diverges_from_subtraction": bool(diverges),
                }
            )
        # Any other candidate count -- refuse, no row, no reconciliation entry.

    derived = pd.DataFrame(derived_rows)
    reconciliation = pd.DataFrame(
        reconciliation_rows,
        columns=["period_end", "q4_subtraction_value", "q4_diverges_from_subtraction"],
    )
    reconciliation["period_end"] = pd.to_datetime(reconciliation["period_end"])
    reconciliation["q4_subtraction_value"] = reconciliation["q4_subtraction_value"].astype(float)
    reconciliation["q4_diverges_from_subtraction"] = reconciliation[
        "q4_diverges_from_subtraction"
    ].astype(bool)
    return derived, reconciliation


def _quarters_tile_fiscal_year(q1, q2, q3, fy_start, fy_end) -> bool:
    """
    True if q1/q2/q3 (sorted by period_start) are contiguous, non-
    overlapping, and start at fy_start with no gaps beyond
    _TILE_TOLERANCE_DAYS -- and the remaining implied Q4 span
    (q3.period_end+1 to fy_end) is itself a plausible quarter length, per
    _Q4_SPAN_DAYS_MIN/_Q4_SPAN_DAYS_MAX (see that constant's comment for why
    it's wider than concepts.py's classification bounds -- 52/53-week
    retail fiscal calendars give Q4 a real ~16-17 week span). Refusing here
    is what prevents a missing quarter, a restatement that shifted a period
    boundary, or a fiscal-calendar change from silently producing a wrong
    derived Q4 value.
    """
    tol = pd.Timedelta(days=_TILE_TOLERANCE_DAYS)

    if abs(q1.period_start - fy_start) > tol:
        return False
    if abs(q2.period_start - (q1.period_end + _ONE_DAY)) > tol:
        return False
    if abs(q3.period_start - (q2.period_end + _ONE_DAY)) > tol:
        return False

    implied_q4_days = (fy_end - (q3.period_end + _ONE_DAY)).days
    return _Q4_SPAN_DAYS_MIN <= implied_q4_days <= _Q4_SPAN_DAYS_MAX


def _four_quarters_tile_fiscal_year(q1, q2, q3, q4, fy_start, fy_end) -> bool:
    """
    True if q1/q2/q3/q4 (sorted by period_start) are contiguous, non-
    overlapping, start at fy_start, and q4's own period_end lands at
    fy_end -- all within _TILE_TOLERANCE_DAYS. q4 is itself a filed fact
    here, not an implied span, so (unlike _quarters_tile_fiscal_year)
    there's no span-plausibility check to make -- only a location check:
    does this real quarter actually cover the fiscal year's remainder.

    Deliberately a separate function from _quarters_tile_fiscal_year
    rather than a shared/generalized helper -- the two check structurally
    different things (3 real quarters plus an implied span that must fall
    in a plausible quarter-length range, vs. 4 real quarters where the
    last is itself a filed fact), and this project prefers duplication
    over a premature abstraction across them.
    """
    tol = pd.Timedelta(days=_TILE_TOLERANCE_DAYS)

    if abs(q1.period_start - fy_start) > tol:
        return False
    if abs(q2.period_start - (q1.period_end + _ONE_DAY)) > tol:
        return False
    if abs(q3.period_start - (q2.period_end + _ONE_DAY)) > tol:
        return False
    if abs(q4.period_start - (q3.period_end + _ONE_DAY)) > tol:
        return False

    return abs(q4.period_end - fy_end) <= tol
