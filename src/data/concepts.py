"""
Financial concept extraction from SEC EDGAR's companyfacts endpoint.

EDGAR's XBRL companyfacts API (https://data.sec.gov/api/xbrl/companyfacts/)
returns every fact a company has ever tagged, keyed by raw XBRL tag name —
not by a stable "revenue" or "net income" concept. Different companies (and
the same company at different times) tag the same real-world line item with
different tags: revenue in particular has several common tags spanning the
2018 ASC 606 revenue-recognition transition
(RevenueFromContractWithCustomerExcludingAssessedTax, Revenues,
SalesRevenueNet), plus SalesRevenueGoodsNet, used by at least one
consumer-goods company (Coca-Cola, confirmed 2009-2018) for its top-line
revenue pre-transition. CONCEPTS below maps plain concept names to a prioritized
list of tags to try. Companies routinely switch tags mid-history (e.g. NVDA
tagged revenue under RevenueFromContractWithCustomerExcludingAssessedTax
only in 10-Ks filed 2017-2022 as stale comparative columns, while its real
ongoing series — 10-K and 10-Q both, 2008 through today — is tagged
Revenues). Because of that, get_concept merges entries from *every* tag in
a concept's priority list rather than stopping at the first tag with any
data — using only the first-priority tag would silently truncate NVDA's
revenue at 2022 despite far more complete data existing under the fallback
tag. When two tags report the same period, the collision is resolved the
same way same-tag restatements are (see below): the entry with the later
"filed" date wins; if "filed" dates tie, the higher-priority tag wins. Each
row's "tag" column records which tag it actually came from — a merged
series can draw from more than one — and get_concept exposes which tags
contributed via df.attrs["tags_used"] / df.attrs["mixed_tags"] so a caller
can tell when a series isn't single-sourced.

EDGAR facts come in two shapes. "Instant" facts (balance sheet items like
Assets, Liabilities, cash) have only an "end" date. "Duration" facts (income
statement and cash flow items like Revenues, NetIncomeLoss) have both
"start" and "end". CONCEPTS declares which each concept is; that also
decides how periods are deduplicated (see below).

The same reporting period appears in companyfacts many times over — once in
the filing that originally reported it, and again as a comparative column in
every subsequent filing that shows prior-period figures, plus again if a
restatement (10-K/A, 10-Q/A) corrects it. get_concept collapses these by
(period start, period end) [duration] or (period end) [instant], always
keeping the entry with the latest "filed" date — the most authoritative
number for that period, including corrections from amendments.

Duration facts also aren't self-describing about length: EDGAR can report a
3-month quarterly fact, a 6/9-month year-to-date fact, and a 12-month annual
fact that all *end* on the same date but with different start dates. Those
are legitimately different (start, end) pairs, so dedup keeps all of them as
separate rows — but without classification a caller can't tell them apart,
so e.g. "revenue last quarter" could silently return a YTD figure instead.
get_concept classifies each duration row's period_length ("quarterly",
"annual", or "other" for YTD/stub periods) from its day span, and takes an
optional period_length filter.

The "Q4 problem": no company files a standalone Q4 report. 10-Qs cover Q1,
Q2, and Q3; the 10-K covers the full fiscal year (fp="FY"), not a discrete
fourth quarter. get_concept returns exactly what EDGAR reports — including
fiscal_period="FY" rows — and does not compute a derived Q4 by subtracting
Q1+Q2+Q3 from the FY total. A subtracted number isn't itself a filed fact,
and this project's design principle is that every number shown carries its
own source; deriving Q4 is analysis-layer work, not extraction.
"""

import pandas as pd

from .cik_lookup import get_cik
from .edgar_client import EdgarClient

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# 10-K/10-Q plus their amendments. Amendments are exactly the "later filed
# date, same period" restatements the latest-filed dedup below is meant to
# prefer, so excluding them would risk keeping a superseded number. 8-Ks
# (e.g. earnings press releases) are excluded.
_ALLOWED_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}

_QUARTERLY_DAYS_MIN, _QUARTERLY_DAYS_MAX = 80, 100
_ANNUAL_DAYS_MIN, _ANNUAL_DAYS_MAX = 350, 380

# Some 52/53-week retail fiscal calendars give one quarter of the year a genuinely longer span
# than the others (~111-125 days, i.e. 16-18 weeks) to absorb the leftover week(s) — confirmed on
# Costco (the extra week lands in Q4) and Kroger (the extra week lands in Q1). A fact this long is
# only classified "quarterly" if it's the *opening* period of a reporting cycle: see
# _reclassify_long_opening_quarters below for why day-span alone isn't a safe way to widen
# _QUARTERLY_DAYS_MAX itself (it would also swallow genuine 4-6 month YTD cumulative facts).
_LONG_OPENING_QUARTER_DAYS_MAX = 125
# Two "same start" or "contiguous" periods aren't always exactly back-to-back in EDGAR's own
# dates — see NOTES.md's one-day (and, confirmed across TGT/KR/HD/MSFT/KO, up to two-day)
# period_start drift between filings of different vintages for what's conceptually one period.
_PERIOD_DATE_TOLERANCE_DAYS = 2

