"""
Claude tool definitions wrapping the data and analysis layers.

Every tool here returns a compact, source-attributed JSON string -- never a
raw DataFrame. Per this project's core design principle (see CLAUDE.md),
these tools are the *only* way the agent layer touches numbers: the model
calls a tool, gets back a JSON result carrying every value's tag/filed
date/period/derived flag, and must never compute or restate a figure from
its own knowledge.

Absence is always legible. A concept a company doesn't report at all (e.g.
Ford has no gross_profit) is called out by name in a "notes"/error field
-- never an empty frame, never a silently-omitted key -- so the model can
tell the user "this isn't reported" instead of guessing or skipping it.
"""

import json
from datetime import datetime, timedelta

import pandas as pd

from ..analysis import forecast as forecast_mod
from ..analysis import ratios as ratios_mod
from ..analysis.statements import DURATION_CONCEPTS, INSTANT_CONCEPTS, get_statement
from ..analysis.trends import detect_anomalies as _detect_anomalies_raw
from ..analysis.trends import growth_anomalies
from ..data.concepts import ConceptNotFoundError
from ..data.market import (
    MarketDataError,
    get_current_quote,
    get_price_history,
    get_valuation_metrics,
)

DEFAULT_PERIODS = 8
# Bounds get_financial_statement/get_ratios payload size when periods is null (or a caller
# passes an oversized value) -- get_statement's own `periods` truncation happens after full-
# history Q4 derivation (see statements.py), so capping the request doesn't affect correctness,
# only how much of the tail gets returned. 40 quarters is ~10 years -- plenty for trend analysis
# -- and a full KO-style history (71 quarters) would otherwise run ~110KB/~27K tokens per call,
# resent on every subsequent turn of the agent loop.
MAX_PERIODS = 40
# Trading days beyond which get_price_history_tool downsamples to weekly bars instead of
# returning one row per day.
MAX_DAILY_PRICE_ROWS = 120
# A naive linear/growth/seasonal projection gets less trustworthy the further out it runs --
# this bounds how far forecast_metric_tool will project regardless of what a caller requests.
MAX_FORECAST_PERIODS_AHEAD = 8
ALL_CONCEPTS = DURATION_CONCEPTS + INSTANT_CONCEPTS

# ratio name -> (function, [statement concept columns it's computed from]).
# The concept list drives both provenance lookup and the "can't be computed
# because X isn't reported" note below.
RATIOS = {
    "gross_margin": (ratios_mod.gross_margin, ["gross_profit", "revenue"]),
    "operating_margin": (ratios_mod.operating_margin, ["operating_income", "revenue"]),
    "net_margin": (ratios_mod.net_margin, ["net_income", "revenue"]),
    "revenue_growth_qoq": (ratios_mod.revenue_growth_qoq, ["revenue"]),
    "revenue_growth_yoy": (ratios_mod.revenue_growth_yoy, ["revenue"]),
    "earnings_growth_qoq": (ratios_mod.earnings_growth_qoq, ["net_income"]),
    "earnings_growth_yoy": (ratios_mod.earnings_growth_yoy, ["net_income"]),
    "free_cash_flow": (ratios_mod.free_cash_flow, ["operating_cash_flow", "capex"]),
    "debt_to_assets": (ratios_mod.debt_to_assets, ["total_liabilities", "total_assets"]),
    "current_ratio": (ratios_mod.current_ratio, ["current_assets", "current_liabilities"]),
    "roa": (ratios_mod.roa, ["net_income", "total_assets"]),
    "roe": (ratios_mod.roe, ["net_income", "stockholders_equity"]),
}

# The stockholders_equity tag (see CONCEPTS["stockholders_equity"] in src/data/concepts.py)
# that includes noncontrolling/minority interests in the equity balance -- the other tag,
# StockholdersEquity, excludes them. get_ratios's roe-specific note below fires whenever this
# tag backed any period's equity figure, since it changes what "equity" (and therefore ROE)
# means. Duplicated here as a literal to keep this presentation-layer note independent of
# concepts.py's tag-priority-list internals.
_NCI_INCLUSIVE_EQUITY_TAG = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"


