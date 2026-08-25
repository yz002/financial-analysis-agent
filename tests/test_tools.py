"""
Tests for src/agent/tools.py, fully offline: every tool built on
get_statement (get_financial_statement, get_ratios, detect_anomalies) is
exercised against a synthetic statement DataFrame installed by monkeypatching
`tools.get_statement`, matching the column shape statements.get_statement
actually produces (see statements.py's docstring). Every tool built on
market.py (get_market_data, get_price_history_tool) is exercised by
monkeypatching the market functions tools.py imported by name. No network
call, live EDGAR request, or yfinance call happens anywhere in this file.
"""

import json

import pandas as pd
import pytest

from src.agent import tools
from src.data.concepts import ConceptNotFoundError


def _make_statement(n_periods, missing=(), start="2020-03-31", filed_tag="SomeTag"):
    """
    A synthetic statement DataFrame with the same column shape
    statements.get_statement produces: period_end, period_start, and per
    concept in tools.ALL_CONCEPTS: "{concept}", "{concept}_tag",
    "{concept}_filed", and (duration concepts only) "{concept}_is_derived",
    "{concept}_q4_subtraction_value" (default NaN -- not applicable),
    "{concept}_q4_diverges_from_subtraction" (default False).
    `missing` concepts get an all-NaN/None column, exactly like a concept
    with zero usable data for a ticker (e.g. Ford's gross_profit).
    """
    period_ends = [pd.Timestamp(start) + pd.Timedelta(days=91 * i) for i in range(n_periods)]
    period_starts = [pe - pd.Timedelta(days=89) for pe in period_ends]
    data = {"period_end": period_ends, "period_start": period_starts}

    for i, concept in enumerate(tools.ALL_CONCEPTS):
        if concept in missing:
            data[concept] = [float("nan")] * n_periods
            data[f"{concept}_tag"] = [None] * n_periods
            data[f"{concept}_filed"] = [pd.NaT] * n_periods
        else:
            base = 1000.0 * (i + 1)
            data[concept] = [base + 10.0 * j for j in range(n_periods)]
            data[f"{concept}_tag"] = [filed_tag] * n_periods
            data[f"{concept}_filed"] = [
                pd.Timestamp("2021-01-01") + pd.Timedelta(days=j) for j in range(n_periods)
            ]
        if concept in tools.DURATION_CONCEPTS:
            data[f"{concept}_is_derived"] = [False] * n_periods
            data[f"{concept}_q4_subtraction_value"] = [float("nan")] * n_periods
            data[f"{concept}_q4_diverges_from_subtraction"] = [False] * n_periods

    return pd.DataFrame(data)


def _install_statement(monkeypatch, stmt_or_factory):
    if callable(stmt_or_factory) and not isinstance(stmt_or_factory, pd.DataFrame):
        monkeypatch.setattr(tools, "get_statement", stmt_or_factory)
    else:
        monkeypatch.setattr(
            tools,
            "get_statement",
            lambda ticker, period_length="quarterly", periods=None: stmt_or_factory,
        )


# --- get_financial_statement -------------------------------------------------


def test_get_financial_statement_shape_and_provenance(monkeypatch):
    _install_statement(monkeypatch, _make_statement(4))
    result = json.loads(tools.get_financial_statement("msft", periods=4))

    assert result["ticker"] == "MSFT"
    assert result["periods_returned"] == 4
    assert result["concepts_unavailable"] == []
    assert result["notes"] == []

    period = result["periods"][-1]
    for concept in tools.DURATION_CONCEPTS:
        entry = period[concept]
        assert entry["tag"] == "SomeTag"
        assert entry["filed"] is not None
        assert entry["is_derived"] is False
    for concept in tools.INSTANT_CONCEPTS:
        entry = period[concept]
        assert entry["tag"] == "SomeTag"
        assert "is_derived" not in entry


