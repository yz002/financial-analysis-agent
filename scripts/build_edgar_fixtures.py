"""
Regenerates tests/fixtures/edgar_cache/ from a live (or already-warm) EdgarClient cache.

CI has no SEC EDGAR access and no SEC_USER_AGENT secret — the tests it runs need their EDGAR
data pre-fetched and committed instead. This script builds that fixture set: the raw
companyfacts JSON for each of MSFT/NVDA/F/WMT (the tickers tests/conftest.py's fixtures and
test_statements.py's direct WMT tests are built around, per CLAUDE.md) trimmed down to only the
XBRL tags in concepts.CONCEPTS — those are the only tags get_concept/get_statement ever read —
plus the shared ticker-to-CIK mapping. Trimming cuts the full companyfacts payloads (each
company's *every* historically tagged fact) from ~18MB combined to ~2.6MB, verified to produce
byte-identical get_statement() output to the untrimmed cache before this script existed.

Run this after adding a new tag to CONCEPTS, or a new ticker to the fixtures tests rely on:

    python scripts/build_edgar_fixtures.py

Needs SEC_USER_AGENT set (via .env) if data/cache/ doesn't already have these tickers cached.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.cik_lookup import COMPANY_TICKERS_URL, get_cik  # noqa: E402
from src.data.concepts import COMPANYFACTS_URL, CONCEPTS  # noqa: E402
from src.data.edgar_client import EdgarClient  # noqa: E402

FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "edgar_cache"
TICKERS = ["MSFT", "NVDA", "F", "WMT"]


def main() -> None:
    client = EdgarClient()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    all_tags = sorted({tag for spec in CONCEPTS.values() for tag in spec["tags"]})

    ticker_map_raw = client.get_json(COMPANY_TICKERS_URL)
    _write_fixture(client, COMPANY_TICKERS_URL, ticker_map_raw)

    for ticker in TICKERS:
        cik = get_cik(ticker, client=client)
        url = COMPANYFACTS_URL.format(cik=cik)
        facts = client.get_json(url)
        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        trimmed = dict(facts)
        trimmed["facts"] = {"us-gaap": {tag: us_gaap[tag] for tag in all_tags if tag in us_gaap}}
        out_path = _write_fixture(client, url, trimmed)
        found = len(trimmed["facts"]["us-gaap"])
        print(f"{ticker}: {found}/{len(all_tags)} tags -> {out_path.name} "
              f"({out_path.stat().st_size / 1024:.0f} KB)")


def _write_fixture(client: EdgarClient, url: str, data: dict) -> Path:
    out_path = FIXTURE_DIR / client._cache_path_for(url).name
    out_path.write_text(json.dumps(data), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    main()