def _iso(value) -> str | None:
    """Timestamp/NaT -> 'YYYY-MM-DD' string, or None."""
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _num(value) -> float | None:
    """Any numeric-ish value (including NaN/None) -> a plain float, or None."""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _unavailable_note(concept: str, ticker: str, suffix: str = "") -> str:
    return f"No {concept} data available for {ticker}; this concept is not reported in its filings.{suffix}"


def _sparse_history_notes(stmt) -> list[str]:
    """Relay get_statement's sparse-history signal (df.attrs["sparse_history_note"]) as a tool
    note, when the resolved CIK's on-file history is implausibly thin -- possibly a successor
    registrant from a merger/reorganization/redomiciliation rather than a genuinely young filer
    (see statements.py's module docstring and NOTES.md's confirmed ExxonMobil case). Empty list
    when not sparse, so callers can unconditionally extend their notes list with this."""
    note = stmt.attrs.get("sparse_history_note")
    return [note] if note else []


def _error(ticker: str, error_type: str, message: str, **extra) -> str:
    """
    Build a tool-result error payload with a machine-checkable `error_type`
    so the model can tell these apart:
      - "data_unavailable" -- the company genuinely doesn't file this
        concept, or there isn't enough history for a computation. A fact
        to relay to the user, same as any other tool result.
      - "source_error" -- the underlying request (EDGAR, Yahoo Finance)
        itself failed (network, HTTP, malformed response). Not evidence
        the data doesn't exist -- the model must report the lookup failed
        rather than treating this like data_unavailable.
      - "invalid_input" -- the tool call itself was malformed (unknown
        ratio/metric name, bad mode). Fix the call and retry.
    """
    return json.dumps({"ticker": ticker, "error_type": error_type, "error": message, **extra})


def _cap_periods(periods: int | None) -> tuple[int, bool]:
    """
    Clamp a requested `periods` (None means "full history") to MAX_PERIODS.
    Returns (effective_periods, was_capped) -- was_capped reflects that the
    *request* asked for more than the cap, not whether the ticker actually
    had more periods than that on file.
    """
    if periods is None or periods > MAX_PERIODS:
        return MAX_PERIODS, True
    return periods, False


def _get_statement_or_error(ticker: str, period_length: str, periods: int | None):
    """
    get_statement, converted into a (stmt, error_json) pair so every tool
    built on it reports a typed error instead of letting an exception
    propagate to the agent loop:
      - ConceptNotFoundError / KeyError (unrecognized ticker, from
        cik_lookup.get_cik) -> "data_unavailable": EDGAR was reachable and
        answered definitively that there's nothing here.
      - anything else (network failure, HTTP error, malformed response,
        missing SEC_USER_AGENT, ...) -> "source_error": the lookup itself
        broke, which says nothing about whether the data exists.
    Returns (stmt, None) on success, (None, error_json_string) on failure.
    """
    try:
        return get_statement(ticker, period_length=period_length, periods=periods), None
    except ConceptNotFoundError as e:
        return None, _error(ticker, "data_unavailable", str(e))
    except KeyError as e:
        return None, _error(ticker, "data_unavailable", str(e).strip('"'))
    except Exception as e:
        return None, _error(ticker, "source_error", f"EDGAR lookup failed for {ticker}: {e}")


