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

Cash-flow-statement concepts (operating_cash_flow, capex) are frequently filed as
fiscal-year-to-date cumulative facts (H1, 9-month) rather than discrete quarters -- these show up
as get_concept's period_length="other" rows and are invisible to a period_length="quarterly"
fetch. Where a genuine gap exists, this module also derives Q2 = H1 - Q1, Q3 = 9-month - H1, and,
as a last resort below the two Q4 paths described next, Q4 = FY - 9-month (see
_derive_ytd_quarters). Precedence for a fiscal year's Q4 value is: (1) a real filed Q4 fact, (2)
FY - (Q1+Q2+Q3) via three real filed quarters, (3) FY - 9-month via this YTD chain, tried only
when neither (1) nor (2) already produced a value. Q2/Q3 are derived independently of Q4 and of
each other, each from its own two adjacent real facts; a real filed Q2/Q3 fact always wins and is
never overwritten. Every derived row -- from either derivation path -- carries the same
"{concept}_is_derived" flag, plus a "{concept}_derivation_method" column ("q1q2q3_subtraction" or
"ytd_chain", None for a real or missing-data row) recording which path produced it. Unlike the
real-Q4-vs-subtraction case below, there is no reconciliation output for the YTD chain: a real
fact and a YTD-chain-derived value are never simultaneously computable for the same slot by
construction (derivation only runs when the slot is confirmed empty), so there's no "both
available" moment to cross-check. See NOTES.md.

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