def test_get_financial_statement_q4_reconciliation_fields(monkeypatch):
    stmt = _make_statement(4)
    diverging_idx = stmt.index[-1]
    stmt.loc[diverging_idx, "revenue_q4_subtraction_value"] = 999.0
    stmt.loc[diverging_idx, "revenue_q4_diverges_from_subtraction"] = True
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_financial_statement("msft", periods=4))

    diverging_period = result["periods"][-1]
    assert diverging_period["revenue"]["q4_subtraction_value"] == 999.0
    assert diverging_period["revenue"]["q4_diverges_from_subtraction"] is True

    non_diverging_period = result["periods"][0]
    assert "q4_subtraction_value" not in non_diverging_period["revenue"]
    assert "q4_diverges_from_subtraction" not in non_diverging_period["revenue"]

    assert any("q4_subtraction_value" in n for n in result["notes"])


def test_fords_missing_gross_profit_in_concepts_unavailable(monkeypatch):
    _install_statement(monkeypatch, _make_statement(4, missing=["gross_profit"]))
    result = json.loads(tools.get_financial_statement("F", periods=4))

    assert "gross_profit" in result["concepts_unavailable"]
    assert any("gross_profit" in n and "F" in n for n in result["notes"])
    assert all(p["gross_profit"] is None for p in result["periods"])


def test_max_periods_cap_fires_on_periods_null(monkeypatch):
    captured = {}

    def fake_get_statement(ticker, period_length="quarterly", periods=None):
        captured["periods"] = periods
        return _make_statement(periods)

    _install_statement(monkeypatch, fake_get_statement)
    result = json.loads(tools.get_financial_statement("MSFT", periods=None))

    assert captured["periods"] == tools.MAX_PERIODS
    assert result["periods_returned"] == tools.MAX_PERIODS
    assert any("periods=None" in n and str(tools.MAX_PERIODS) in n for n in result["notes"])


def test_max_periods_cap_fires_when_request_exceeds_cap(monkeypatch):
    _install_statement(
        monkeypatch,
        lambda ticker, period_length="quarterly", periods=None: _make_statement(periods),
    )
    result = json.loads(tools.get_financial_statement("MSFT", periods=100))

    assert result["periods_returned"] == tools.MAX_PERIODS
    assert any("periods=100" in n for n in result["notes"])


def test_get_financial_statement_data_unavailable(monkeypatch):
    def raise_not_found(ticker, period_length="quarterly", periods=None):
        raise ConceptNotFoundError(f"No usable data for any concept for {ticker!r}")

    _install_statement(monkeypatch, raise_not_found)
    result = json.loads(tools.get_financial_statement("BOGUSTICKER"))
    assert result["error_type"] == "data_unavailable"


def test_get_financial_statement_relays_sparse_history_note(monkeypatch):
    # get_statement flags an implausibly thin resolved CIK via df.attrs (see statements.py --
    # e.g. a successor registrant from a merger/reorganization/redomiciliation, confirmed real
    # case: ExxonMobil's 2026-07-01 redomiciliation). get_financial_statement must relay that
    # note, not just the row data, so the agent doesn't treat a short series as the company's
    # whole history.
    stmt = _make_statement(2)
    stmt.attrs["sparse_history"] = True
    stmt.attrs["sparse_history_note"] = "XOM resolves to CIK 0002115436 (ExxonMobil Holdings Corp)..."
    _install_statement(monkeypatch, stmt)

    result = json.loads(tools.get_financial_statement("XOM", periods=2))
    assert any("ExxonMobil Holdings Corp" in n for n in result["notes"])


def test_get_financial_statement_no_sparse_history_note_when_not_flagged(monkeypatch):
    stmt = _make_statement(4)
    stmt.attrs["sparse_history"] = False
    stmt.attrs["sparse_history_note"] = None
    _install_statement(monkeypatch, stmt)

    result = json.loads(tools.get_financial_statement("msft", periods=4))
    assert result["notes"] == []


