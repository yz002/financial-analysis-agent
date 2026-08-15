"""
Cached, rate-limited HTTP client for SEC EDGAR.

EDGAR requires every request to carry a descriptive ``User-Agent`` header
identifying the requester (name and contact email) — requests without one,
or with a generic one, are blocked outright. See
https://www.sec.gov/os/webmaster-faq#developers. This module reads that
header from the ``SEC_USER_AGENT`` environment variable (via python-dotenv)
so the identity lives in configuration, not in code.

EDGAR also asks automated clients to stay under 10 requests/second
(https://www.sec.gov/os/accessing-edgar-data). ``EdgarClient`` enforces that
limit locally and caches every response to disk under ``data/cache/`` so
repeat calls for the same resource never hit the network again.
"""

import hashlib
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
MIN_REQUEST_INTERVAL = 1 / 10  # 10 requests/second, per SEC's fair-access guidance

load_dotenv(PROJECT_ROOT / ".env")


class EdgarClientError(Exception):
    """Raised when SEC_USER_AGENT is missing from the environment."""


class EdgarClient:
    """
    Fetches JSON resources from SEC EDGAR with disk caching and rate limiting.

    Every response is cached to ``data/cache/`` keyed by the request URL, so
    repeated calls for the same resource (e.g. the ticker-to-CIK mapping, or
    a company's filing history) are served from disk after the first fetch.
    """

    def __init__(self, cache_dir: Path = CACHE_DIR):
        user_agent = os.getenv("SEC_USER_AGENT")
        if not user_agent:
            raise EdgarClientError(
                "SEC_USER_AGENT is not set. EDGAR rejects requests without a "
                "descriptive User-Agent (e.g. 'Your Name your@email.com'); "
                "set it in .env. See https://www.sec.gov/os/webmaster-faq#developers."
            )
        self._headers = {"User-Agent": user_agent}
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request_time = 0.0

    def get_json(self, url: str) -> dict:
        """Return the parsed JSON body at ``url``, using the on-disk cache when present."""
        cache_path = self._cache_path_for(url)
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))

        self._throttle()
        response = requests.get(url, headers=self._headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        cache_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def _cache_path_for(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _throttle(self) -> None:
        """Sleep if needed so consecutive requests stay under 10/second."""
        elapsed = time.monotonic() - self._last_request_time
        remaining = MIN_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_time = time.monotonic()