total_liabilities gets this module's first derivation machinery for an *instant* (balance-sheet)
concept -- everything above is duration-concept-only. Some filers (confirmed: Walmart, across its
entire filing history) never report the rolled-up us-gaap:Liabilities tag at all, so
_derive_total_liabilities falls back, per period_end, to current_liabilities +
liabilities_noncurrent when both are present, then to the accounting identity
total_assets - stockholders_equity when both of those are present, refusing (leaving the value
absent) only if neither fallback's inputs are available. One convention differs from the
duration-concept columns above: total_liabilities_derivation_method can be "direct_tag" even
though total_liabilities_is_derived is False for that same row -- duration concepts never pair
is_derived=False with a non-None method. See _derive_total_liabilities's own docstring for the
full tier order, why the third tier is an accounting identity rather than a sum of individual
liability line items, and NOTES.md for the real tag-availability findings and the confirmed
divergence causes for the identity's own cross-check.
"""

import pandas as pd

from ..data.cik_lookup import get_cik, get_company_name
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

# Relative tolerance (fraction of the directly filed total_liabilities value) for flagging a
# divergence between that real value and the best available fallback cross-check
# (current_liabilities + liabilities_noncurrent, or total_assets - stockholders_equity). Mirrors
# _Q4_RECONCILIATION_TOLERANCE's value and reasoning: confirmed real divergences (MSFT
# period_end 2016-06-30 at ~9.1%, two NVDA periods at ~3.1%/0.8%, all via the assets-minus-equity
# identity) clear this threshold comfortably, while most periods checked against a filer with a
# direct tag agree with the identity to 0.000%. The root cause is the same phenomenon documented
# above for Q4 subtraction divergence -- each concept is deduped/tag-prioritized independently,
# so total_assets/stockholders_equity/total_liabilities for the same period_end can legitimately
# be sourced from different filings' restated comparative columns -- plus, for the identity
# specifically, an additive source: stockholders_equity resolving to the noncontrolling-interest-
# inclusive tag understates the identity's implied liabilities by the NCI amount (see
# stockholders_equity_tag on the relevant row, and NOTES.md).
_LIABILITIES_ALT_TOLERANCE = 0.005

_ONE_DAY = pd.Timedelta(days=1)

# Minimum period count below which a resolved CIK's on-file history is implausibly thin for what
# a caller would normally expect of an established company -- the signal this project uses to
# flag a likely SEC Rule 12g-3(a) "successor registrant" situation: a merger, reorganization, or
# redomiciliation creates a *new* CIK that a ticker's SEC mapping repoints to, which inherits the
# ticker but starts with none of the predecessor entity's XBRL filing history. Confirmed real
# case: ExxonMobil redomiciled from New Jersey to Texas on 2026-07-01, and SEC's ticker map now
# resolves XOM to "ExxonMobil Holdings Corp" (CIK 2115436), a registrant with only one 10-Q on
# file, while 15+ years of Exxon's real history sits under a predecessor CIK the ticker map no
# longer points at. This is a *class* of problem, not an XOM quirk -- any ticker can be repointed
# this way after a corporate restructuring. See NOTES.md. Annual mode uses a lower bound than
# quarterly (3 vs. 8) since annual filings are inherently sparser -- a genuinely long-filing
# company still clears 3 annual periods easily, while a fresh successor registrant won't.
_MIN_PLAUSIBLE_PERIODS = {"quarterly": 8, "annual": 3}

# --- YTD-chain quarter derivation (Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M) -------------------
#
# H1/9-month candidate day-span bounds, for recognizing an already get_concept-"other"-classified
# YTD cumulative fact by its multi-quarter span. Deliberately a separate local constant from
# concepts._QUARTERLY_DAYS_MIN/_QUARTERLY_DAYS_MAX (80-100) rather than importing those -- that
# pair classifies a single reported fact and must stay tight so a real 6-/9-month YTD fact is
# never misread as a quarter; these bounds instead recognize a fact *already* bucketed "other" by
# its span, a different job. Roughly 2x/3x the 80-100 quarterly window, with enough margin
# (+/-18 and +/-27 days) to absorb a 52/53-week fiscal calendar's extra week landing anywhere
# inside H1/9M.
_H1_SPAN_DAYS_MIN, _H1_SPAN_DAYS_MAX = 160, 200
_NINE_MONTH_SPAN_DAYS_MIN, _NINE_MONTH_SPAN_DAYS_MAX = 240, 300

# Bound for an *implied* Q2/Q3 span (H1.end - Q1.end, or 9M.end - H1.end). Unlike
# _Q4_SPAN_DAYS_MIN/_Q4_SPAN_DAYS_MAX above, this stays as tight as concepts.py's own quarterly
# classification bounds (80-100) rather than widened: Q2/Q3 are always a fiscal year's *middle*
# quarters, and the 52/53-week elongation that justifies _Q4_SPAN_DAYS_MAX's wider range only
# ever hits the fiscal year's opening (Kroger) or closing (Costco) quarter -- confirmed via
# concepts._reclassify_long_opening_quarters' docstring -- never a middle one, so there's no
# legitimate case where a middle quarter needs the wider tolerance.
_MID_QUARTER_SPAN_DAYS_MIN, _MID_QUARTER_SPAN_DAYS_MAX = 80, 100


def get_statement(
    ticker: str,
    period_length: str = "quarterly",
    periods: int | None = None,
    client: EdgarClient | None = None,
) -> pd.DataFrame:
    """
    Return one wide financial statement DataFrame for `ticker`, one row per
    period_end, joining all 13 concepts in CONCEPTS.

    Columns: period_end, period_start, and per concept: "{concept}" (value),
    "{concept}_tag" (source XBRL tag, or "derived" for any synthesized
    row -- Q4-by-subtraction, a YTD-chain-derived Q2/Q3/Q4, or a
    total_liabilities fallback), "{concept}_filed"
    (the filing date backing that value), and -- for duration concepts only --
    "{concept}_is_derived" (bool, always literal True/False, never NaN),
    "{concept}_derivation_method" ("q1q2q3_subtraction", "ytd_chain", or None
    for a real or missing-data row -- see module docstring),
    "{concept}_q4_subtraction_value" (float, NaN unless this row is a real
    filed Q4 fact being cross-checked against subtraction -- see module
    docstring), and "{concept}_q4_diverges_from_subtraction" (bool, always
    literal True/False, never NaN). A concept with no usable data for this
    ticker still gets these columns, filled with NaN/None/False.

    total_liabilities alone additionally carries "total_liabilities_is_derived",
    "total_liabilities_derivation_method" ("direct_tag",
    "current_plus_noncurrent_sum", "assets_minus_equity_identity", or None),
    "total_liabilities_alt_value" (float, NaN unless a direct-tag row also had a
    fallback available to cross-check against), "total_liabilities_alt_method"
    (same three method strings, or None), and
    "total_liabilities_diverges_from_alt" (bool, always literal, never NaN) --
    see module docstring and _derive_total_liabilities.

    period_length: "quarterly" (default) or "annual". In "quarterly" mode,
    a Q4 row per fiscal year is synthesized for duration concepts where the
    real Q1/Q2/Q3 quarters and the FY total are available and their periods
    reconcile (see module docstring); instant concepts (balance-sheet
    items) need no such synthesis since the 10-K already reports them at
    fiscal-year-end. When a real Q4 fact already exists instead, it's used
    directly and cross-checked against subtraction (see module docstring)
    rather than synthesized. Cash-flow-statement concepts filed as H1/9-month
    cumulative facts instead of discrete quarters get Q2/Q3, and Q4 as a last
    resort, derived from those YTD facts when the corresponding discrete
    quarter is otherwise unavailable (see module docstring).

    periods: if given, return only the most recent `periods` rows (applied
    after assembling full history, since Q4 derivation for a recent quarter
    can depend on an FY row older than the truncation window).

    df.attrs carries entity/history-depth metadata not tied to any one row:
    "entity_name" and "cik" (the resolved registrant, from SEC's ticker
    map), "periods_available" (count of distinct period_end values across
    the *full* assembled history, before the `periods` truncation above),
    "sparse_history" (bool -- True if periods_available is implausibly low
    for period_length, see `_MIN_PLAUSIBLE_PERIODS`), and
    "sparse_history_note" (a plain-English explanation naming the resolved
    entity/CIK, or None when not sparse) -- see `_is_sparse_history`/
    `_sparse_history_note` and NOTES.md's confirmed ExxonMobil case for what
    this is meant to catch: SEC's ticker map repointing to a newly
    registered successor entity after a merger/reorganization/
    redomiciliation, which inherits the ticker but not the predecessor's
    filing history.

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
            qtr_df["derivation_method"] = None
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

                # YTD-chain derivation (Q2 = H1-Q1, Q3 = 9M-H1, Q4 = FY-9M as a last resort) runs
                # after _derive_q4 above, so it can see whether that already resolved this fiscal
                # year's Q4 -- see _derive_ytd_quarters' docstring for the precedence ordering.
                ytd_df = _try_get_ytd_concept(ticker, concept, client)
                if ytd_df is not None:
                    ytd_derived = _derive_ytd_quarters(qtr_df, ytd_df, ann_df)
                    if len(ytd_derived):
                        qtr_df = pd.concat([qtr_df, ytd_derived], ignore_index=True)
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
            statement[f"{concept}_derivation_method"] = None
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

    statement = _derive_total_liabilities(statement)

    statement = statement.sort_values("period_end").reset_index(drop=True)

    periods_available = len(period_ends)
    entity_name = get_company_name(ticker, client=client) or ticker.upper()
    cik = get_cik(ticker, client=client)
    sparse = _is_sparse_history(period_length, periods_available)
    statement.attrs["entity_name"] = entity_name
    statement.attrs["cik"] = cik
    statement.attrs["periods_available"] = periods_available
    statement.attrs["sparse_history"] = sparse
    statement.attrs["sparse_history_note"] = (
        _sparse_history_note(ticker, cik, entity_name, period_length, periods_available)
        if sparse
        else None
    )

    if periods is not None:
        statement = statement.tail(periods).reset_index(drop=True)
    return statement