def get_financial_statement(
    ticker: str, period_length: str = "quarterly", periods: int | None = DEFAULT_PERIODS
) -> str:
    """
    Return `ticker`'s financial statement (all 12 tracked concepts, one row
    per period_end) as a JSON string. Every present value carries its
    source tag, filing date, and (for duration concepts) whether it was
    derived. A concept with zero usable data for this ticker is listed in
    "concepts_unavailable" with a plain-English note, and every one of its
    per-period values is null rather than silently missing. `periods`
    (including null, meaning "full history") is capped at MAX_PERIODS
    periods to bound response size -- pass a smaller `periods` and repeat
    the call to page through a longer history.

    When a fiscal year's Q4 is a real filed fact rather than a synthesized
    one, its entry may also carry "q4_subtraction_value" (what
    FY-Q1-Q2-Q3 subtraction would have given for the same year) and
    "q4_diverges_from_subtraction" (true if the two differ meaningfully) --
    present only when applicable. A divergence reflects a real vintage
    mismatch between the fiscal-year total and the quarters (see the
    "notes" entry when this occurs), not an error in either figure.

    If the resolved ticker's CIK has implausibly little on-file history for
    an established company (e.g. only a couple of quarters), "notes" names
    the resolved entity/CIK and flags that this may be a newly registered
    successor entity from a merger/reorganization/redomiciliation rather
    than a genuinely young company -- a predecessor CIK may hold the real
    history under a different registration (see statements.py's
    sparse-history detection; this tool never looks for or splices in a
    predecessor's data itself).
    """
    ticker = ticker.upper()
    effective_periods, capped = _cap_periods(periods)
    stmt, error = _get_statement_or_error(ticker, period_length, effective_periods)
    if error is not None:
        return error

    unavailable = [c for c in ALL_CONCEPTS if stmt[c].isna().all()]
    notes = [_unavailable_note(c, ticker) for c in unavailable]
    notes.extend(_sparse_history_notes(stmt))
    if capped:
        notes.append(
            f"Returning at most the most recent {MAX_PERIODS} periods to bound response size "
            f"(requested periods={periods!r}). Call again with a smaller `periods` for a "
            "narrower, uncapped window."
        )

    periods_out = []
    any_q4_divergence = False
    for row in stmt.itertuples():
        period = {
            "period_end": _iso(row.period_end),
            "period_start": _iso(getattr(row, "period_start", None)),
        }
        for concept in ALL_CONCEPTS:
            value = getattr(row, concept)
            if pd.isna(value):
                period[concept] = None
                continue
            entry = {
                "value": _num(value),
                "tag": getattr(row, f"{concept}_tag"),
                "filed": _iso(getattr(row, f"{concept}_filed")),
            }
            if concept in DURATION_CONCEPTS:
                entry["is_derived"] = bool(getattr(row, f"{concept}_is_derived"))
                q4_subtraction_value = getattr(row, f"{concept}_q4_subtraction_value")
                if not pd.isna(q4_subtraction_value):
                    entry["q4_subtraction_value"] = _num(q4_subtraction_value)
                    diverges = bool(getattr(row, f"{concept}_q4_diverges_from_subtraction"))
                    entry["q4_diverges_from_subtraction"] = diverges
                    if diverges:
                        any_q4_divergence = True
            period[concept] = entry
        periods_out.append(period)

    if any_q4_divergence:
        notes.append(
            "Some fiscal years' Q4 figures are directly filed values that differ from what "
            "Q1+Q2+Q3 subtracted from the fiscal-year total would give (see "
            "q4_subtraction_value/q4_diverges_from_subtraction on the relevant period). This is "
            "a real, filed number, not an error -- the divergence typically reflects the "
            "fiscal-year total being sourced from a later filing's restated comparative column "
            "under a possibly different tag than the quarters, which are filed together and "
            "don't get that later refresh."
        )

    result = {
        "ticker": ticker,
        "period_length": period_length,
        "periods_returned": len(periods_out),
        "concepts_unavailable": unavailable,
        "notes": notes,
        "periods": periods_out,
    }
    return json.dumps(result, indent=2)


