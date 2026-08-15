"""
Financial concept extraction from SEC EDGAR's companyfacts endpoint.

EDGAR's XBRL companyfacts API (https://data.sec.gov/api/xbrl/companyfacts/)
returns every fact a company has ever tagged, keyed by raw XBRL tag name —
not by a stable "revenue" or "net income" concept. Different companies (and
the same company at different times) tag the same real-world line item with
different tags: revenue in particular has three common tags spanning the
2018 ASC 606 revenue-recognition transition
(RevenueFromContractWithCustomerExcludingAssessedTax, Revenues,
SalesRevenueNet). CONCEPTS below maps plain concept names to a prioritized
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

CONCEPTS = {
    "revenue": {
        "tags": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
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
        df["period_length"] = [
            _classify_period_length(e["start"], e["end"]) for e in deduped
        ]
    return df.sort_values("period_end").reset_index(drop=True)