def _is_sparse_history(period_length: str, periods_available: int) -> bool:
    """True if `periods_available` falls below the plausible-minimum for `period_length` -- see
    `_MIN_PLAUSIBLE_PERIODS` above for what this is meant to catch and why."""
    return periods_available < _MIN_PLAUSIBLE_PERIODS[period_length]


def _sparse_history_note(
    ticker: str, cik: str, entity_name: str, period_length: str, periods_available: int
) -> str:
    """Plain-English explanation of a sparse-history finding, naming the actually-resolved entity
    and CIK so a caller (ultimately the agent, then the user) can tell this apart from the ticker
    genuinely being an unrecognized/bad company -- see `_MIN_PLAUSIBLE_PERIODS` above and
    NOTES.md's confirmed ExxonMobil case. Deliberately does not attempt to locate or splice in a
    predecessor CIK's data: silently combining two distinct legal entities' filings into one
    series would violate this project's provenance principle (every number traces to one filing
    from one registrant) even if the combined series looked more complete."""
    return (
        f"{ticker.upper()} resolves to CIK {cik} ({entity_name}), which has only "
        f"{periods_available} {period_length} period(s) of history in SEC EDGAR -- implausibly "
        "little for an established company. This commonly happens when SEC's ticker-to-CIK "
        "mapping points at a newly registered successor entity (e.g. a merger, reorganization, "
        "or redomiciliation creating a new registrant under SEC Rule 12g-3(a)) rather than the "
        "company's original, longer-filing registrant. A predecessor CIK may hold the company's "
        "earlier financial history under a different registration -- this tool does not look for "
        "or splice in a predecessor's data, since combining two distinct legal entities' filings "
        "into one series without saying so would contradict this project's source-provenance "
        "principle."
    )