def get_ratios(
    ticker: str,
    ratio_names: list[str] | None = None,
    period_length: str = "quarterly",
    periods: int | None = DEFAULT_PERIODS,
) -> str:
    """
    Compute the requested ratios (default: all of them) for `ticker` and
    return them as a JSON string. Each period's entry carries the computed
    value, the raw inputs, and provenance (tag/filed/is_derived) for every
    concept the ratio is built from. A ratio that can't be computed because
    an input concept isn't reported for this ticker still appears -- its
    values are null and a note in "notes" explains why. `periods`
    (including null, meaning "full history") is capped at MAX_PERIODS
    periods to bound response size. As with get_financial_statement,
    "notes" flags when the resolved ticker's CIK has implausibly little
    on-file history -- possibly a successor entity from a merger/
    reorganization/redomiciliation rather than a genuinely young company.
    "notes" also flags for `roe` when its stockholders_equity input was
    sourced (even partially) from the noncontrolling-interest-inclusive XBRL
    tag rather than the parent-only tag -- see each period's
    provenance.stockholders_equity.tag for the exact source.
    """
    ticker = ticker.upper()
    if ratio_names is None:
        ratio_names = sorted(RATIOS)
    unknown = [r for r in ratio_names if r not in RATIOS]
    if unknown:
        return _error(
            ticker, "invalid_input", f"Unknown ratio name(s) {unknown}; valid names: {sorted(RATIOS)}"
        )

    effective_periods, capped = _cap_periods(periods)
    stmt, error = _get_statement_or_error(ticker, period_length, effective_periods)
    if error is not None:
        return error

    notes = []
    notes.extend(_sparse_history_notes(stmt))
    if capped:
        notes.append(
            f"Returning at most the most recent {MAX_PERIODS} periods to bound response size "
            f"(requested periods={periods!r}). Call again with a smaller `periods` for a "
            "narrower, uncapped window."
        )
    ratios_out = {}
    for name in ratio_names:
        func, concept_cols = RATIOS[name]
        for c in concept_cols:
            if stmt[c].isna().all():
                note = _unavailable_note(c, ticker, f" {name} cannot be computed.")
                if note not in notes:
                    notes.append(note)

        if name in ("roa", "roe"):
            ratio_df = func(stmt, period_length=period_length)
            if period_length == "quarterly":
                note = (
                    f"{name} uses trailing-twelve-month net_income (current quarter + prior 3, "
                    "field 'net_income_ttm' in inputs below) rather than the single quarter's "
                    "figure, so it's comparable to a published annual ROA/ROE. Its "
                    "'net_income' provenance below is for the current quarter's contributing "
                    "filing only, not all four summed quarters. The first 3 periods have no "
                    "full trailing window yet, so their value is null."
                )
                if note not in notes:
                    notes.append(note)
            if name == "roe":
                equity_tags = set(stmt["stockholders_equity_tag"].dropna())
                if _NCI_INCLUSIVE_EQUITY_TAG in equity_tags:
                    if len(equity_tags) > 1:
                        note = (
                            "roe's stockholders_equity is sourced from two different XBRL tags "
                            "across this ticker's own reported history: some periods report "
                            "StockholdersEquity (excludes noncontrolling/minority interests) and "
                            "others report StockholdersEquityIncludingPortionAttributableTo"
                            "NoncontrollingInterest (includes them) -- check each period's "
                            "provenance.stockholders_equity.tag below. Even this ticker's own ROE "
                            "isn't on a consistent basis period to period, so a trend across its "
                            "history -- and any comparison to another company's ROE -- may reflect "
                            "a change in what's counted as equity rather than a real change in "
                            "profitability."
                        )
                    else:
                        note = (
                            "roe's stockholders_equity for this ticker is sourced from "
                            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest, "
                            "which includes noncontrolling/minority interests in the equity base -- "
                            "see provenance.stockholders_equity.tag below. A company whose equity "
                            "is instead sourced from the parent-only StockholdersEquity tag is on a "
                            "different basis, so comparing ROE directly across the two isn't "
                            "apples-to-apples."
                        )
                    if note not in notes:
                        notes.append(note)
        else:
            ratio_df = func(stmt)
        rows = []
        for srow, rrow in zip(stmt.itertuples(), ratio_df.itertuples()):
            inputs = {
                col: _num(getattr(rrow, col))
                for col in ratio_df.columns
                if col not in ("period_end", "value")
            }
            provenance = {}
            for c in concept_cols:
                prov = {
                    "tag": getattr(srow, f"{c}_tag"),
                    "filed": _iso(getattr(srow, f"{c}_filed")),
                }
                if hasattr(srow, f"{c}_is_derived"):
                    prov["is_derived"] = bool(getattr(srow, f"{c}_is_derived"))
                provenance[c] = prov
            rows.append(
                {
                    "period_end": _iso(rrow.period_end),
                    "value": _num(rrow.value),
                    "inputs": inputs,
                    "provenance": provenance,
                }
            )
        ratios_out[name] = rows

    result = {
        "ticker": ticker,
        "period_length": period_length,
        "notes": notes,
        "ratios": ratios_out,
    }
    return json.dumps(result, indent=2)