def test_get_financial_statement_source_error(monkeypatch):
    def raise_generic(ticker, period_length="quarterly", periods=None):
        raise RuntimeError("EDGAR is down")

    _install_statement(monkeypatch, raise_generic)
    result = json.loads(tools.get_financial_statement("MSFT"))
    assert result["error_type"] == "source_error"
    assert "MSFT" in result["error"]


# --- get_ratios ----------------------------------------------------------


def test_get_ratios_unknown_ratio_is_invalid_input():
    result = json.loads(tools.get_ratios("MSFT", ratio_names=["not_a_real_ratio"]))
    assert result["error_type"] == "invalid_input"


def test_get_ratios_provenance_carries_is_derived_only_for_duration_concepts(monkeypatch):
    _install_statement(monkeypatch, _make_statement(8))
    result = json.loads(
        tools.get_ratios("MSFT", ratio_names=["gross_margin", "current_ratio"], periods=8)
    )

    gm_row = result["ratios"]["gross_margin"][-1]
    assert "is_derived" in gm_row["provenance"]["gross_profit"]
    assert "is_derived" in gm_row["provenance"]["revenue"]

    cr_row = result["ratios"]["current_ratio"][-1]
    assert "is_derived" not in cr_row["provenance"]["current_assets"]
    assert "is_derived" not in cr_row["provenance"]["current_liabilities"]


def test_get_ratios_roa_roe_use_ttm_on_quarterly_cadence(monkeypatch):
    _install_statement(monkeypatch, _make_statement(8))
    result = json.loads(
        tools.get_ratios("MSFT", ratio_names=["roa", "roe"], period_length="quarterly")
    )

    assert any("trailing-twelve-month" in n for n in result["notes"])
    for name in ("roa", "roe"):
        last_row = result["ratios"][name][-1]
        assert "net_income_ttm" in last_row["inputs"]
        assert last_row["inputs"]["net_income_ttm"] is not None


def test_get_ratios_roe_note_fires_when_equity_tag_is_nci_inclusive(monkeypatch):
    stmt = _make_statement(8)
    stmt["stockholders_equity_tag"] = (
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    )
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_ratios("F", ratio_names=["roe"]))
    assert any("noncontrolling" in n.lower() for n in result["notes"])


def test_get_ratios_roe_note_distinguishes_mixed_tags_wording(monkeypatch):
    stmt = _make_statement(8)
    stmt["stockholders_equity_tag"] = ["StockholdersEquity"] * 4 + [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    ] * 4
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_ratios("F", ratio_names=["roe"]))
    assert any("own reported history" in n for n in result["notes"])


def test_get_ratios_roe_note_absent_when_equity_tag_is_plain(monkeypatch):
    stmt = _make_statement(8)
    stmt["stockholders_equity_tag"] = "StockholdersEquity"
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_ratios("MSFT", ratio_names=["roe"]))
    assert not any("noncontrolling" in n.lower() for n in result["notes"])


def test_get_ratios_roa_note_absent_even_when_equity_tag_would_trigger_it(monkeypatch):
    # roa never touches stockholders_equity -- confirms the roe-only note doesn't leak into roa
    # even when the underlying statement's equity tag would trigger it for roe.
    stmt = _make_statement(8)
    stmt["stockholders_equity_tag"] = (
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    )
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_ratios("F", ratio_names=["roa"]))
    assert not any("noncontrolling" in n.lower() for n in result["notes"])


def test_get_ratios_missing_concept_note_and_null_values(monkeypatch):
    _install_statement(monkeypatch, _make_statement(8, missing=["gross_profit"]))
    result = json.loads(tools.get_ratios("F", ratio_names=["gross_margin"]))

    assert all(r["value"] is None for r in result["ratios"]["gross_margin"])
    assert any("gross_profit" in n and "gross_margin" in n for n in result["notes"])


def test_get_ratios_relays_sparse_history_note(monkeypatch):
    stmt = _make_statement(2)
    stmt.attrs["sparse_history"] = True
    stmt.attrs["sparse_history_note"] = "XOM resolves to CIK 0002115436 (ExxonMobil Holdings Corp)..."
    _install_statement(monkeypatch, stmt)

    result = json.loads(tools.get_ratios("XOM", ratio_names=["gross_margin"]))
    assert any("ExxonMobil Holdings Corp" in n for n in result["notes"])