CONCEPTS = {
    "revenue": {
        "tags": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "RevenuesNetOfInterestExpense",
        ],
        "kind": "duration",
    },
    "gross_profit": {"tags": ["GrossProfit"], "kind": "duration"},
    "operating_income": {"tags": ["OperatingIncomeLoss"], "kind": "duration"},
    "net_income": {
        "tags": [
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ],
        "kind": "duration",
    },
    "operating_cash_flow": {
        "tags": [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ],
        "kind": "duration",
    },
    "capex": {
        "tags": [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsForCapitalImprovements",
            "PaymentsToAcquireProductiveAssets",
        ],
        "kind": "duration",
    },
    "total_assets": {"tags": ["Assets"], "kind": "instant"},
    "total_liabilities": {"tags": ["Liabilities"], "kind": "instant"},
    "cash": {
        "tags": [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ],
        "kind": "instant",
    },
    "stockholders_equity": {
        "tags": [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ],
        "kind": "instant",
    },
    "current_assets": {"tags": ["AssetsCurrent"], "kind": "instant"},
    "current_liabilities": {"tags": ["LiabilitiesCurrent"], "kind": "instant"},
    "liabilities_noncurrent": {"tags": ["LiabilitiesNoncurrent"], "kind": "instant"},
}


class ConceptNotFoundError(Exception):
    """Raised when no tag in a concept's priority list has usable filing data."""


def get_company_facts(ticker: str, client: EdgarClient | None = None) -> dict:
    """Fetch (and cache, via EdgarClient) the raw companyfacts JSON for `ticker`."""
    client = client or EdgarClient()
    cik = get_cik(ticker, client=client)
    return client.get_json(COMPANYFACTS_URL.format(cik=cik))


def get_concept(
    ticker: str,
    concept_name: str,
    client: EdgarClient | None = None,
    period_length: str | None = None,
) -> pd.DataFrame:
    """
    Return a DataFrame of `concept_name` for `ticker`.

    Columns: period_end, value, fiscal_year, fiscal_period, form, filed, tag,
    and — for duration concepts only — period_start and period_length
    ("quarterly", "annual", or "other"). df.attrs["tags_used"] lists every
    tag that contributed a row; df.attrs["mixed_tags"] is True if more than
    one did (see module docstring re: merged tag selection).

    Merges entries from every tag in CONCEPTS[concept_name]["tags"] that has
    usable 10-K/10-Q/10-K-A/10-Q-A data — see module docstring for why this
    isn't a first-match-wins choice. Raises ConceptNotFoundError if no tag
    in the list has any data, and ValueError for an unknown concept_name.

    period_length: pass "quarterly" or "annual" to filter a duration
    concept to just that period length (see module docstring — EDGAR can
    report quarterly, YTD, and annual facts sharing the same period end
    date). Raises ValueError if passed for an instant concept, which has
    no duration to classify.
    """
    if concept_name not in CONCEPTS:
        raise ValueError(f"Unknown concept {concept_name!r}. Valid: {sorted(CONCEPTS)}")
    spec = CONCEPTS[concept_name]

    if period_length is not None:
        if spec["kind"] != "duration":
            raise ValueError(
                f"{concept_name!r} is an instant concept; period_length doesn't apply"
            )
        if period_length not in ("quarterly", "annual"):
            raise ValueError('period_length must be "quarterly" or "annual"')

    facts = get_company_facts(ticker, client=client)
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    tag_entries = []
    for priority, tag in enumerate(spec["tags"]):
        tag_facts = us_gaap.get(tag)
        if not tag_facts:
            continue
        units = tag_facts.get("units", {})
        raw = units.get("USD") or next(iter(units.values()), [])
        entries = [e for e in raw if e.get("form") in _ALLOWED_FORMS]
        if entries:
            tag_entries.append((priority, tag, entries))

    if not tag_entries:
        raise ConceptNotFoundError(
            f"No usable {concept_name!r} data for {ticker}; tried tags {spec['tags']}"
        )

    deduped = _merge_and_dedupe(tag_entries, spec["kind"])
    df = _build_dataframe(deduped, spec["kind"])
    if period_length is not None:
        df = df[df["period_length"] == period_length].reset_index(drop=True)

    tags_used = sorted(df["tag"].unique().tolist()) if len(df) else []
    df.attrs["tags_used"] = tags_used
    df.attrs["mixed_tags"] = len(tags_used) > 1
    return df


