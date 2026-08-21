"""
Independently computed expected figures for the eval questions that have a concrete right
answer -- calling src.data/src.analysis directly, the same way the agent's own tools do, rather
than trusting the agent's tool results. This is what lets scoring catch a *grounded but wrong*
figure (e.g. citing last year's revenue instead of last quarter's for a real tool value) and not
just an ungrounded one -- grounding is guardrails.check_figures's job, correctness is this
module's.

Values are computed fresh each harness invocation (never hardcoded), so "NVDA's revenue last
quarter" or "MSFT's FY2025 free cash flow" stay correct as new filings land -- the questions
don't go stale the way a snapshot constant would. `compute()` memoizes each static question's
result for the lifetime of the process, since it doesn't change across repeated runs of the same
question in one harness invocation.

Three of the seven questions don't get a precomputed value here, deliberately:
  - "revenue_growth_direction" is the one README question with no fixed ticker ("for a given
    company"). Its ground truth can't be known in advance -- it's computed *reactively*, after a
    run, from whichever ticker the agent's own tool calls actually analyzed.
  - "amd_nvda_comparison" ("who's better positioned?") and "costco_revenue_forecast" don't need
    an external ground truth at all: the comparison question is scored structurally (were both
    companies actually analyzed with real data), and the forecast question is scored for
    self-consistency (does the stated projection match the agent's own forecast_metric result,
    whatever method it chose) -- see scoring.py.
"""

import pandas as pd

from src.analysis import ratios as ratios_mod
from src.analysis.statements import get_statement

# "Accelerating"/"decelerating" only means something once the growth rate itself has moved by a
# non-trivial amount -- this is the noise floor below which two runs' worth of rounding shouldn't
# flip the verdict. In growth-rate units (0.01 == 1 percentage point of QoQ growth-rate change).
_GROWTH_DIRECTION_FLAT_THRESHOLD = 0.01
# Same idea for a margin trending "up"/"down" vs. "flat", in percentage points.
_MARGIN_FLAT_THRESHOLD_PP = 0.5

_static_cache: dict[str, dict | None] = {}


def _clean(value):
    """pandas NaN (from a missing ratio input) -> None; anything else -> a plain float."""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _nvda_revenue_and_margin() -> dict:
    stmt = get_statement("NVDA", "quarterly").sort_values("period_end").reset_index(drop=True)
    revenue_valid = stmt.dropna(subset=["revenue"])
    if revenue_valid.empty:
        return {"period_end": None, "revenue": None, "gross_margin": None}

    last_row = revenue_valid.iloc[-1]
    period_end = last_row["period_end"]
    margin_df = ratios_mod.gross_margin(stmt)
    match = margin_df[margin_df["period_end"] == period_end]
    margin_value = _clean(match["value"].iloc[0]) if not match.empty else None

    return {
        "period_end": period_end,
        "revenue": _clean(last_row["revenue"]),
        "gross_margin": margin_value,
    }


def _msft_fcf_fy2025() -> dict:
    # Microsoft's fiscal year is named for the calendar year it ends in (FY2025 ends
    # 2025-06-30), so filtering annual rows by period_end.year is exact here -- not a generic
    # "FY label -> calendar year" rule (see CLAUDE.md on why period_end, not fiscal_year, is this
    # project's time key).
    stmt = get_statement("MSFT", "annual").sort_values("period_end").reset_index(drop=True)
    fy_rows = stmt[stmt["period_end"].dt.year == 2025]
    if fy_rows.empty:
        return {"period_end": None, "free_cash_flow": None}

    period_end = fy_rows.iloc[-1]["period_end"]
    fcf_df = ratios_mod.free_cash_flow(stmt)
    match = fcf_df[fcf_df["period_end"] == period_end]
    fcf_value = _clean(match["value"].iloc[0]) if not match.empty else None

    return {"period_end": period_end, "free_cash_flow": fcf_value}


def _aapl_operating_margin_trend() -> dict:
    stmt = get_statement("AAPL", "quarterly", periods=8).sort_values("period_end").reset_index(drop=True)
    margin_df = ratios_mod.operating_margin(stmt)
    values = [_clean(v) for v in margin_df["value"].tolist()]
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return {"direction": None, "values": values}

    delta_pp = (values[-1] - values[0]) * 100
    if delta_pp > _MARGIN_FLAT_THRESHOLD_PP:
        direction = "up"
    elif delta_pp < -_MARGIN_FLAT_THRESHOLD_PP:
        direction = "down"
    else:
        direction = "flat"
    return {"direction": direction, "values": values, "delta_pp": delta_pp}


def _analyzed_tickers(tool_calls: list[dict]) -> list[str]:
    """Tickers the agent's own successful get_ratios/get_financial_statement calls targeted, in
    first-seen order -- used to figure out which company the agent picked for the one README
    question that doesn't name one."""
    tickers = []
    for call in tool_calls:
        if call.get("is_error"):
            continue
        if call.get("tool_name") not in ("get_ratios", "get_financial_statement"):
            continue
        ticker = (call.get("tool_input") or {}).get("ticker")
        if ticker:
            ticker = ticker.upper()
            if ticker not in tickers:
                tickers.append(ticker)
    return tickers


def _growth_direction_reactive(run_result: dict | None) -> dict | None:
    if not run_result:
        return None
    tickers = _analyzed_tickers(run_result.get("tool_calls") or [])
    if not tickers:
        return {"ticker": None, "direction": None}

    ticker = tickers[0]
    try:
        stmt = get_statement(ticker, "quarterly")
    except Exception:
        return {"ticker": ticker, "direction": None}

    growth_df = ratios_mod.revenue_growth_qoq(stmt)
    values = [_clean(v) for v in growth_df["value"].tolist()]
    values = [v for v in values if v is not None]
    recent = values[-5:] if len(values) >= 5 else values
    if len(recent) < 2:
        return {"ticker": ticker, "direction": None}

    delta = recent[-1] - recent[0]
    if delta > _GROWTH_DIRECTION_FLAT_THRESHOLD:
        direction = "accelerating"
    elif delta < -_GROWTH_DIRECTION_FLAT_THRESHOLD:
        direction = "decelerating"
    else:
        direction = "flat"
    return {"ticker": ticker, "direction": direction, "recent_growth_rates": recent}


_STATIC_COMPUTE = {
    "nvda_revenue_margin": _nvda_revenue_and_margin,
    "msft_fy2025_fcf": _msft_fcf_fy2025,
    "aapl_operating_margin_trend": _aapl_operating_margin_trend,
}


def compute(question, run_result: dict | None = None) -> dict | None:
    """
    Ground truth for `question`, or None for a question that's scored structurally/by
    self-consistency instead (see module docstring). `run_result` is only used -- and required to
    get a non-trivial answer -- for "revenue_growth_direction"; every other question's ground
    truth is independent of any run and cached after the first call.
    """
    if question.id == "revenue_growth_direction":
        return _growth_direction_reactive(run_result)

    if question.id not in _static_cache:
        fn = _STATIC_COMPUTE.get(question.id)
        _static_cache[question.id] = fn() if fn is not None else None
    return _static_cache[question.id]
