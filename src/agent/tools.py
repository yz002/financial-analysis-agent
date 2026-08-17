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
    per-period values is null rather than silently missing.
    """
    ticker = ticker.upper()
    stmt, error = _get_statement_or_error(ticker, period_length, periods)
    if error is not None:
        return error

    unavailable = [c for c in ALL_CONCEPTS if stmt[c].isna().all()]
    notes = [_unavailable_note(c, ticker) for c in unavailable]

    periods_out = []
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
            period[concept] = entry
        periods_out.append(period)

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
    values are null and a note in "notes" explains why.
    """
    ticker = ticker.upper()
    if ratio_names is None:
        ratio_names = sorted(RATIOS)
    unknown = [r for r in ratio_names if r not in RATIOS]
    if unknown:
        return _error(
            ticker, "invalid_input", f"Unknown ratio name(s) {unknown}; valid names: {sorted(RATIOS)}"
        )

    stmt, error = _get_statement_or_error(ticker, period_length, periods)
    if error is not None:
        return error

    notes = []
    ratios_out = {}
    for name in ratio_names:
        func, concept_cols = RATIOS[name]
        for c in concept_cols:
            if stmt[c].isna().all():
                note = _unavailable_note(c, ticker, f" {name} cannot be computed.")
                if note not in notes:
                    notes.append(note)

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


def get_price_history_tool(ticker: str, start: str | None = None, end: str | None = None) -> str:
    """
    Return daily split/dividend-adjusted OHLCV price history for `ticker`
    (default: the trailing 90 days) via Yahoo Finance, as a JSON string.
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

    result = {
        "ticker": ticker,
        "start": start,
        "end": end,
        "note": "Split/dividend-adjusted daily OHLCV via Yahoo Finance (yfinance); not SEC-filed data.",
        "days_returned": len(prices),
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
            "explicitly in concepts_unavailable/notes rather than silently omitted."
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
                        f"Most recent N periods to return. Defaults to {DEFAULT_PERIODS}. "
                        "Pass null for full available history."
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
            "omitted."
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
                        "full available history."
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
        "name": "get_price_history",
        "description": (
            "Get daily split/dividend-adjusted OHLCV price history for a ticker, via Yahoo "
            "Finance."
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
    "get_price_history": get_price_history_tool,
}


def execute_tool(name: str, tool_input: dict) -> str:
    """
    Dispatch a tool call by name to its implementation, returning the
    already-JSON-serialized result string. Raises KeyError for an unknown
    tool name -- the agent loop catches that and surfaces it as a tool
    error result rather than raising through to the caller.
    """
    return _TOOL_FUNCTIONS[name](**tool_input)