def _try_get_concept(
    ticker: str, concept: str, period_length: str | None, client: EdgarClient
) -> pd.DataFrame | None:
    """get_concept, returning None instead of raising when this ticker has no usable data."""
    try:
        return get_concept(ticker, concept, client=client, period_length=period_length)
    except ConceptNotFoundError:
        return None


def _try_get_ytd_concept(ticker: str, concept: str, client: EdgarClient) -> pd.DataFrame | None:
    """
    Real filed fiscal-year-to-date cumulative facts for `concept` -- get_concept's
    period_length="other" bucket (6-/9-month YTD figures; most common for cash-flow-statement
    concepts, see module docstring), deduped to one row per period_end the same way qtr_df/ann_df
    already are. get_concept doesn't accept period_length="other" directly (only
    "quarterly"/"annual"/None -- see its docstring), so this fetches everything unfiltered and
    filters to "other" locally. Returns None if this ticker has no usable "other"-classified data
    for this concept at all.
    """
    df = _try_get_concept(ticker, concept, None, client)
    if df is None:
        return None
    df = df[df["period_length"] == "other"]
    if df.empty:
        return None
    return _dedupe_by_period_end(df)


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
    derivation_method = df["derivation_method"] if "derivation_method" in df.columns else None
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
            f"{concept}_derivation_method": derivation_method,
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
                    "derivation_method": "q1q2q3_subtraction",
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


def _select_ytd_candidate(
    ytd_df: pd.DataFrame, fy_start: pd.Timestamp, span_min: int, span_max: int
):
    """
    Return the single "other"-classified duration row in `ytd_df` whose period_start matches
    `fy_start` (within _TILE_TOLERANCE_DAYS) and whose day-span falls in [span_min, span_max] --
    i.e. a candidate H1 or 9-month year-to-date cumulative fact opening this fiscal year. Returns
    None, refusing to guess, if zero or more than one row qualifies -- e.g. a stub period from a
    fiscal-calendar transition that happens to also land in the same span bucket as the real fact.
    """
    tol = pd.Timedelta(days=_TILE_TOLERANCE_DAYS)
    days = (ytd_df["period_end"] - ytd_df["period_start"]).dt.days
    mask = (ytd_df["period_start"] - fy_start).abs() <= tol
    mask &= (days >= span_min) & (days <= span_max)
    matches = ytd_df[mask]
    if len(matches) != 1:
        return None
    return matches.iloc[0]


