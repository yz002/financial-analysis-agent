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
from pathlib import Path

import pandas as pd
import pytest

from src.agent import csv_session, tools
from src.analysis import csv_statement
from src.data import csv_ingest
from src.data.concepts import ConceptNotFoundError

CSV_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_small_business.csv"
_CSV_UPLOADED_AT = pd.Timestamp("2025-06-01 12:00:00").to_pydatetime()
_CSV_MAPPING = {
    "Quarter Ending": "period_end", "Total Revenue": "revenue", "Net Income": "net_income",
    "Total Assets": "total_assets", "Total Liabilities": "total_liabilities",
    "Cash on Hand": "cash", "Owner's Equity": "stockholders_equity", "Internal Notes": "unmapped",
}


@pytest.fixture(autouse=True)
def _reset_csv_session():
    """csv_session's active-CSV registry is a process-global (see src/agent/csv_session.py) --
    reset it before and after every test in this file so a CSV set active by one test can never
    leak into an unrelated EDGAR-only test, and so tests can rely on "no active CSV" as their
    starting state without depending on test execution order."""
    csv_session.set_active_csv(None)
    yield
    csv_session.set_active_csv(None)


def _normalized_csv_statement():
    raw, error = csv_ingest.parse_csv(
        CSV_FIXTURE_PATH.read_bytes(), "sample_small_business.csv", uploaded_at=_CSV_UPLOADED_AT
    )
    assert error is None, error
    df, errors, _warnings = csv_statement.normalize(raw, _CSV_MAPPING, entity_name="Test Bakery LLC")
    assert errors == [], errors
    return df


