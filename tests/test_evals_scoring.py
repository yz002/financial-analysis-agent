"""
Tests for evals/scoring.py, fully offline: synthetic run_agent-shaped result dicts, built by
hand, same style as tests/test_agent.py and tests/test_guardrails.py. No network, no Anthropic
client, no EDGAR calls -- ground truth is passed in directly rather than computed.
"""

import json

import pytest

from evals import scoring
from evals.questions import QUESTIONS_BY_ID


def _tool_call(tool_name, tool_input, payload, iteration=1, is_error=False):
    return {
        "iteration": iteration,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_result": json.dumps(payload),
        "is_error": is_error,
    }


def _result(final_answer, tool_calls):
    return {"final_answer": final_answer, "tool_calls": tool_calls}


# --- check_figure_present / check_percent_present (tolerance) -----------------------------


def test_figure_present_matches_within_relative_tolerance():
    check = scoring.check_figure_present("Revenue was $35.08 billion.", 35_082_000_000.0)
    assert check["passed"] is True


def test_figure_present_fails_just_outside_tolerance():
    # 35.08B vs 35.30B is a ~0.63% relative difference, past the default 0.5% tolerance.
    check = scoring.check_figure_present("Revenue was $35.30 billion.", 35_082_000_000.0)
    assert check["passed"] is False


def test_figure_present_passes_just_inside_tolerance():
    # 35.08B vs 35.20B is a ~0.34% relative difference, inside the default 0.5% tolerance.
    check = scoring.check_figure_present("Revenue was $35.20 billion.", 35_082_000_000.0)
    assert check["passed"] is True


def test_figure_present_none_expected_value_fails():
    check = scoring.check_figure_present("Revenue was $35.08 billion.", None)
    assert check["passed"] is False


def test_percent_present_matches_rounding_case():
    # NOTES.md's documented case: 0.749967 (74.9967%) stated as "75.00%" -- within 0.15pp.
    check = scoring.check_percent_present("Gross margin was 75.00%.", 0.749967)
    assert check["passed"] is True


def test_percent_present_fails_outside_tolerance():
    check = scoring.check_percent_present("Gross margin was 74.0%.", 0.749967)
    assert check["passed"] is False


def test_percent_present_ignores_non_percent_numbers():
    # 75 with no "%" shouldn't count as matching a 0.75 fraction.
    check = scoring.check_percent_present("There were 75 items.", 0.749967)
    assert check["passed"] is False


# --- check_direction ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Operating margin has been increasing over the period.",
        "Margins improved steadily.",
        "The margin expanded each quarter.",
    ],
)
def test_direction_up_matches_up_phrasings(text):
    check = scoring.check_direction(text, "up", scoring._MARGIN_UP_WORDS, scoring._MARGIN_DOWN_WORDS, scoring._MARGIN_FLAT_WORDS)
    assert check["passed"] is True


def test_direction_up_fails_on_down_wording():
    check = scoring.check_direction("Margins declined this quarter.", "up", scoring._MARGIN_UP_WORDS, scoring._MARGIN_DOWN_WORDS)
    assert check["passed"] is False


def test_direction_ambiguous_tie_fails():
    text = "Margins increased in Q1 but declined in Q2."
    check = scoring.check_direction(text, "up", scoring._MARGIN_UP_WORDS, scoring._MARGIN_DOWN_WORDS)
    assert check["passed"] is False


def test_direction_up_wins_by_count_despite_one_down_mention():
    # A real live-run case: a long "up" answer that mentions one unrelated dip (revenue
    # declining sequentially) in passing shouldn't false-fail just because both vocabularies
    # appear somewhere in the text.
    text = (
        "Margins have stepped up overall: every year-over-year comparison improved, and gross "
        "margin rose steadily too, reaching a record high. One quarter saw revenue declining "
        "sequentially, but margin still held up on strong profitability."
    )
    check = scoring.check_direction(text, "up", scoring._MARGIN_UP_WORDS, scoring._MARGIN_DOWN_WORDS)
    assert check["passed"] is True


def test_direction_flat_matches_flat_wording_or_absence():
    check = scoring.check_direction("Margins were roughly stable.", "flat", scoring._MARGIN_UP_WORDS, scoring._MARGIN_DOWN_WORDS, scoring._MARGIN_FLAT_WORDS)
    assert check["passed"] is True