def _derive_ytd_quarters(
    qtr_df: pd.DataFrame, ytd_df: pd.DataFrame, ann_df: pd.DataFrame
) -> pd.DataFrame:
    """
    Derive Q2/Q3, and Q4 as a last resort, from real filed fiscal-year-to-date cumulative facts:
    Q2 = H1 - Q1, Q3 = 9-month - H1, Q4 = FY - 9-month. Targets the coverage gap documented in
    the module docstring and NOTES.md -- cash-flow-statement concepts (operating_cash_flow,
    capex) are routinely filed as H1/9-month cumulative facts rather than discrete quarters,
    which get_concept's period_length="quarterly" filter correctly excludes (they're genuinely
    not quarterly facts) but which this module previously had no way to recover a discrete
    quarter from.

    `qtr_df` is expected to be the frame *after* _derive_q4's own output has already been
    concatenated onto it -- Q4-via-9-month only fires for a fiscal year where neither a real
    filed Q4 fact nor Q1+Q2+Q3 subtraction already produced one (see module docstring's
    precedence ordering). Q2/Q3 similarly only fill a genuinely empty slot: a real filed
    quarterly fact for that period always wins and is never overwritten or duplicated. "Already
    covered" is checked by period_end match -- a discrete quarter and its corresponding YTD
    cumulative fact always share the same period_end by calendar definition -- rather than by
    positionally chaining through Q1/Q2/Q3.

    Each of Q2, Q3, and Q4-via-9-month is derived independently per fiscal year from its own two
    adjacent real facts (never chained through another *derived* value -- Q3 uses the real H1
    fact, not a derived Q2) and independently refuses (skips that slot, that year) if its inputs
    are missing, ambiguous, or don't plausibly tile -- the same "refuse rather than guess"
    discipline as _derive_q4. Unlike _derive_q4, there is no reconciliation/cross-check output: a
    real fact and one of these derived values are never simultaneously available for the same
    slot by construction (derivation only runs when the slot is confirmed empty), so there is no
    "both available, do they agree" case to reconcile -- see NOTES.md.

    Returns a DataFrame of new rows only (period_end, period_start, value, fiscal_year,
    fiscal_period, form, filed, tag, period_length, is_derived, derivation_method), meant to be
    concatenated onto qtr_df by the caller exactly like _derive_q4's `derived` return value.
    """
    tol = pd.Timedelta(days=_TILE_TOLERANCE_DAYS)
    real_qtr_df = qtr_df[~qtr_df["is_derived"]]
    derived_rows = []

    for fy in ann_df.itertuples():
        h1 = _select_ytd_candidate(ytd_df, fy.period_start, _H1_SPAN_DAYS_MIN, _H1_SPAN_DAYS_MAX)
        nine_month = _select_ytd_candidate(
            ytd_df, fy.period_start, _NINE_MONTH_SPAN_DAYS_MIN, _NINE_MONTH_SPAN_DAYS_MAX
        )

        # Q2 = H1 - Q1: needs exactly one real filed fact opening this fiscal year (Q1) to
        # subtract, and no real fact already covering H1's own period_end.
        if h1 is not None:
            q1_candidates = real_qtr_df[(real_qtr_df["period_start"] - fy.period_start).abs() <= tol]
            q2_already_real = ((real_qtr_df["period_end"] - h1.period_end).abs() <= tol).any()
            if len(q1_candidates) == 1 and not q2_already_real:
                q1 = q1_candidates.iloc[0]
                implied_start = q1.period_end + _ONE_DAY
                implied_days = (h1.period_end - implied_start).days
                if _MID_QUARTER_SPAN_DAYS_MIN <= implied_days <= _MID_QUARTER_SPAN_DAYS_MAX:
                    derived_rows.append(
                        {
                            "period_end": h1.period_end,
                            "period_start": implied_start,
                            "value": h1.value - q1.value,
                            "fiscal_year": fy.fiscal_year,
                            "fiscal_period": "Q2",
                            "form": h1.form,
                            "filed": max(q1.filed, h1.filed),
                            "tag": "derived",
                            "period_length": "quarterly",
                            "is_derived": True,
                            "derivation_method": "ytd_chain",
                        }
                    )

        # Q3 = 9-month - H1: needs the real filed H1 cumulative fact itself (not a derived Q2),
        # and no real fact already covering the 9-month fact's own period_end.
        if h1 is not None and nine_month is not None:
            q3_already_real = ((real_qtr_df["period_end"] - nine_month.period_end).abs() <= tol).any()
            implied_start = h1.period_end + _ONE_DAY
            implied_days = (nine_month.period_end - implied_start).days
            if (
                not q3_already_real
                and _MID_QUARTER_SPAN_DAYS_MIN <= implied_days <= _MID_QUARTER_SPAN_DAYS_MAX
            ):
                derived_rows.append(
                    {
                        "period_end": nine_month.period_end,
                        "period_start": implied_start,
                        "value": nine_month.value - h1.value,
                        "fiscal_year": fy.fiscal_year,
                        "fiscal_period": "Q3",
                        "form": nine_month.form,
                        "filed": max(h1.filed, nine_month.filed),
                        "tag": "derived",
                        "period_length": "quarterly",
                        "is_derived": True,
                        "derivation_method": "ytd_chain",
                    }
                )

        # Q4 = FY - 9-month: lowest precedence -- only when neither a real filed Q4 fact nor
        # _derive_q4's Q1+Q2+Q3 subtraction already produced a value at this fiscal year's
        # period_end. qtr_df here already has _derive_q4's output concatenated on, so checking
        # the *full* frame (real or derived, either counts as "resolved") is deliberate --
        # unlike the real_qtr_df-only checks above for Q2/Q3, where nothing else could ever have
        # produced a value.
        if nine_month is not None:
            q4_already_resolved = ((qtr_df["period_end"] - fy.period_end).abs() <= tol).any()
            implied_start = nine_month.period_end + _ONE_DAY
            implied_days = (fy.period_end - implied_start).days
            if (
                not q4_already_resolved
                and _Q4_SPAN_DAYS_MIN <= implied_days <= _Q4_SPAN_DAYS_MAX
            ):
                derived_rows.append(
                    {
                        "period_end": fy.period_end,
                        "period_start": implied_start,
                        "value": fy.value - nine_month.value,
                        "fiscal_year": fy.fiscal_year,
                        "fiscal_period": "Q4",
                        "form": fy.form,
                        "filed": max(nine_month.filed, fy.filed),
                        "tag": "derived",
                        "period_length": "quarterly",
                        "is_derived": True,
                        "derivation_method": "ytd_chain",
                    }
                )

    return pd.DataFrame(derived_rows)


