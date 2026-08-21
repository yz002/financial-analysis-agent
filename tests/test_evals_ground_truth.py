"""
Tests for evals/ground_truth.py's compute functions against real EDGAR data (NVDA, MSFT, AAPL,
F) via the same get_statement path the rest of the suite uses -- no Anthropic calls anywhere in
this file. Per CLAUDE.md, this hits EDGAR live only on a cold cache; once data/cache/ is warm
(which it will be after this file's first run, or already is from the rest of the suite for
NVDA/MSFT/F) these are offline like everything else in tests/. AAPL isn't used elsewhere in the
suite, so this file is what first warms its cache entry.
"""

import pandas as pd

from evals import ground_truth
from evals.questions import QUESTIONS_BY_ID


def test_nvda_ground_truth_has_a_recent_revenue_and_margin():
    result = ground_truth.compute(QUESTIONS_BY_ID["nvda_revenue_margin"])
    assert result["period_end"] is not None
    assert result["revenue"] is not None
    assert result["revenue"] > 0
    # Nvidia has reported gross_profit throughout its history -- margin should be computable.
    assert result["gross_margin"] is not None
    assert 0 < result["gross_margin"] < 1


def test_msft_fy2025_fcf_ground_truth_resolves_a_real_fiscal_year():
    result = ground_truth.compute(QUESTIONS_BY_ID["msft_fy2025_fcf"])
    assert result["period_end"] is not None
    assert pd.Timestamp(result["period_end"]).year == 2025


def test_aapl_operating_margin_trend_has_a_direction():
    result = ground_truth.compute(QUESTIONS_BY_ID["aapl_operating_margin_trend"])
    assert result["direction"] in ("up", "down", "flat")
    assert len(result["values"]) >= 2


def test_static_ground_truth_is_cached_across_calls():
    question = QUESTIONS_BY_ID["nvda_revenue_margin"]
    first = ground_truth.compute(question)
    second = ground_truth.compute(question)
    assert first is second


def test_growth_direction_reactive_uses_ticker_from_trace():
    question = QUESTIONS_BY_ID["revenue_growth_direction"]
    run_result = {
        "tool_calls": [
            {
                "iteration": 1,
                "tool_name": "get_ratios",
                "tool_input": {"ticker": "F"},
                "tool_result": "{}",
                "is_error": False,
            }
        ]
    }
    result = ground_truth.compute(question, run_result=run_result)
    assert result["ticker"] == "F"
    assert result["direction"] in ("accelerating", "decelerating", "flat", None)


def test_growth_direction_reactive_with_no_run_result_is_none():
    assert ground_truth.compute(QUESTIONS_BY_ID["revenue_growth_direction"], run_result=None) is None


def test_growth_direction_reactive_with_no_ticker_in_trace():
    question = QUESTIONS_BY_ID["revenue_growth_direction"]
    result = ground_truth.compute(question, run_result={"tool_calls": []})
    assert result == {"ticker": None, "direction": None}


def test_amd_and_costco_have_no_precomputed_ground_truth():
    assert ground_truth.compute(QUESTIONS_BY_ID["amd_nvda_comparison"]) is None
    assert ground_truth.compute(QUESTIONS_BY_ID["costco_revenue_forecast"]) is None


def test_ford_question_has_no_precomputed_ground_truth():
    assert ground_truth.compute(QUESTIONS_BY_ID["ford_10q_anomalies"]) is None