def get_market_data(ticker: str) -> str:
    """
    Return current quote (price, market cap, shares outstanding) and
    valuation metrics (trailing/forward P/E, price-to-sales, enterprise
    value, EV/EBITDA) for `ticker` via Yahoo Finance, as a JSON string.
    This is market data, not a filed figure -- it's timestamped by when the
    call ran, not by a filing date. If the ticker looks invalid or Yahoo has
    no data, or the lookup itself fails, the relevant section is null with
    a "{section}_error": {error_type, error} explanation rather than a
    raised exception -- error_type is "data_unavailable" for an
    invalid/unrecognized ticker, "source_error" for an underlying request
    failure.
    """
    ticker = ticker.upper()
    result = {
        "ticker": ticker,
        "source": "Yahoo Finance via yfinance (unofficial; not SEC-filed data)",
        "as_of": datetime.now().strftime("%Y-%m-%d"),
    }

    try:
        quote = get_current_quote(ticker)
        result["quote"] = {k: v for k, v in quote.items() if k != "ticker"}
    except MarketDataError as e:
        result["quote"] = None
        result["quote_error"] = {"error_type": "data_unavailable", "error": str(e)}
    except Exception as e:
        result["quote"] = None
        result["quote_error"] = {"error_type": "source_error", "error": f"Quote lookup failed for {ticker}: {e}"}

    try:
        valuation = get_valuation_metrics(ticker)
        result["valuation"] = {k: v for k, v in valuation.items() if k != "ticker"}
    except MarketDataError as e:
        result["valuation"] = None
        result["valuation_error"] = {"error_type": "data_unavailable", "error": str(e)}
    except Exception as e:
        result["valuation"] = None
        result["valuation_error"] = {
            "error_type": "source_error",
            "error": f"Valuation lookup failed for {ticker}: {e}",
        }

    return json.dumps(result, indent=2)


def detect_anomalies(
    ticker: str,
    metric: str,
    mode: str = "growth",
    window: int = 8,
    threshold: float = 2.0,
    lag: int = 1,
    period_length: str = "quarterly",
) -> str:
    """
    Flag statistically unusual periods for `metric` (a statement concept
    name) and return them as a JSON string. mode="growth" (default) runs
    detection on period-over-period growth rates, so a metric with a real,
    sustained high growth rate isn't flagged just for being consistently
    high -- only a break from its own trend is; mode="level" runs detection
    directly on raw values. See trends.py's module docstring for when each
    is the right tool.
    """
    ticker = ticker.upper()
    if metric not in ALL_CONCEPTS:
        return _error(ticker, "invalid_input", f"Unknown metric {metric!r}; valid metrics: {sorted(ALL_CONCEPTS)}")
    if mode not in ("level", "growth"):
        return _error(ticker, "invalid_input", 'mode must be "level" or "growth"')

    stmt, error = _get_statement_or_error(ticker, period_length, None)
    if error is not None:
        return error

    if stmt[metric].dropna().empty:
        return _error(ticker, "data_unavailable", _unavailable_note(metric, ticker), metric=metric)

    try:
        if mode == "growth":
            result_df = growth_anomalies(stmt, metric, window=window, threshold=threshold, lag=lag)
            value_description = f"{metric} period-over-period growth rate (lag={lag})"
        else:
            series = stmt.set_index("period_end")[metric].dropna()
            result_df = _detect_anomalies_raw(series, window=window, threshold=threshold)
            value_description = f"{metric} level"
    except ValueError as e:
        # Not enough history for a trailing baseline -- a data fact, not a source failure.
        return _error(ticker, "data_unavailable", str(e), metric=metric, mode=mode)

    periods_out = [
        {
            "period_end": _iso(period_end),
            "value": _num(row["value"]),
            "trailing_mean": _num(row["trailing_mean"]),
            "trailing_std": _num(row["trailing_std"]),
            "deviation_std": _num(row["deviation_std"]),
            "is_anomaly": bool(row["is_anomaly"]),
        }
        for period_end, row in result_df.iterrows()
    ]

    result = {
        "ticker": ticker,
        "metric": metric,
        "mode": mode,
        "value_description": value_description,
        "window": window,
        "threshold": threshold,
        "note": (
            "value/trailing_mean/trailing_std are computed from filed concept data, not "
            "themselves a single filed fact. is_anomaly flags points more than `threshold` "
            "trailing standard deviations from their own trailing baseline (which excludes "
            "the point itself, so no look-ahead)."
        ),
        "periods": periods_out,
    }
    return json.dumps(result, indent=2)