def test_direction_accelerating_matches_decel_synonyms_fail():
    check = scoring.check_direction("Growth is slowing down.", "accelerating", scoring._ACCEL_WORDS, scoring._DECEL_WORDS)
    assert check["passed"] is False
    check2 = scoring.check_direction("Growth is accelerating.", "accelerating", scoring._ACCEL_WORDS, scoring._DECEL_WORDS)
    assert check2["passed"] is True


def test_direction_none_expected_fails():
    check = scoring.check_direction("Margins improved.", None, scoring._MARGIN_UP_WORDS, scoring._MARGIN_DOWN_WORDS)
    assert check["passed"] is False


# --- check_tool_called / check_any_tool_called ----------------------------------------------


def test_check_tool_called_matches_ticker():
    calls = [_tool_call("get_ratios", {"ticker": "NVDA"}, {"ok": True})]
    check = scoring.check_tool_called(calls, "get_ratios", ticker="NVDA")
    assert check["passed"] is True


def test_check_tool_called_ignores_error_calls():
    calls = [_tool_call("get_ratios", {"ticker": "NVDA"}, {"error": True}, is_error=True)]
    check = scoring.check_tool_called(calls, "get_ratios", ticker="NVDA")
    assert check["passed"] is False


def test_check_tool_called_wrong_ticker_fails():
    calls = [_tool_call("get_ratios", {"ticker": "AMD"}, {"ok": True})]
    check = scoring.check_tool_called(calls, "get_ratios", ticker="NVDA")
    assert check["passed"] is False


def test_check_any_tool_called_matches_second_tool():
    calls = [_tool_call("get_financial_statement", {"ticker": "NVDA"}, {"ok": True})]
    check = scoring.check_any_tool_called(calls, ("get_ratios", "get_financial_statement"), ticker="NVDA")
    assert check["passed"] is True


# --- Ford unavailable check ------------------------------------------------------------------


def _ford_statement_call(concepts_unavailable):
    return _tool_call(
        "get_financial_statement",
        {"ticker": "F"},
        {"ticker": "F", "concepts_unavailable": concepts_unavailable, "periods": []},
    )


def test_ford_check_passes_when_text_and_trace_both_confirm_unavailable():
    result = _result(
        "Ford does not report gross profit in its filings.",
        [_ford_statement_call(["gross_profit"])],
    )
    check = scoring.check_ford_gross_profit_unavailable(result)
    assert check["passed"] is True


def test_ford_check_fails_when_text_silent_on_gross_profit():
    result = _result(
        "Ford's revenue was flat quarter over quarter.",
        [_ford_statement_call(["gross_profit"])],
    )
    check = scoring.check_ford_gross_profit_unavailable(result)
    assert check["passed"] is False


def test_ford_check_fails_when_trace_does_not_confirm_unavailability():
    # Model claims it's unavailable, but the tool result never actually said that -- shouldn't
    # score as correct just because the words are present.
    result = _result(
        "Ford does not report gross profit in its filings.",
        [_ford_statement_call([])],
    )
    check = scoring.check_ford_gross_profit_unavailable(result)
    assert check["passed"] is False


# --- forecast assumptions/refusal check -------------------------------------------------------


def _forecast_call(forecast_available, projections=None, reason=None):
    payload = {
        "ticker": "COST",
        "column": "revenue",
        "forecast_available": forecast_available,
    }
    if forecast_available:
        payload["projections"] = [{"period_end": "2026-02-28", "value": v} for v in (projections or [])]
    else:
        payload["reason"] = reason or "Not enough history."
    return _tool_call("forecast_metric", {"ticker": "COST", "column": "revenue"}, payload)


def test_forecast_check_passes_with_projection_and_assumptions():
    result = _result(
        "Costco's revenue is projected to be $65.0 billion next quarter, based on the recent growth trend assumption.",
        [_forecast_call(True, projections=[65_000_000_000.0])],
    )
    check = scoring.check_forecast_assumptions_or_refusal(result, "COST", "revenue")
    assert check["passed"] is True


def test_forecast_check_fails_when_projection_stated_without_assumptions():
    result = _result(
        "Costco's revenue will be $65.0 billion next quarter.",
        [_forecast_call(True, projections=[65_000_000_000.0])],
    )
    check = scoring.check_forecast_assumptions_or_refusal(result, "COST", "revenue")
    assert check["passed"] is False


def test_forecast_check_passes_on_explained_refusal():
    result = _result(
        "There isn't enough historical data to project Costco's revenue reliably.",
        [_forecast_call(False, reason="Not enough history.")],
    )
    check = scoring.check_forecast_assumptions_or_refusal(result, "COST", "revenue")
    assert check["passed"] is True