def _merge_and_dedupe(tag_entries: list[tuple[int, str, list[dict]]], kind: str) -> list[dict]:
    """
    Pool entries from every (priority, tag, entries) group and collapse
    repeated (start,end)/end periods to a single winner: the entry with the
    latest `filed` date, breaking ties in favor of the higher-priority tag
    (lower `priority`). Each surviving entry is annotated with `_tag`.
    """
    key = (lambda e: (e["start"], e["end"])) if kind == "duration" else (lambda e: e["end"])
    best: dict = {}
    for priority, tag, entries in tag_entries:
        for e in entries:
            k = key(e)
            candidate = {**e, "_tag": tag, "_priority": priority}
            current = best.get(k)
            if current is None or (candidate["filed"], -priority) > (
                current["filed"],
                -current["_priority"],
            ):
                best[k] = candidate
    return sorted(best.values(), key=lambda e: e["end"])


def _classify_period_length(start: str, end: str) -> str:
    """Bucket a duration fact by its day span: quarterly, annual, or other (YTD/stub)."""
    days = (pd.Timestamp(end) - pd.Timestamp(start)).days
    if _QUARTERLY_DAYS_MIN <= days <= _QUARTERLY_DAYS_MAX:
        return "quarterly"
    if _ANNUAL_DAYS_MIN <= days <= _ANNUAL_DAYS_MAX:
        return "annual"
    return "other"


def _reclassify_long_opening_quarters(deduped: list[dict], lengths: list[str]) -> list[str]:
    """
    Upgrade an "other"-classified fact to "quarterly" when it's a genuinely long
    (up to `_LONG_OPENING_QUARTER_DAYS_MAX` days) *opening* period of a reporting cycle — the
    52/53-week-calendar case documented on `_LONG_OPENING_QUARTER_DAYS_MAX` above.

    Widening `_QUARTERLY_DAYS_MAX` itself was considered and rejected: that bound is deliberately
    tight so a 6-/9-month year-to-date cumulative fact (routinely 180+ days) never gets mistaken
    for a real quarter. Contiguity ("does nothing else end right where this fact starts") was
    tried and rejected too: it doesn't actually distinguish an opening quarter from a closing one,
    because a fiscal calendar has no gaps — the *previous* fiscal year's annual fact ends exactly
    one day before *every* year's Q1 starts, so Q1 is always "contiguous" with something. The
    signal that does work is comparing against the annual fact for the same fiscal year: Q1 is the
    one period whose period_start coincides with the annual fact's own period_start (both begin
    the fiscal year), while a long *closing* quarter (Costco's Q4) shares the annual fact's
    period_end instead, starting well after the year began. So a candidate is only promoted when:

    - its period_start matches (within `_PERIOD_DATE_TOLERANCE_DAYS`) an annual-classified fact's
      period_start for this concept — i.e. it opens the fiscal year, not closes it; and
    - it's the shortest fact sharing that period_start — a YTD-through-Q2 fact also starts at the
      fiscal-year start (so also matches an annual fact's start) but is always longer than the
      real Q1 fact it shares that start with, so picking only the shortest member of each
      start-sharing group keeps a long YTD fact from qualifying just because it starts alongside a
      genuine long opening quarter.

    This deliberately leaves a long *closing* quarter (Costco's ~111-125-day Q4) classified
    "other" here — its real filed value is still surfaced quarterly via statements.py's
    discrete-Q4-fact path, which already handles exactly this case (see NOTES.md), and this
    function's job is only the opening-quarter gap that nothing else in the pipeline covers.
    """
    starts = [pd.Timestamp(e["start"]) for e in deduped]
    days = [(pd.Timestamp(e["end"]) - s).days for e, s in zip(deduped, starts)]
    tol = pd.Timedelta(days=_PERIOD_DATE_TOLERANCE_DAYS)
    annual_starts = [starts[j] for j, length in enumerate(lengths) if length == "annual"]

    result = list(lengths)
    for i, length in enumerate(lengths):
        if length != "other":
            continue
        d = days[i]
        if not (_QUARTERLY_DAYS_MAX < d <= _LONG_OPENING_QUARTER_DAYS_MAX):
            continue
        s = starts[i]
        opens_fiscal_year = any(abs(a - s) <= tol for a in annual_starts)
        if not opens_fiscal_year:
            continue
        is_shortest_of_start_family = not any(
            j != i and abs(starts[j] - s) <= tol and days[j] < d for j in range(len(deduped))
        )
        if is_shortest_of_start_family:
            result[i] = "quarterly"
    return result


def _build_dataframe(deduped: list[dict], kind: str) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "period_end": pd.to_datetime([e["end"] for e in deduped]),
            "value": [e["val"] for e in deduped],
            "fiscal_year": [e["fy"] for e in deduped],
            "fiscal_period": [e["fp"] for e in deduped],
            "form": [e["form"] for e in deduped],
            "filed": pd.to_datetime([e["filed"] for e in deduped]),
            "tag": [e["_tag"] for e in deduped],
        }
    )
    if kind == "duration":
        df.insert(1, "period_start", pd.to_datetime([e["start"] for e in deduped]))
        lengths = [_classify_period_length(e["start"], e["end"]) for e in deduped]
        df["period_length"] = _reclassify_long_opening_quarters(deduped, lengths)
    return df.sort_values("period_end").reset_index(drop=True)