def _make_statement(n_periods, missing=(), start="2020-03-31", filed_tag="SomeTag"):
    """
    A synthetic statement DataFrame with the same column shape
    statements.get_statement produces: period_end, period_start, and per
    concept in tools.ALL_CONCEPTS: "{concept}", "{concept}_tag",
    "{concept}_filed", and (duration concepts only) "{concept}_is_derived",
    "{concept}_q4_subtraction_value" (default NaN -- not applicable),
    "{concept}_q4_diverges_from_subtraction" (default False). total_liabilities
    additionally gets "total_liabilities_is_derived" (False),
    "total_liabilities_derivation_method" ("direct_tag" when present, None
    when missing -- see statements._derive_total_liabilities), and
    "total_liabilities_alt_value"/"_alt_method"/"_diverges_from_alt" (NaN/
    None/False -- no cross-check computed by default).
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
        if concept == "total_liabilities":
            data["total_liabilities_is_derived"] = [False] * n_periods
            data["total_liabilities_derivation_method"] = [
                None if concept in missing else "direct_tag"
            ] * n_periods
            data["total_liabilities_alt_value"] = [float("nan")] * n_periods
            data["total_liabilities_alt_method"] = [None] * n_periods
            data["total_liabilities_diverges_from_alt"] = [False] * n_periods

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
        if concept == "total_liabilities":
            # total_liabilities is the one instant concept with its own derivation
            # machinery (see statements._derive_total_liabilities) -- it always carries
            # is_derived/derivation_method, unlike every other instant concept.
            assert entry["is_derived"] is False
            assert entry["derivation_method"] == "direct_tag"
        else:
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


def test_get_financial_statement_total_liabilities_derivation_fields(monkeypatch):
    stmt = _make_statement(4)
    derived_idx = stmt.index[-1]
    stmt.loc[derived_idx, "total_liabilities_is_derived"] = True
    stmt.loc[derived_idx, "total_liabilities_derivation_method"] = "assets_minus_equity_identity"
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_financial_statement("msft", periods=4))

    derived_period = result["periods"][-1]
    assert derived_period["total_liabilities"]["is_derived"] is True
    assert derived_period["total_liabilities"]["derivation_method"] == "assets_minus_equity_identity"

    real_period = result["periods"][0]
    assert real_period["total_liabilities"]["is_derived"] is False
    assert real_period["total_liabilities"]["derivation_method"] == "direct_tag"

    assert any("derived from" in n and "total_liabilities" in n for n in result["notes"])


def test_get_financial_statement_total_liabilities_alt_divergence_note(monkeypatch):
    stmt = _make_statement(4)
    diverging_idx = stmt.index[-1]
    stmt.loc[diverging_idx, "total_liabilities_alt_value"] = 999.0
    stmt.loc[diverging_idx, "total_liabilities_alt_method"] = "assets_minus_equity_identity"
    stmt.loc[diverging_idx, "total_liabilities_diverges_from_alt"] = True
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_financial_statement("msft", periods=4))

    diverging_period = result["periods"][-1]
    assert diverging_period["total_liabilities"]["alt_value"] == 999.0
    assert diverging_period["total_liabilities"]["alt_method"] == "assets_minus_equity_identity"
    assert diverging_period["total_liabilities"]["diverges_from_alt"] is True

    non_diverging_period = result["periods"][0]
    assert "alt_value" not in non_diverging_period["total_liabilities"]

    assert any("diverges" in n.lower() and "total_liabilities" in n for n in result["notes"])


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


def test_get_ratios_debt_to_assets_provenance_includes_derivation_method(monkeypatch):
    stmt = _make_statement(4)
    derived_idx = stmt.index[-1]
    stmt.loc[derived_idx, "total_liabilities_is_derived"] = True
    stmt.loc[derived_idx, "total_liabilities_derivation_method"] = "assets_minus_equity_identity"
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_ratios("WMT", ratio_names=["debt_to_assets"]))

    derived_row = result["ratios"]["debt_to_assets"][-1]
    assert derived_row["provenance"]["total_liabilities"]["is_derived"] is True
    assert (
        derived_row["provenance"]["total_liabilities"]["derivation_method"]
        == "assets_minus_equity_identity"
    )
    real_row = result["ratios"]["debt_to_assets"][0]
    assert real_row["provenance"]["total_liabilities"]["derivation_method"] == "direct_tag"
    assert any("derived from" in n and "total_liabilities" in n for n in result["notes"])


def test_get_ratios_debt_to_assets_alt_divergence_note_and_provenance(monkeypatch):
    stmt = _make_statement(4)
    diverging_idx = stmt.index[-1]
    stmt.loc[diverging_idx, "total_liabilities_alt_value"] = 999.0
    stmt.loc[diverging_idx, "total_liabilities_alt_method"] = "assets_minus_equity_identity"
    stmt.loc[diverging_idx, "total_liabilities_diverges_from_alt"] = True
    _install_statement(monkeypatch, stmt)
    result = json.loads(tools.get_ratios("MSFT", ratio_names=["debt_to_assets"]))

    diverging_row = result["ratios"]["debt_to_assets"][-1]
    assert diverging_row["provenance"]["total_liabilities"]["alt_value"] == 999.0
    assert diverging_row["provenance"]["total_liabilities"]["diverges_from_alt"] is True
    assert any("diverges" in n.lower() and "total_liabilities" in n for n in result["notes"])


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


# --- get_csv_statement / get_csv_ratios --------------------------------------


def test_get_csv_statement_no_active_csv_is_data_unavailable():
    result = json.loads(tools.get_csv_statement())
    assert result["error_type"] == "data_unavailable"
    assert result["business_name"] is None


def test_get_csv_ratios_no_active_csv_is_data_unavailable():
    result = json.loads(tools.get_csv_ratios())
    assert result["error_type"] == "data_unavailable"


def test_get_csv_statement_shape_and_citation_fields():
    csv_session.set_active_csv(_normalized_csv_statement())
    result = json.loads(tools.get_csv_statement())

    assert result["business_name"] == "Test Bakery LLC"
    assert result["cadence"] == "quarterly"
    assert result["periods_returned"] == 8
    assert set(result["concepts_unavailable"]) == {
        "gross_profit", "operating_income", "operating_cash_flow", "capex",
        "current_assets", "current_liabilities", "liabilities_noncurrent",
    }

    revenue_entry = result["periods"][0]["revenue"]
    assert revenue_entry["value"] == 125000.0
    assert revenue_entry["tag"] == "Total Revenue"
    assert revenue_entry["source_file"] == "sample_small_business.csv"
    assert revenue_entry["source_row"] == 0
    assert revenue_entry["source_column"] == "Total Revenue"
    assert revenue_entry["uploaded_at"] == "2025-06-01 12:00:00"
    # No EDGAR-only derivation fields should ever appear on a CSV-sourced entry -- none of
    # that machinery applies to CSV data (see csv_statement.py's module docstring).
    for key in (
        "is_derived", "derivation_method", "q4_subtraction_value", "q4_diverges_from_subtraction",
    ):
        assert key not in revenue_entry

    assert result["periods"][0]["gross_profit"] is None


def test_get_csv_ratios_shape_and_citation_fields():
    csv_session.set_active_csv(_normalized_csv_statement())
    result = json.loads(tools.get_csv_ratios(ratio_names=["net_margin", "gross_margin"]))

    assert result["business_name"] == "Test Bakery LLC"
    assert result["cadence"] == "quarterly"
    # gross_margin's input (gross_profit) isn't mapped -- should be noted, not silently omitted.
    assert any("gross_profit" in n for n in result["notes"])

    net_margin_last = result["ratios"]["net_margin"][-1]
    assert net_margin_last["value"] is not None
    revenue_prov = net_margin_last["provenance"]["revenue"]
    assert revenue_prov["source_file"] == "sample_small_business.csv"
    assert revenue_prov["source_row"] == 7
    assert revenue_prov["source_column"] == "Total Revenue"

    gross_margin_rows = result["ratios"]["gross_margin"]
    assert all(r["value"] is None for r in gross_margin_rows)


def test_get_csv_ratios_unknown_ratio_is_invalid_input():
    result = json.loads(tools.get_csv_ratios(ratio_names=["not_a_real_ratio"]))
    assert result["error_type"] == "invalid_input"


def test_get_csv_ratios_single_period_roa_roe_use_annual_semantics():
    """A single-period CSV has no detected cadence (None) -- roa/roe should still compute a
    real value by treating it as annual (net_income used directly), not silently return None
    for lack of a trailing window that could never exist regardless of cadence."""
    df_raw = pd.DataFrame(
        {"Date": ["2024-12-31"], "Revenue": ["100000"], "NetIncome": ["10000"], "Assets": ["50000"]}
    )
    raw = csv_ingest.RawCsv(df=df_raw, filename="single.csv", uploaded_at=_CSV_UPLOADED_AT)
    mapping = {
        "Date": "period_end", "Revenue": "revenue", "NetIncome": "net_income", "Assets": "total_assets",
    }
    df, errors, _warnings = csv_statement.normalize(raw, mapping, entity_name="Single Period Co")
    assert errors == []
    csv_session.set_active_csv(df)

    result = json.loads(tools.get_csv_ratios(ratio_names=["roa"]))
    assert result["cadence"] is None
    roa_row = result["ratios"]["roa"][0]
    assert roa_row["value"] == pytest.approx(0.2)


def test_execute_tool_dispatches_get_csv_statement():
    """execute_tool's dispatch is pure name -> function, additive for the two new CSV tools --
    confirms get_csv_statement is reachable the same way every other tool is, with no special
    casing needed in execute_tool itself."""
    result = json.loads(tools.execute_tool("get_csv_statement", {}))
    assert result["error_type"] == "data_unavailable"  # no active CSV in this test


def test_csv_tool_schemas_have_no_ticker_or_period_length_input():
    for name in ("get_csv_statement", "get_csv_ratios"):
        defn = next(d for d in tools.TOOL_DEFINITIONS if d["name"] == name)
        properties = defn["input_schema"]["properties"]
        assert "ticker" not in properties
        assert "period_length" not in properties
        assert defn["input_schema"]["required"] == []


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


def test_get_market_data_degrades_cleanly_for_non_ticker_business_name(monkeypatch):
    """The system prompt tells the model never to call get_market_data for a CSV-backed
    business (no traded share price exists for a private company) -- this is the deterministic
    safety net for if it ever did anyway: a business name passed where a ticker is expected
    must still fail cleanly (data_unavailable), not crash, the same way any other unrecognized
    ticker string already does. Simulates yfinance's real behavior for an unresolvable symbol
    via the same MarketDataError mocking test_get_market_data_quote_data_unavailable uses --
    the model's actual refusal to call this tool for a CSV entity is checked live, not here."""

    def raise_mde(ticker):
        raise tools.MarketDataError(f"No quote data for {ticker!r}")

    monkeypatch.setattr(tools, "get_current_quote", raise_mde)
    monkeypatch.setattr(tools, "get_valuation_metrics", raise_mde)

    result = json.loads(tools.get_market_data("TEST BAKERY LLC"))
    assert result["quote"] is None
    assert result["quote_error"]["error_type"] == "data_unavailable"
    assert result["valuation"] is None
    assert result["valuation_error"]["error_type"] == "data_unavailable"


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
        "trailing_gap",
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