def test_forecast_check_fails_when_no_matching_tool_call():
    result = _result("Costco's revenue will grow.", [])
    check = scoring.check_forecast_assumptions_or_refusal(result, "COST", "revenue")
    assert check["passed"] is False


# --- score_question end to end (per question id) ---------------------------------------------


def test_score_nvda_lookup_all_checks_pass():
    calls = [_tool_call("get_ratios", {"ticker": "NVDA"}, {"ratios": {}})]
    result = _result(
        "Nvidia's revenue was $35.08 billion last quarter, with a gross margin of 75.00%.",
        calls,
    )
    gt = {"revenue": 35_082_000_000.0, "gross_margin": 0.749967}
    score = scoring.score_question(QUESTIONS_BY_ID["nvda_revenue_margin"], result, gt)
    assert score["passed"] is True
    assert all(c["passed"] for c in score["checks"])


def test_score_nvda_lookup_fails_when_margin_missing():
    calls = [_tool_call("get_ratios", {"ticker": "NVDA"}, {"ratios": {}})]
    result = _result("Nvidia's revenue was $35.08 billion last quarter.", calls)
    gt = {"revenue": 35_082_000_000.0, "gross_margin": 0.749967}
    score = scoring.score_question(QUESTIONS_BY_ID["nvda_revenue_margin"], result, gt)
    assert score["passed"] is False
    failing = [c["name"] for c in score["checks"] if not c["passed"]]
    assert "gross_margin_figure" in failing


def test_score_growth_direction_with_no_ticker_and_no_clarification_fails():
    # Fabricating a verdict with no company analyzed at all is a real failure -- distinct from
    # the legitimate clarifying-question case below.
    result = _result("Revenue growth appears to be accelerating.", [])
    score = scoring.score_question(QUESTIONS_BY_ID["revenue_growth_direction"], result, {"ticker": None, "direction": None})
    assert score["passed"] is False


def test_score_growth_direction_accepts_a_clarifying_question():
    # A live run showed the model correctly asking which company to analyze rather than
    # guessing one, for README's one genuinely ticker-less question -- that should score as a
    # legitimate answer, not a failure to answer.
    result = _result(
        "I can answer that -- I just need to know which company you'd like me to look at. "
        "Give me a ticker and I'll run the numbers.",
        [],
    )
    score = scoring.score_question(QUESTIONS_BY_ID["revenue_growth_direction"], result, {"ticker": None, "direction": None})
    assert score["passed"] is True


def test_score_growth_direction_matches_reactive_ground_truth():
    calls = [_tool_call("get_ratios", {"ticker": "NVDA"}, {"ratios": {}})]
    result = _result("Nvidia's revenue growth is accelerating.", calls)
    gt = {"ticker": "NVDA", "direction": "accelerating"}
    score = scoring.score_question(QUESTIONS_BY_ID["revenue_growth_direction"], result, gt)
    assert score["passed"] is True


def test_score_comparison_requires_both_tickers_and_verdict():
    calls = [
        _tool_call("get_ratios", {"ticker": "AMD"}, {"ratios": {}}),
        _tool_call("get_ratios", {"ticker": "NVDA"}, {"ratios": {}}),
    ]
    result = _result("Nvidia looks better positioned than AMD given its wider margins.", calls)
    score = scoring.score_question(QUESTIONS_BY_ID["amd_nvda_comparison"], result, None)
    assert score["passed"] is True


def test_score_comparison_accepts_ticker_only_mention():
    # A live run showed the model referring to Nvidia only as "NVDA" throughout, never the word
    # "Nvidia" -- a legitimate stylistic choice that should still count as mentioning the company.
    calls = [
        _tool_call("get_ratios", {"ticker": "AMD"}, {"ratios": {}}),
        _tool_call("get_ratios", {"ticker": "NVDA"}, {"ratios": {}}),
    ]
    result = _result("NVDA is better positioned than AMD given its wider margins.", calls)
    score = scoring.score_question(QUESTIONS_BY_ID["amd_nvda_comparison"], result, None)
    assert score["passed"] is True


def test_score_comparison_fails_without_comparative_verdict():
    calls = [
        _tool_call("get_ratios", {"ticker": "AMD"}, {"ratios": {}}),
        _tool_call("get_ratios", {"ticker": "NVDA"}, {"ratios": {}}),
    ]
    result = _result("AMD and Nvidia both reported margins this quarter.", calls)
    score = scoring.score_question(QUESTIONS_BY_ID["amd_nvda_comparison"], result, None)
    assert score["passed"] is False