def forecast_metric_tool(
    ticker: str,
    column: str,
    periods_ahead: int = 2,
    method: str = "trend",
    period_length: str = "quarterly",
    lookback: int | None = None,
) -> str:
    """
    Project a statement concept `periods_ahead` periods into the future via
    src.analysis.forecast.forecast_metric -- the only source of a forward-
    looking number in this codebase (see CLAUDE.md: the model itself never
    computes or estimates a figure). Every successful result carries the
    method's fitted assumptions (slope/growth rate, historical periods
    used, fit quality, seasonal factors where relevant) in "assumptions",
    which must be relayed alongside any projected value -- these are not
    filed figures. When there isn't enough usable history, a gap in the
    period sequence, or a fit too poor to trust, "forecast_available" is
    false with a plain-English "reason" instead of a guessed number.
    """
    ticker = ticker.upper()
    if column not in ALL_CONCEPTS:
        return _error(
            ticker, "invalid_input", f"Unknown column {column!r}; valid columns: {sorted(ALL_CONCEPTS)}"
        )
    if method not in forecast_mod.METHODS:
        return _error(
            ticker, "invalid_input", f"Unknown method {method!r}; valid methods: {sorted(forecast_mod.METHODS)}"
        )
    if not (1 <= periods_ahead <= MAX_FORECAST_PERIODS_AHEAD):
        return _error(
            ticker,
            "invalid_input",
            f"periods_ahead must be between 1 and {MAX_FORECAST_PERIODS_AHEAD}, got {periods_ahead}",
        )

    stmt, error = _get_statement_or_error(ticker, period_length, MAX_PERIODS)
    if error is not None:
        return error

    if stmt[column].isna().all():
        return _error(ticker, "data_unavailable", _unavailable_note(column, ticker), column=column)

    try:
        result_df = forecast_mod.forecast_metric(
            stmt, column, periods_ahead=periods_ahead, method=method, lookback=lookback
        )
    except ValueError as e:
        return _error(ticker, "invalid_input", str(e))

    attrs = result_df.attrs
    result = {
        "ticker": ticker,
        "column": column,
        "period_length": period_length,
        "method": method,
        "periods_ahead": periods_ahead,
        "historical_periods_used": attrs["historical_periods_used"],
        "forecast_available": not attrs["refused"],
    }
    if attrs["refused"]:
        result["reason"] = attrs["reason"]
        result["projections"] = []
    else:
        result["assumptions"] = attrs["assumptions"]
        result["note"] = (
            "This is a projection computed deterministically from the assumptions above, not "
            "a filed figure or a prediction -- always state the method and assumptions when "
            "relaying a projected value."
        )
        result["projections"] = [
            {
                "period_end": _iso(row.period_end),
                "value": _num(row.value),
                "lower": _num(row.lower),
                "upper": _num(row.upper),
            }
            for row in result_df.itertuples()
        ]
    return json.dumps(result, indent=2)


def get_price_history_tool(ticker: str, start: str | None = None, end: str | None = None) -> str:
    """
    Return split/dividend-adjusted OHLCV price history for `ticker`
    (default: the trailing 90 days) via Yahoo Finance, as a JSON string.
    Daily bars beyond MAX_DAILY_PRICE_ROWS trading days are downsampled to
    weekly bars (Open=first, High=max, Low=min, Close=last, Volume=summed)
    to bound response size -- a long requested range no longer means
    dumping one row per day for years of history.
    """
    ticker = ticker.upper()
    if start is None:
        start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    try:
        df = get_price_history(ticker, start=start, end=end)
    except MarketDataError as e:
        return _error(ticker, "data_unavailable", str(e), start=start, end=end)
    except Exception as e:
        return _error(
            ticker, "source_error", f"Price history lookup failed for {ticker}: {e}", start=start, end=end
        )

    resolution = "daily"
    if len(df) > MAX_DAILY_PRICE_ROWS:
        df = df.resample("W").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna(how="all")
        resolution = "weekly"

    prices = [
        {
            "date": _iso(idx),
            "open": _num(row["Open"]),
            "high": _num(row["High"]),
            "low": _num(row["Low"]),
            "close": _num(row["Close"]),
            "volume": _num(row["Volume"]),
        }
        for idx, row in df.iterrows()
    ]

    note = "Split/dividend-adjusted OHLCV via Yahoo Finance (yfinance); not SEC-filed data."
    if resolution == "weekly":
        note += (
            f" Downsampled to weekly bars because the range exceeded {MAX_DAILY_PRICE_ROWS} "
            "trading days; pass a narrower start/end for daily granularity."
        )

    result = {
        "ticker": ticker,
        "start": start,
        "end": end,
        "resolution": resolution,
        "note": note,
        "rows_returned": len(prices),
        "prices": prices,
    }
    return json.dumps(result, indent=2)