def test_get_ratios_cap_note(monkeypatch):
    _install_statement(
        monkeypatch,
        lambda ticker, period_length="quarterly", periods=None: _make_statement(periods),
    )
    result = json.loads(tools.get_ratios("MSFT", ratio_names=["gross_margin"], periods=None))
    assert any(str(tools.MAX_PERIODS) in n for n in result["notes"])


# --- get_market_data -------------------------------------------------------


def test_get_market_data_success_excludes_ticker_key(monkeypatch):
    monkeypatch.setattr(
        tools,
        "get_current_quote",
        lambda ticker: {"ticker": ticker, "price": 1.0, "market_cap": 2.0, "shares_outstanding": 3.0},
    )
    monkeypatch.setattr(
        tools,
        "get_valuation_metrics",
        lambda ticker: {"ticker": ticker, "trailing_pe": 10.0},
    )
    result = json.loads(tools.get_market_data("aapl"))

    assert result["ticker"] == "AAPL"
    assert "ticker" not in result["quote"]
    assert result["quote"]["price"] == 1.0
    assert "ticker" not in result["valuation"]
    assert result["valuation"]["trailing_pe"] == 10.0


def test_get_market_data_quote_data_unavailable(monkeypatch):
    def raise_mde(ticker):
        raise tools.MarketDataError(f"No quote data for {ticker!r}")

    monkeypatch.setattr(tools, "get_current_quote", raise_mde)
    monkeypatch.setattr(tools, "get_valuation_metrics", lambda ticker: {"ticker": ticker})

    result = json.loads(tools.get_market_data("BADTICKER"))
    assert result["quote"] is None
    assert result["quote_error"]["error_type"] == "data_unavailable"


def test_get_market_data_valuation_source_error(monkeypatch):
    monkeypatch.setattr(tools, "get_current_quote", lambda ticker: {"ticker": ticker})

    def raise_generic(ticker):
        raise RuntimeError("yfinance blew up")

    monkeypatch.setattr(tools, "get_valuation_metrics", raise_generic)

    result = json.loads(tools.get_market_data("MSFT"))
    assert result["valuation"] is None
    assert result["valuation_error"]["error_type"] == "source_error"


# --- detect_anomalies --------------------------------------------------------


def test_detect_anomalies_unknown_metric_is_invalid_input():
    result = json.loads(tools.detect_anomalies("MSFT", metric="not_a_metric"))
    assert result["error_type"] == "invalid_input"


def test_detect_anomalies_invalid_mode_is_invalid_input():
    result = json.loads(tools.detect_anomalies("MSFT", metric="revenue", mode="sideways"))
    assert result["error_type"] == "invalid_input"


def test_detect_anomalies_all_nan_metric_is_data_unavailable(monkeypatch):
    _install_statement(monkeypatch, _make_statement(12, missing=["gross_profit"]))
    result = json.loads(tools.detect_anomalies("F", metric="gross_profit"))
    assert result["error_type"] == "data_unavailable"
    assert result["metric"] == "gross_profit"


def test_detect_anomalies_insufficient_history_is_data_unavailable(monkeypatch):
    # window defaults to 8; growth_anomalies needs more than `window` growth
    # points, and a 3-period statement yields only 2 -- not enough for a
    # trailing baseline, which trends.trailing_stats raises ValueError for.
    _install_statement(monkeypatch, _make_statement(3))
    result = json.loads(tools.detect_anomalies("MSFT", metric="revenue"))
    assert result["error_type"] == "data_unavailable"


def test_detect_anomalies_source_error(monkeypatch):
    def raise_generic(ticker, period_length="quarterly", periods=None):
        raise RuntimeError("EDGAR is down")

    _install_statement(monkeypatch, raise_generic)
    result = json.loads(tools.detect_anomalies("MSFT", metric="revenue"))
    assert result["error_type"] == "source_error"


