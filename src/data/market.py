"""
Market data layer via yfinance, complementing the EDGAR data in this package.

yfinance is an unofficial client that scrapes Yahoo Finance rather than
calling a supported API — it can break without warning on Yahoo-side
changes and isn't a dependable production data source. Accepted here as a
reasonable tradeoff for a portfolio project (see NOTES.md); a production
system would use a licensed market data vendor.

Unlike SEC filings, market data goes stale daily, so caching here is
TTL-based (see _is_fresh), unlike EdgarClient's cache-forever disk cache.

yfinance doesn't raise a consistent exception for an invalid/delisted
ticker across its data paths — behavior differs by call:
  - .history() returns an empty DataFrame.
  - .fast_info raises (KeyError, observed) when its lazily-computed fields
    are accessed.
  - .info returns a near-empty dict (observed: just {"trailingPegRatio": None})
    with no "symbol"/"shortName"/"longName".
Every function here normalizes all three into a single clear
MarketDataError rather than letting an empty DataFrame/dict pass through
silently.
"""

import hashlib
import json
import time

import pandas as pd
import yfinance as yf

from .edgar_client import CACHE_DIR

DEFAULT_TTL_SECONDS = 24 * 60 * 60  # daily bars / daily-refresh data need ~daily freshness

_OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
_VALUATION_FIELDS = {
    "trailing_pe": "trailingPE",
    "forward_pe": "forwardPE",
    "price_to_sales": "priceToSalesTrailing12Months",
    "enterprise_value": "enterpriseValue",
    "enterprise_to_ebitda": "enterpriseToEbitda",
}


class MarketDataError(Exception):
    """Raised when a ticker is invalid or yfinance returns no usable data."""


def _cache_path(key: str, ext: str):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"market_{digest}.{ext}"


def _is_fresh(path, ttl_seconds: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl_seconds


def get_price_history(
    ticker: str, start=None, end=None, ttl_seconds: float = DEFAULT_TTL_SECONDS
) -> pd.DataFrame:
    """
    Daily OHLCV for `ticker` as a DataFrame with a DatetimeIndex, cached to disk with a TTL.

    Prices are split/dividend-adjusted (auto_adjust=True, set explicitly
    rather than left to yfinance's own default, which has changed across
    versions). NVDA's June 2024 10-for-1 split is the concrete reason:
    unadjusted closes show a fake ~90% one-day drop at the split date,
    which would corrupt anything downstream treating this as a return
    series. Callers needing exact as-traded historical prices should know
    this returns adjusted values, not raw ticker-tape prices.

    The index is tz-naive: yfinance returns an exchange-local tz-aware
    index, but EDGAR's period dates (concepts.py) are tz-naive, and a
    tz-aware/tz-naive join raises in pandas. The tz label is stripped here
    (exchange-local calendar date kept as-is), not converted to UTC.

    Cached as pickle, not CSV: a CSV round-trip (to_csv/read_csv) changed
    the index's datetime64 resolution (datetime64[s] fresh vs. datetime64[us]
    reloaded) even after stripping tz, so a cached read was not identical
    to a fresh fetch — confirmed with DataFrame.equals() during testing.
    Pickle preserves dtypes exactly and needs no extra dependency (unlike
    parquet, which would need pyarrow).
    """
    ticker = ticker.upper()
    cache_path = _cache_path(f"price_{ticker}_{start}_{end}", "pkl")
    if _is_fresh(cache_path, ttl_seconds):
        return pd.read_pickle(cache_path)

    df = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    if df.empty:
        raise MarketDataError(
            f"No price history for {ticker!r} (start={start}, end={end}); "
            "ticker may be invalid, delisted, or has no data in this range."
        )
    df = df[_OHLCV_COLUMNS]
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.to_pickle(cache_path)
    return df


def get_current_quote(ticker: str, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> dict:
    """Latest price, market cap, and shares outstanding for `ticker`, cached to disk with a TTL."""
    ticker = ticker.upper()
    cache_path = _cache_path(f"quote_{ticker}", "json")
    if _is_fresh(cache_path, ttl_seconds):
        return json.loads(cache_path.read_text(encoding="utf-8"))

    try:
        fast_info = yf.Ticker(ticker).fast_info
        price = fast_info["lastPrice"]
        market_cap = fast_info["marketCap"]
        shares = fast_info["shares"]
    except Exception as e:
        raise MarketDataError(f"Could not get a quote for {ticker!r}: {e}") from e
    if price is None:
        raise MarketDataError(f"No quote data for {ticker!r}; ticker may be invalid.")

    quote = {
        "ticker": ticker,
        "price": price,
        "market_cap": market_cap,
        "shares_outstanding": shares,
    }
    cache_path.write_text(json.dumps(quote), encoding="utf-8")
    return quote


def get_valuation_metrics(ticker: str, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> dict:
    """
    A small set of dependable valuation fields for `ticker` (trailing/forward
    P/E, price-to-sales, enterprise value, EV/EBITDA), cached to disk with a
    TTL. yfinance's .info dict is inconsistent across tickers, so this
    exposes only a short, dependable field list rather than the dozens of
    fields .info carries that intermittently come back None. A field this
    ticker doesn't have is just None in the result; the call only fails if
    the ticker itself looks invalid (no symbol/shortName/longName at all).
    """
    ticker = ticker.upper()
    cache_path = _cache_path(f"valuation_{ticker}", "json")
    if _is_fresh(cache_path, ttl_seconds):
        return json.loads(cache_path.read_text(encoding="utf-8"))

    info = yf.Ticker(ticker).info
    if not info or not (info.get("symbol") or info.get("shortName") or info.get("longName")):
        raise MarketDataError(f"No data for {ticker!r}; ticker may be invalid.")

    metrics = {"ticker": ticker, **{k: info.get(v) for k, v in _VALUATION_FIELDS.items()}}
    cache_path.write_text(json.dumps(metrics), encoding="utf-8")
    return metrics
