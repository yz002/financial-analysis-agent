"""
Tests for src/data/market.py, fully offline: yfinance is monkeypatched at
`market.yf.Ticker`, and the on-disk TTL cache is redirected to a per-test
tmp_path (via `market.CACHE_DIR`) so nothing here touches the real
data/cache/ directory or hits the network.
"""

import pandas as pd
import pytest

from src.data import market


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch, tmp_path):
    """Every test gets its own empty cache dir, so no test starts warm."""
    monkeypatch.setattr(market, "CACHE_DIR", tmp_path)


class _FakeTicker:
    """Stands in for yf.Ticker(ticker); each hook is None unless a test sets it."""

    def __init__(self, history_df=None, fast_info=None, info=None):
        self._history_df = history_df
        self._fast_info = fast_info
        self._info = info

    def history(self, start=None, end=None, auto_adjust=True):
        return self._history_df if self._history_df is not None else pd.DataFrame()

    @property
    def fast_info(self):
        if self._fast_info is None:
            raise KeyError("fast_info not available")
        return self._fast_info

    @property
    def info(self):
        return self._info if self._info is not None else {}


def _install_fake_ticker(monkeypatch, **kwargs):
    fake = _FakeTicker(**kwargs)
    calls = {"n": 0}

    def factory(ticker):
        calls["n"] += 1
        return fake

    monkeypatch.setattr(market.yf, "Ticker", factory)
    return calls


def _tz_aware_ohlcv(n=10):
    idx = pd.date_range("2024-01-02", periods=n, freq="D", tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": range(n),
            "High": range(n),
            "Low": range(n),
            "Close": range(n),
            "Volume": range(n),
            "Dividends": 0.0,
            "Stock Splits": 0.0,
        },
        index=idx,
    )


# --- get_price_history -------------------------------------------------


def test_get_price_history_returns_tz_naive_index_and_ohlcv_columns(monkeypatch):
    _install_fake_ticker(monkeypatch, history_df=_tz_aware_ohlcv())
    result = market.get_price_history("AAPL", start="2024-01-01", end="2024-01-15")
    assert result.index.tz is None
    assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_get_price_history_raises_market_data_error_on_empty_history(monkeypatch):
    _install_fake_ticker(monkeypatch, history_df=pd.DataFrame())
    with pytest.raises(market.MarketDataError):
        market.get_price_history("BADTICKER")


# --- get_current_quote ---------------------------------------------------


def test_get_current_quote_shape(monkeypatch):
    _install_fake_ticker(
        monkeypatch,
        fast_info={"lastPrice": 123.45, "marketCap": 999.0, "shares": 10.0},
    )
    quote = market.get_current_quote("aapl")
    assert quote == {
        "ticker": "AAPL",
        "price": 123.45,
        "market_cap": 999.0,
        "shares_outstanding": 10.0,
    }


def test_get_current_quote_raises_market_data_error_when_fast_info_unavailable(monkeypatch):
    _install_fake_ticker(monkeypatch, fast_info=None)
    with pytest.raises(market.MarketDataError):
        market.get_current_quote("BADTICKER")


def test_get_current_quote_raises_market_data_error_when_price_is_none(monkeypatch):
    _install_fake_ticker(
        monkeypatch, fast_info={"lastPrice": None, "marketCap": None, "shares": None}
    )
    with pytest.raises(market.MarketDataError):
        market.get_current_quote("BADTICKER")


# --- get_valuation_metrics -------------------------------------------------


def test_get_valuation_metrics_shape(monkeypatch):
    _install_fake_ticker(
        monkeypatch,
        info={
            "symbol": "AAPL",
            "trailingPE": 30.0,
            "forwardPE": 28.0,
            "priceToSalesTrailing12Months": 8.0,
            "enterpriseValue": 3_000_000.0,
            "enterpriseToEbitda": 20.0,
        },
    )
    metrics = market.get_valuation_metrics("aapl")
    assert metrics == {
        "ticker": "AAPL",
        "trailing_pe": 30.0,
        "forward_pe": 28.0,
        "price_to_sales": 8.0,
        "enterprise_value": 3_000_000.0,
        "enterprise_to_ebitda": 20.0,
    }


def test_get_valuation_metrics_missing_fields_are_none(monkeypatch):
    _install_fake_ticker(monkeypatch, info={"symbol": "AAPL"})
    metrics = market.get_valuation_metrics("aapl")
    assert metrics["trailing_pe"] is None
    assert metrics["enterprise_to_ebitda"] is None


def test_get_valuation_metrics_raises_market_data_error_on_invalid_ticker(monkeypatch):
    # Observed yfinance behavior for an invalid ticker: a near-empty info dict
    # with no symbol/shortName/longName (see market.py's module docstring).
    _install_fake_ticker(monkeypatch, info={"trailingPegRatio": None})
    with pytest.raises(market.MarketDataError):
        market.get_valuation_metrics("BADTICKER")


def test_get_valuation_metrics_raises_market_data_error_on_empty_info(monkeypatch):
    _install_fake_ticker(monkeypatch, info={})
    with pytest.raises(market.MarketDataError):
        market.get_valuation_metrics("BADTICKER")


# --- TTL cache -------------------------------------------------------------


def test_ttl_cache_serves_second_call_without_refetching(monkeypatch):
    calls = _install_fake_ticker(
        monkeypatch, fast_info={"lastPrice": 1.0, "marketCap": 2.0, "shares": 3.0}
    )
    first = market.get_current_quote("MSFT")
    second = market.get_current_quote("MSFT")
    assert first == second
    assert calls["n"] == 1


def test_ttl_cache_expired_entry_triggers_refetch(monkeypatch, tmp_path):
    calls = _install_fake_ticker(
        monkeypatch, fast_info={"lastPrice": 1.0, "marketCap": 2.0, "shares": 3.0}
    )
    market.get_current_quote("MSFT", ttl_seconds=0)
    market.get_current_quote("MSFT", ttl_seconds=0)
    assert calls["n"] == 2