def test_detect_anomalies_growth_mode_success_shape(monkeypatch):
    _install_statement(monkeypatch, _make_statement(12))
    result = json.loads(tools.detect_anomalies("MSFT", metric="revenue", mode="growth"))

    assert result["mode"] == "growth"
    assert "growth rate" in result["value_description"]
    assert len(result["periods"]) > 0
    period = result["periods"][0]
    assert set(period) == {
        "period_end",
        "value",
        "trailing_mean",
        "trailing_std",
        "deviation_std",
        "is_anomaly",
    }


def test_detect_anomalies_level_mode_success_shape(monkeypatch):
    _install_statement(monkeypatch, _make_statement(12))
    result = json.loads(tools.detect_anomalies("MSFT", metric="cash", mode="level"))

    assert result["mode"] == "level"
    assert "level" in result["value_description"]
    assert len(result["periods"]) > 0


# --- get_price_history_tool --------------------------------------------------


def _ohlcv_df(n, start="2023-01-02"):
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame(
        {
            "Open": range(n),
            "High": range(n),
            "Low": range(n),
            "Close": range(n),
            "Volume": range(n),
        },
        index=idx,
    )


def test_price_history_stays_daily_under_max_rows(monkeypatch):
    df = _ohlcv_df(30)
    monkeypatch.setattr(
        tools, "get_price_history", lambda ticker, start=None, end=None: df
    )
    result = json.loads(tools.get_price_history_tool("MSFT", start="2023-01-01", end="2023-02-01"))

    assert result["resolution"] == "daily"
    assert result["rows_returned"] == 30
    assert "Downsampled" not in result["note"]


def test_price_history_downsamples_to_weekly_past_max_daily_rows(monkeypatch):
    n = tools.MAX_DAILY_PRICE_ROWS + 30
    df = _ohlcv_df(n)
    monkeypatch.setattr(
        tools, "get_price_history", lambda ticker, start=None, end=None: df
    )
    result = json.loads(tools.get_price_history_tool("MSFT", start="2023-01-01", end="2023-12-01"))

    assert result["resolution"] == "weekly"
    assert result["rows_returned"] < n
    assert "Downsampled to weekly bars" in result["note"]


def test_price_history_data_unavailable(monkeypatch):
    def raise_mde(ticker, start=None, end=None):
        raise tools.MarketDataError(f"No price history for {ticker!r}")

    monkeypatch.setattr(tools, "get_price_history", raise_mde)
    result = json.loads(tools.get_price_history_tool("BADTICKER", start="2023-01-01"))
    assert result["error_type"] == "data_unavailable"
    assert result["start"] == "2023-01-01"


def test_price_history_source_error(monkeypatch):
    def raise_generic(ticker, start=None, end=None):
        raise RuntimeError("yfinance blew up")

    monkeypatch.setattr(tools, "get_price_history", raise_generic)
    result = json.loads(tools.get_price_history_tool("MSFT", start="2023-01-01"))
    assert result["error_type"] == "source_error"


# --- execute_tool / TOOL_NAMES / TOOL_DEFINITIONS ---------------------------


def test_execute_tool_dispatches_by_name(monkeypatch):
    monkeypatch.setattr(tools, "get_current_quote", lambda ticker: {"ticker": ticker})
    monkeypatch.setattr(tools, "get_valuation_metrics", lambda ticker: {"ticker": ticker})
    result = json.loads(tools.execute_tool("get_market_data", {"ticker": "MSFT"}))
    assert result["ticker"] == "MSFT"


def test_execute_tool_unknown_name_raises_keyerror():
    with pytest.raises(KeyError):
        tools.execute_tool("not_a_real_tool", {})


def test_tool_definitions_names_match_tool_names():
    defined_names = {d["name"] for d in tools.TOOL_DEFINITIONS}
    assert defined_names == tools.TOOL_NAMES
