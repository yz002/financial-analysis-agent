"""
Tests for the non-Streamlit logic in src/app/main.py: build_charts, _mentions,
_escape_markdown_dollars, and the run_agent-error-to-message mapping in
_run_agent_or_error. Fully offline, pure-function tests only -- no Streamlit
script execution (main(), render_*) and no real network/EDGAR/Anthropic calls.
get_company_name is stubbed everywhere below so these tests don't depend on
EdgarClient's on-disk cache state.
"""

import json

import anthropic
import httpx
import pytest

from src.app import main as app_main


@pytest.fixture(autouse=True)
def _stub_company_name(monkeypatch):
    """build_charts calls get_company_name (SEC ticker -> registered name) for chart titles;
    stub it to a deterministic value so these tests never touch EdgarClient/the network."""
    monkeypatch.setattr(app_main, "get_company_name", lambda ticker: f"{ticker} INC")


def _tool_call(tool_name, payload, tool_input=None, iteration=1, is_error=False):
    return {
        "iteration": iteration,
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "tool_result": json.dumps(payload),
        "is_error": is_error,
    }


def _statement_periods(ticker, concept_values):
    """concept_values: {concept_name: [value_or_None, ...]} across periods, one period per
    index. Builds get_financial_statement-shaped periods with sequential quarter-end dates."""
    months = ["01-31", "04-30", "07-31", "10-31", "01-31", "04-30", "07-31", "10-31"]
    n = len(next(iter(concept_values.values())))
    periods = []
    for i in range(n):
        year = 2024 + (i // 4)
        period = {"period_end": f"{year}-{months[i % len(months)]}", "period_start": None}
        for concept, values in concept_values.items():
            v = values[i]
            period[concept] = {"value": v, "tag": "Tag", "filed": "2025-01-01"} if v is not None else None
        periods.append(period)
    return {"ticker": ticker, "period_length": "quarterly", "periods_returned": n,
            "concepts_unavailable": [], "notes": [], "periods": periods}


def _ratios_payload(ticker, ratio_values):
    """ratio_values: {ratio_name: [value_or_None, ...]}."""
    ratios = {}
    for name, values in ratio_values.items():
        ratios[name] = [
            {"period_end": f"2024-0{i + 1}-01", "value": v, "inputs": {}, "provenance": {}}
            for i, v in enumerate(values)
        ]
    return {"ticker": ticker, "period_length": "quarterly", "notes": [], "ratios": ratios}


# ---------------------------------------------------------------------------
# _mentions
# ---------------------------------------------------------------------------


def test_mentions_finds_space_joined_variant_of_underscored_name():
    pos = app_main._mentions("gross_margin", "The gross margin improved this quarter.")
    assert pos == 4


def test_mentions_finds_raw_underscored_form():
    pos = app_main._mentions("gross_margin", "Reported gross_margin was strong.")
    assert pos == 9


def test_mentions_is_case_insensitive():
    assert app_main._mentions("revenue", "Revenue grew year over year.") == 0


def test_mentions_returns_none_when_absent():
    assert app_main._mentions("revenue", "Net income grew this quarter.") is None


def test_mentions_does_not_false_positive_on_a_longer_underscored_name():
    # "revenue" must not match inside "revenue_growth_qoq" -- the trailing "_" is a word
    # character, so \b shouldn't hold between "revenue" and "_growth_qoq".
    assert app_main._mentions("revenue", "revenue_growth_qoq was flagged as an anomaly.") is None


# ---------------------------------------------------------------------------
# _escape_markdown_dollars
# ---------------------------------------------------------------------------


def test_escape_markdown_dollars_escapes_every_dollar_sign():
    text = "Price $216.39 and market cap $5.241T"
    assert app_main._escape_markdown_dollars(text) == r"Price \$216.39 and market cap \$5.241T"


def test_escape_markdown_dollars_leaves_text_without_dollars_unchanged():
    text = "Revenue grew 12% year over year."
    assert app_main._escape_markdown_dollars(text) == text


# ---------------------------------------------------------------------------
# build_charts
# ---------------------------------------------------------------------------


def test_build_charts_ranks_a_mentioned_concept_first():
    stmt = _statement_periods("NVDA", {
        "revenue": [100.0, 110.0],
        "gross_profit": [40.0, 45.0],
    })
    call = _tool_call("get_financial_statement", stmt)
    charts = app_main.build_charts(
        "How has gross profit trended?", "Gross profit rose steadily.", [call]
    )

    names = [c["title"] for c in charts]
    assert names[0] == "NVDA INC — gross_profit"
    # revenue still appears via the "revenue" fallback, just ranked after the mentioned one
    assert "NVDA INC — revenue" in names
    assert names.index("NVDA INC — gross_profit") < names.index("NVDA INC — revenue")


def test_build_charts_ranks_mentions_in_order_of_first_appearance():
    stmt = _statement_periods("MSFT", {
        "net_income": [10.0, 12.0],
        "operating_income": [20.0, 22.0],
    })
    call = _tool_call("get_financial_statement", stmt)
    # "operating income" appears before "net income" in the text
    charts = app_main.build_charts(
        "How do operating income and net income compare?", "", [call]
    )
    names = [c["title"] for c in charts]
    assert names.index("MSFT INC — operating_income") < names.index("MSFT INC — net_income")


def test_build_charts_caps_output_at_max_charts():
    # All 12 concepts qualify (>=2 points each) and all 12 are explicitly named in the question,
    # so the mention-ranking pass alone produces far more than MAX_CHARTS candidates.
    stmt = _statement_periods("AAPL", {c: [1.0, 2.0, 3.0] for c in app_main.ALL_CONCEPTS})
    call = _tool_call("get_financial_statement", stmt)
    question = "Compare " + ", ".join(app_main.ALL_CONCEPTS) + "."
    charts = app_main.build_charts(question, "", [call])

    assert len(charts) == app_main.MAX_CHARTS
    # capped to the first MAX_CHARTS concepts in order of mention (== ALL_CONCEPTS' own order,
    # since the question lists them in that order)
    expected = [f"AAPL INC — {c}" for c in app_main.ALL_CONCEPTS[: app_main.MAX_CHARTS]]
    assert [c["title"] for c in charts] == expected


def test_build_charts_falls_back_to_revenue_and_requested_ratio_names():
    stmt = _statement_periods("F", {"revenue": [100.0, 110.0]})
    stmt_call = _tool_call("get_financial_statement", stmt)
    ratios = _ratios_payload("F", {
        "gross_margin": [0.4, 0.41],
        "operating_margin": [0.1, 0.11],
    })
    ratios_call = _tool_call(
        "get_ratios", ratios, tool_input={"ticker": "F", "ratio_names": ["gross_margin", "operating_margin"]}
    )

    # Nothing in the question/answer text names any of these series -- the ranking pass finds
    # no mentions, so every chart here comes from the fallback tier.
    charts = app_main.build_charts("How is Ford doing?", "Ford is doing fine.", [stmt_call, ratios_call])

    names = [c["title"] for c in charts]
    assert names == ["F INC — revenue", "F INC — gross_margin", "F INC — operating_margin"]


def test_build_charts_skips_null_values_but_keeps_series_with_enough_real_points():
    # 3 periods, middle one null -- 2 real points remain, still >= 2, so the series is kept
    # with only its real points plotted.
    stmt = _statement_periods("KO", {"revenue": [100.0, None, 120.0]})
    call = _tool_call("get_financial_statement", stmt)
    charts = app_main.build_charts("revenue", "", [call])

    assert len(charts) == 1
    df = charts[0]["df"]
    assert len(df) == 2
    assert sorted(df["value"].tolist()) == [100.0, 120.0]


def test_build_charts_drops_series_with_fewer_than_two_points():
    # Only 1 real point for capex after nulling out the other two -- must not appear at all,
    # even though it's explicitly named in the question (mention-ranking only ranks candidates
    # that already qualified on point count).
    stmt = _statement_periods("KO", {"capex": [50.0, None, None]})
    call = _tool_call("get_financial_statement", stmt)
    charts = app_main.build_charts("What was capex?", "", [call])

    assert charts == []


def test_build_charts_skips_null_ratio_values():
    ratios = _ratios_payload("KO", {"roa": [0.05, None, None, 0.06]})
    call = _tool_call("get_ratios", ratios, tool_input={"ticker": "KO", "ratio_names": ["roa"]})
    charts = app_main.build_charts("roa", "", [call])

    assert len(charts) == 1
    assert len(charts[0]["df"]) == 2


def test_build_charts_ignores_error_tool_calls():
    stmt = _statement_periods("KO", {"revenue": [100.0, 110.0]})
    call = _tool_call("get_financial_statement", stmt, is_error=True)
    charts = app_main.build_charts("revenue", "", [call])
    assert charts == []


def test_build_charts_returns_empty_list_with_no_qualifying_tool_calls():
    assert app_main.build_charts("anything", "anything", []) == []


# ---------------------------------------------------------------------------
# _run_agent_or_error
# ---------------------------------------------------------------------------


def _httpx_request():
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_run_agent_or_error_returns_result_on_success(monkeypatch):
    sentinel = {"final_answer": "ok", "figure_check": {}}
    monkeypatch.setattr(app_main, "run_agent", lambda question: sentinel)

    result, error = app_main._run_agent_or_error("What was revenue?")

    assert result is sentinel
    assert error is None


def test_run_agent_or_error_maps_authentication_error(monkeypatch):
    response = httpx.Response(401, request=_httpx_request())

    def raise_auth_error(question):
        raise anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)

    monkeypatch.setattr(app_main, "run_agent", raise_auth_error)

    result, error = app_main._run_agent_or_error("What was revenue?")

    assert result is None
    assert "ANTHROPIC_API_KEY" in error
    assert "invalid x-api-key" not in error  # message is user-facing, not the raw SDK error text


def test_run_agent_or_error_maps_connection_error(monkeypatch):
    def raise_connection_error(question):
        raise anthropic.APIConnectionError(request=_httpx_request())

    monkeypatch.setattr(app_main, "run_agent", raise_connection_error)

    result, error = app_main._run_agent_or_error("What was revenue?")

    assert result is None
    assert "network connection" in error


def test_run_agent_or_error_maps_unexpected_exception(monkeypatch):
    def raise_value_error(question):
        raise ValueError("max_iterations must be at least 1")

    monkeypatch.setattr(app_main, "run_agent", raise_value_error)

    result, error = app_main._run_agent_or_error("What was revenue?")

    assert result is None
    assert "Unexpected error" in error
    assert "max_iterations must be at least 1" in error
