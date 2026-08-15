"""
Ticker-to-CIK resolution for SEC EDGAR.

EDGAR identifies companies by CIK (Central Index Key), not ticker symbol, and
its REST endpoints require that CIK zero-padded to exactly 10 digits — e.g.
Apple's CIK 320193 must appear as "0000320193" in a URL like
https://data.sec.gov/submissions/CIK0000320193.json. An unpadded or
short-form CIK returns a 404. ``get_cik`` applies that padding so callers can
work with tickers instead of memorizing EDGAR's CIK formatting rules.

The ticker-to-CIK mapping itself is downloaded once from
https://www.sec.gov/files/company_tickers.json and cached to disk (via
EdgarClient) so subsequent lookups don't re-fetch it.
"""

from .edgar_client import EdgarClient

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_ticker_to_cik: dict[str, str] | None = None


def _load_ticker_map(client: EdgarClient | None = None) -> dict[str, str]:
    """Build (and memoize in-process) the ticker -> zero-padded CIK mapping."""
    global _ticker_to_cik
    if _ticker_to_cik is not None:
        return _ticker_to_cik

    client = client or EdgarClient()
    raw = client.get_json(COMPANY_TICKERS_URL)

    _ticker_to_cik = {
        entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
        for entry in raw.values()
    }
    return _ticker_to_cik


def get_cik(ticker: str, client: EdgarClient | None = None) -> str:
    """
    Return the 10-digit zero-padded CIK for ``ticker`` (e.g. "AAPL" -> "0000320193").

    Raises KeyError if the ticker isn't in SEC's company_tickers.json.
    """
    ticker_map = _load_ticker_map(client)
    try:
        return ticker_map[ticker.upper()]
    except KeyError:
        raise KeyError(f"Unknown ticker: {ticker!r}") from None
