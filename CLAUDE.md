# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An FP&A copilot that answers plain-English questions about company financials by pulling data
from SEC EDGAR and market sources, running the analysis in Python, and explaining the results
with sources attached. Not an investment advisor — it reports and analyzes; the human judges.

## Design principles (non-negotiable)

- **All math happens in Python, never in the model.** The LLM (agent layer) decides what to
  look up and writes the explanation; every figure shown is the output of a deterministic
  Python computation. Never have the LLM compute or restate a number from memory/reasoning.
- **Every number carries its source** — which filing, which period, which computation produced
  it. Ungrounded figures must not be shown. When adding a new derived metric, thread through
  enough provenance (tag, form, filed date, period) that the caller can cite it.
- **`period_end` is the time key, not `fiscal_year`.** EDGAR's `fiscal_year`/`fiscal_period`
  reflect the filing's own attribution and can shift when a period is later reported as a
  comparative column in a different filing's context (e.g. NVDA FY2022 revenue can show
  `fiscal_year=2024` if the highest-`filed` row came from the FY2024 10-K). Always group/sort/join
  periods on `period_end` (and `period_start` for duration facts); don't assume `fiscal_year`
  alone identifies when a period actually occurred.
- **No synthesized facts.** E.g. there's no filed Q4 report (10-Qs cover Q1–Q3, the 10-K covers
  full-year `fp="FY"`), so extraction never derives Q4 = FY − (Q1+Q2+Q3) — a subtracted number
  isn't itself a filed fact. Deriving values like this is analysis-layer work, done explicitly
  and labeled, never silently inserted into extraction output.
- **Forecasts state their assumptions** and are labeled as projections, not predictions.
- **Human-on-the-loop.** The agent produces a draft analysis; it doesn't make decisions.

## Commands

No test suite, linter, or build tooling is set up yet (`evals/` and `notebooks/` are currently
empty placeholders). Modules have no `__main__` entry points — exercise them by importing
functions in a Python session or a one-liner, e.g.:

```
python -c "from src.data.concepts import get_concept; print(get_concept('AAPL', 'revenue'))"
```

Dependencies: `pip install -r requirements.txt` (Python, requests, pandas, numpy,
python-dotenv, matplotlib, yfinance, anthropic).

Environment: copy `.env.example` to `.env` and set `SEC_USER_AGENT` (required — EDGAR rejects
requests without a descriptive `Name email` User-Agent) and `ANTHROPIC_API_KEY`.

## Architecture

```
Question
   ↓
Agent layer      — decides which tools to call, in what order          (src/agent/)
   ↓
Data layer       — SEC EDGAR (XBRL financials, filing text), market data (src/data/)
   ↓
Analysis layer   — ratios, period deltas, anomaly flags, forecasts (Python, deterministic) (src/analysis/)
   ↓
Guardrails       — source attribution, consistency checks, uncertainty flags
   ↓
Answer + charts + sources                                              (src/app/ — Streamlit)
```

Only the data layer (`src/data/`) is built so far; `analysis/`, `agent/`, and `app/` are empty
packages awaiting Phase 2+ (see README.md roadmap).

### Data layer (`src/data/`)

- **`edgar_client.py`** — `EdgarClient`: the only thing in this codebase that talks to EDGAR
  over HTTP. Enforces the required `SEC_USER_AGENT` header, throttles to under 10 req/s per SEC's
  fair-access guidance, and caches every response to `data/cache/` keyed by SHA-256 of the URL.
  The cache has **no TTL** — once written it's served forever until manually deleted. That's fine
  for EDGAR data (filings don't change), but means a stale ticker-to-CIK mapping or bug in the
  cached JSON survives until the cache file is removed by hand. Rate limiting is per-instance,
  in-process only — not coordinated across processes.
- **`cik_lookup.py`** — `get_cik(ticker)`: resolves ticker → zero-padded 10-digit CIK (EDGAR
  requires the padded form, e.g. `"0000320193"`, in URLs). The ticker→CIK map is fetched once
  and memoized in a module-level global, in addition to `EdgarClient`'s disk cache.
- **`concepts.py`** — `get_concept(ticker, concept_name, period_length=None)`: the core XBRL
  extraction function, returns a tidy `DataFrame` for one financial concept (revenue, net
  income, etc.). This is the most intricate module in the repo — read its module docstring
  before touching it. Key mechanics: `CONCEPTS` maps a stable concept name to a *prioritized
  list* of raw XBRL tags (companies retag the same line item over time, e.g. revenue has three
  historical tags spanning the 2018 ASC 606 transition); `get_concept` merges data from every
  tag that has usable data rather than stopping at the first match, because a single tag can be
  incomplete for a given company. Only `10-K`/`10-Q`/`10-K/A`/`10-Q/A` forms are included (no
  8-Ks). Duplicate periods (a period re-reported as a comparative column, or corrected by an
  amendment) are collapsed to the entry with the latest `filed` date. Duration facts are
  classified into `period_length` ("quarterly"/"annual"/"other") by day-span since EDGAR doesn't
  self-describe a duration fact's length and a quarterly and YTD fact can share the same
  `period_end`. Every returned `DataFrame` carries `df.attrs["tags_used"]` /
  `df.attrs["mixed_tags"]` so callers can tell when a series was stitched from more than one tag.
- **`market.py`** — price history, quotes, and valuation metrics via `yfinance` (an *unofficial*
  Yahoo Finance scraper — accepted tradeoff for this project, not production-grade; see NOTES.md).
  Unlike EDGAR data, market data goes stale daily, so this cache is **TTL-based** (default 24h),
  the opposite policy from `EdgarClient`'s cache-forever. Price history is split/dividend-adjusted
  (`auto_adjust=True`, set explicitly) and returned with a tz-naive index to match EDGAR's
  tz-naive period dates — a tz-aware/tz-naive join raises in pandas. Cached as pickle rather than
  CSV because a CSV round-trip silently changed the index's datetime64 resolution. yfinance fails
  inconsistently across its own APIs for invalid tickers (empty DataFrame / raised KeyError /
  near-empty dict depending on which call) — every function here normalizes that into a single
  `MarketDataError`.

## Known limitations (see NOTES.md for full detail)

- EDGAR cache (`data/cache/`) has no expiration — delete stale entries manually if needed.
- The EDGAR rate limiter doesn't coordinate across processes/instances.
- `get_concept` never derives a Q4 value; callers needing it must compute
  `FY − (Q1+Q2+Q3)` themselves at the analysis layer.
- `fiscal_year`/`fiscal_period` on EDGAR facts can reflect a later filing's comparative-column
  attribution rather than the period's original context — always key on `period_end` instead.
- `market.py`/yfinance is not a dependable long-term data source; a production system would
  swap in a licensed market data vendor.