TOOL_DEFINITIONS = [
    {
        "name": "get_financial_statement",
        "description": (
            "Get a company's financial statement from SEC EDGAR filings -- revenue, gross "
            "profit, operating income, net income, operating cash flow, capex, total assets, "
            "total liabilities, cash, stockholders' equity, current assets, current "
            "liabilities -- one row per period. Every value carries its source XBRL tag, the "
            "date it was filed, and (for income/cash-flow items) whether it was derived "
            "(synthesized Q4 = FY - Q1 - Q2 - Q3, marked is_derived=true). A concept the "
            "company doesn't report at all (e.g. Ford has no gross_profit) is called out "
            "explicitly in concepts_unavailable/notes rather than silently omitted. When a "
            "fiscal year's Q4 is instead a real filed fact, its entry may carry "
            "q4_subtraction_value (what FY-Q1-Q2-Q3 subtraction would have given) and "
            "q4_diverges_from_subtraction (true if the two differ meaningfully) -- present only "
            "when applicable; a divergence reflects a real vintage mismatch between the FY "
            "total and the quarters, not an error, and is also called out in notes when it "
            "occurs. If the resolved ticker's CIK has implausibly little on-file history for an "
            "established company, notes flags this as a possible successor-registrant case "
            "(e.g. after a merger, reorganization, or redomiciliation) rather than treating a "
            "short series as the company's whole history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker, e.g. 'MSFT'."},
                "period_length": {
                    "type": "string",
                    "enum": ["quarterly", "annual"],
                    "description": "Defaults to 'quarterly'.",
                },
                "periods": {
                    "type": ["integer", "null"],
                    "description": (
                        f"Most recent N periods to return. Defaults to {DEFAULT_PERIODS}. Pass "
                        f"null for the longest available history, capped at {MAX_PERIODS} most "
                        "recent periods to bound response size."
                    ),
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_ratios",
        "description": (
            "Compute financial ratios from a company's statement: gross_margin, "
            "operating_margin, net_margin, revenue_growth_qoq, revenue_growth_yoy, "
            "earnings_growth_qoq, earnings_growth_yoy, free_cash_flow, debt_to_assets, "
            "current_ratio, roa, roe. Every value includes the raw inputs and their source "
            "tag/filed date/derived flag. A ratio that can't be computed because an input "
            "concept isn't reported comes back null with an explanatory note, never silently "
            "omitted. Also flags implausibly little on-file history for the resolved CIK as a "
            "possible successor-registrant case, same as get_financial_statement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "ratio_names": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "enum": sorted(RATIOS)},
                    "description": "Which ratios to compute. Defaults to all of them if omitted.",
                },
                "period_length": {
                    "type": "string",
                    "enum": ["quarterly", "annual"],
                    "description": "Defaults to 'quarterly'.",
                },
                "periods": {
                    "type": ["integer", "null"],
                    "description": (
                        f"Most recent N periods. Defaults to {DEFAULT_PERIODS}. Pass null for "
                        f"the longest available history, capped at {MAX_PERIODS} most recent "
                        "periods to bound response size."
                    ),
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_market_data",
        "description": (
            "Get current price, market cap, shares outstanding, and valuation metrics "
            "(trailing/forward P/E, price-to-sales, enterprise value, EV/EBITDA) for a ticker, "
            "via Yahoo Finance. This is live market data, not an SEC filing figure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "detect_anomalies",
        "description": (
            "Flag statistically unusual periods for a statement metric (e.g. revenue, "
            "net_income, operating_cash_flow). mode='growth' (recommended default) runs "
            "detection on period-over-period growth rates, so a company with a real, "
            "sustained high growth rate (e.g. Nvidia) isn't flagged just for being "
            "consistently high-growth -- only a break from its own trend is. mode='level' runs "
            "detection directly on raw values, useful for metrics without a strong growth "
            "trend. Flags points more than `threshold` trailing standard deviations from their "
            "own trailing baseline (excludes the point itself)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "metric": {
                    "type": "string",
                    "enum": sorted(ALL_CONCEPTS),
                    "description": "Which statement concept to check.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["level", "growth"],
                    "description": "Defaults to 'growth'.",
                },
                "window": {
                    "type": "integer",
                    "description": "Trailing baseline window size, in periods. Defaults to 8.",
                },
                "threshold": {
                    "type": "number",
                    "description": "Standard deviations from baseline to flag. Defaults to 2.0.",
                },
                "lag": {
                    "type": "integer",
                    "description": (
                        "Only used when mode='growth': periods between growth comparisons "
                        "(1=QoQ, 4=YoY on quarterly data). Defaults to 1."
                    ),
                },
                "period_length": {
                    "type": "string",
                    "enum": ["quarterly", "annual"],
                    "description": "Defaults to 'quarterly'.",
                },
            },
            "required": ["ticker", "metric"],
        },
    },
    {
        "name": "forecast_metric",
        "description": (
            "Project a statement concept (e.g. revenue, net_income) forward using a "
            "deterministic model -- the only way to get a forward-looking figure from this "
            "system; there is no other forecasting capability. method='trend' fits a linear "
            "regression on recent history; method='growth' compounds the average recent "
            "period-over-period growth rate; method='seasonal' adds a fiscal-quarter seasonal "
            "adjustment on top of the trend (needs at least 8 quarters of quarterly-cadence "
            "history). Every result carries the fitted assumptions (slope or growth rate, how "
            "many historical periods were used, fit quality, seasonal factors) that must be "
            "relayed alongside any projected value. Refuses -- forecast_available=false with a "
            "plain-English reason -- rather than guess, when there isn't enough history, a gap "
            "in the period sequence, or a fit too poor to trust."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "column": {
                    "type": "string",
                    "enum": sorted(ALL_CONCEPTS),
                    "description": "Which statement concept to project.",
                },
                "periods_ahead": {
                    "type": "integer",
                    "description": (
                        f"How many future periods to project, 1-{MAX_FORECAST_PERIODS_AHEAD}. "
                        "Defaults to 2."
                    ),
                },
                "method": {
                    "type": "string",
                    "enum": list(forecast_mod.METHODS),
                    "description": "Defaults to 'trend'.",
                },
                "period_length": {
                    "type": "string",
                    "enum": ["quarterly", "annual"],
                    "description": "Defaults to 'quarterly'.",
                },
                "lookback": {
                    "type": ["integer", "null"],
                    "description": (
                        "How many of the most recent historical periods to fit on. Defaults to "
                        "8, capped by however much history is actually available."
                    ),
                },
            },
            "required": ["ticker", "column"],
        },
    },
    {
        "name": "get_price_history",
        "description": (
            f"Get split/dividend-adjusted OHLCV price history for a ticker, via Yahoo Finance. "
            f"Daily bars beyond {MAX_DAILY_PRICE_ROWS} trading days are downsampled to weekly "
            "bars to bound response size -- check the result's \"resolution\" field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "start": {
                    "type": ["string", "null"],
                    "description": "YYYY-MM-DD. Defaults to 90 days before today.",
                },
                "end": {"type": ["string", "null"], "description": "YYYY-MM-DD. Defaults to today."},
            },
            "required": ["ticker"],
        },
    },
]

_TOOL_FUNCTIONS = {
    "get_financial_statement": get_financial_statement,
    "get_ratios": get_ratios,
    "get_market_data": get_market_data,
    "detect_anomalies": detect_anomalies,
    "forecast_metric": forecast_metric_tool,
    "get_price_history": get_price_history_tool,
}

# Public so agent.py can check a tool name is known *before* calling execute_tool -- that lets
# it classify "unknown tool" as invalid_input up front, rather than relying on catching the
# KeyError execute_tool raises for it (which would be indistinguishable from a KeyError raised
# by an actual bug inside a tool function).
TOOL_NAMES = frozenset(_TOOL_FUNCTIONS)


def execute_tool(name: str, tool_input: dict) -> str:
    """
    Dispatch a tool call by name to its implementation, returning the
    already-JSON-serialized result string. Raises KeyError for an unknown
    tool name -- callers that haven't already checked `name in TOOL_NAMES`
    should expect that.
    """
    return _TOOL_FUNCTIONS[name](**tool_input)