def _derive_total_liabilities(statement: pd.DataFrame) -> pd.DataFrame:
    """
    Resolve total_liabilities per period_end through three tiers, no partial credit at any tier
    -- a period missing one of a tier's two required inputs moves to the next tier, never
    computes a one-sided value:

      (a) The real, directly filed Liabilities tag, if present -- used as-is.
          derivation_method="direct_tag", is_derived=False. This is *not* a redefinition of
          is_derived's usual "was this row synthesized" meaning: "direct_tag" is a real filed
          value like any other concept's tag/value columns. The label exists only because,
          unlike every other concept, total_liabilities can also be filled by fallback below, so
          derivation_method needs a value even in the real-tag case (contrast with duration
          concepts, where is_derived=False always pairs with derivation_method=None).
      (b) current_liabilities + liabilities_noncurrent, only when BOTH are present for this
          period_end. derivation_method="current_plus_noncurrent_sum", is_derived=True,
          tag="derived" (mirroring _derive_q4's convention), filed=the later of the two inputs'
          filed dates. In practice this rarely fires: LiabilitiesNoncurrent is absent from every
          real filer checked while building this (MSFT, NVDA, Ford, WMT, KR, JPMorgan, BofA
          Finance, Berkshire, GE, plus live-checked Ally and Schwab) -- kept because it's cheap
          and correct on the rare filer that does report both.
      (c) total_assets - stockholders_equity (Assets = Liabilities + Equity), only when BOTH are
          present. derivation_method="assets_minus_equity_identity", is_derived=True,
          tag="derived", filed=the later of the two inputs' filed dates. This is the tier that
          recovers Walmart's total_liabilities: WMT has zero us-gaap:Liabilities facts across its
          entire filing history and no LiabilitiesNoncurrent either, but total_assets and
          stockholders_equity are both reliably reported. Deliberately the accounting identity
          rather than a sum of individual liability line items (accounts payable, accrued
          liabilities, long-term debt, deferred tax liabilities, ...): there is no way to prove
          an arbitrary filer's liability tags have been enumerated *completely*, so a partial sum
          could masquerade as "total liabilities" while understating it -- exactly the kind of
          wrong-but-plausible number this project already refuses to produce (see the Q4-tiling
          refusal above). Assets - Equity is definitionally exhaustive, a rearrangement of the
          fundamental accounting equation rather than an enumeration, so there's no "did we miss
          a part" question. See NOTES.md.
      (d) Otherwise: refuse. total_liabilities/_tag/_filed are left exactly as they came in
          (NaN/None/NaT), derivation_method=None, is_derived=False.

    When tier (a) is what filled a row, this also opportunistically computes the best available
    *alternative* purely for cross-checking -- tier (b)'s sum if both its inputs are present,
    else tier (c)'s identity if both its inputs are present, else nothing -- without ever
    overriding the real filed value:
      - total_liabilities_alt_value (float, NaN = not computable)
      - total_liabilities_alt_method ("current_plus_noncurrent_sum" /
        "assets_minus_equity_identity", or None)
      - total_liabilities_diverges_from_alt (bool, always literal, default False): True when the
        alt value differs from the real filed value by more than _LIABILITIES_ALT_TOLERANCE of
        the real value. A zero real value can't produce a meaningful relative divergence --
        defaults to False, same guard _derive_q4 uses for a zero FY total. A True here is not
        necessarily an error in either number -- see _LIABILITIES_ALT_TOLERANCE's comment and
        NOTES.md for the two confirmed real causes (cross-filing restatement-vintage mismatch;
        an NCI-inclusive stockholders_equity tag).

    Every row gets all five new columns set -- never left unset -- mirroring this module's
    existing convention that a derived-value flag is always a real bool/None, never NaN.
    """
    statement = statement.copy()
    n = len(statement)

    direct = statement["total_liabilities"]
    cur = statement["current_liabilities"]
    noncur = statement["liabilities_noncurrent"]
    assets = statement["total_assets"]
    equity = statement["stockholders_equity"]
    cur_filed = statement["current_liabilities_filed"]
    noncur_filed = statement["liabilities_noncurrent_filed"]
    assets_filed = statement["total_assets_filed"]
    equity_filed = statement["stockholders_equity_filed"]

    new_value = list(direct)
    new_tag = list(statement["total_liabilities_tag"])
    new_filed = list(statement["total_liabilities_filed"])
    is_derived = [False] * n
    method: list = [None] * n
    alt_value = [float("nan")] * n
    alt_method: list = [None] * n
    diverges = [False] * n

    for i in range(n):
        if pd.notna(direct.iloc[i]):
            method[i] = "direct_tag"
            alt = None
            if pd.notna(cur.iloc[i]) and pd.notna(noncur.iloc[i]):
                alt, alt_method[i] = cur.iloc[i] + noncur.iloc[i], "current_plus_noncurrent_sum"
            elif pd.notna(assets.iloc[i]) and pd.notna(equity.iloc[i]):
                alt, alt_method[i] = assets.iloc[i] - equity.iloc[i], "assets_minus_equity_identity"
            if alt is not None:
                alt_value[i] = float(alt)
                real = direct.iloc[i]
                diverges[i] = bool(
                    real != 0 and abs(alt - real) / abs(real) > _LIABILITIES_ALT_TOLERANCE
                )
            continue

        if pd.notna(cur.iloc[i]) and pd.notna(noncur.iloc[i]):
            new_value[i] = cur.iloc[i] + noncur.iloc[i]
            new_tag[i] = "derived"
            new_filed[i] = max(cur_filed.iloc[i], noncur_filed.iloc[i])
            method[i] = "current_plus_noncurrent_sum"
            is_derived[i] = True
            continue

        if pd.notna(assets.iloc[i]) and pd.notna(equity.iloc[i]):
            new_value[i] = assets.iloc[i] - equity.iloc[i]
            new_tag[i] = "derived"
            new_filed[i] = max(assets_filed.iloc[i], equity_filed.iloc[i])
            method[i] = "assets_minus_equity_identity"
            is_derived[i] = True
            continue
        # else: refuse -- leave new_value[i]/new_tag[i]/new_filed[i] as the original NaN/None/NaT.

    statement["total_liabilities"] = new_value
    statement["total_liabilities_tag"] = new_tag
    statement["total_liabilities_filed"] = pd.to_datetime(new_filed)
    statement["total_liabilities_is_derived"] = pd.array(is_derived, dtype=bool)
    statement["total_liabilities_derivation_method"] = method
    statement["total_liabilities_alt_value"] = alt_value
    statement["total_liabilities_alt_method"] = alt_method
    statement["total_liabilities_diverges_from_alt"] = pd.array(diverges, dtype=bool)
    return statement
