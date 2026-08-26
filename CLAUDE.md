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

Tests: `pytest` from the repo root (`tests/`, config in `pytest.ini`). Runs offline once
`data/cache/` is warm for MSFT, NVDA, and Ford — the three companies `tests/conftest.py`'s
fixtures are built around (Ford deliberately, since it has no `gross_profit` data at all and
exercises the graceful-degradation paths) — plus WMT, called directly rather than via a fixture,
for `test_statements.py`'s Q4-filed-vs-subtraction reconciliation regression tests.
`data/cache/` is gitignored, though, so a fresh
clone's *first* `pytest` run makes live requests to SEC EDGAR to populate it (needs network
access and a valid `SEC_USER_AGENT`) — only subsequent runs are actually offline. Run a single
file/test with `pytest tests/test_ratios.py` or `pytest tests/test_ratios.py::test_name`. No
linter or build tooling is set up yet (`evals/` and `notebooks/` are currently empty
placeholders).

Modules other than tests have no `__main__` entry points — exercise them by importing functions in
a Python session or a one-liner, e.g.:

```
python -c "from src.data.concepts import get_concept; print(get_concept('AAPL', 'revenue'))"
```

Dependencies: `pip install -r requirements.txt` (Python, requests, pandas, numpy,
python-dotenv, matplotlib, yfinance, anthropic, pytest).

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

The data, analysis, agent, and app layers (`src/data/`, `src/analysis/`, `src/agent/`,
`src/app/`) are built; see README.md's roadmap for what's left (Phase 6 evaluation harness,
Phase 7 documentation and demo).

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

### Analysis layer (`src/analysis/`)

- **`statements.py`** — `get_statement(ticker, period_length="quarterly", periods=None)`: joins
  all 13 `CONCEPTS` (the original 9 plus `stockholders_equity`/`current_assets`/
  `current_liabilities`, added specifically to unblock ROE/current ratio below, plus
  `liabilities_noncurrent`, a fallback input for `total_liabilities` below that's populated for
  almost no real filer) into one wide
  DataFrame per ticker, indexed by `period_end` — never `fiscal_year`, per this project's core
  design principle. Every ticker gets the identical column schema regardless of what data is
  actually available (a concept with zero usable data, e.g. Ford's `gross_profit`, still gets its
  columns, filled with NaN/None/False) so `ratios.py` never needs `hasattr`/`in df.columns`
  guards. This is also the one place in the codebase allowed to synthesize a Q4 value
  (`Q4 = FY − (Q1+Q2+Q3)`, per concepts.py's documented Q4 problem) — every derived row is marked
  via a companion `{concept}_is_derived` boolean column, and derivation is refused (leaving that
  concept/year absent rather than emitting a wrong number) unless the real Q1/Q2/Q3 periods
  actually tile the fiscal year within a small tolerance; see the module docstring for exactly
  what's checked and why the check is about period *tiling*, not the arithmetic. When a real,
  separately filed Q4 fact exists instead (4 real quarterly candidates tiling the fiscal year —
  common for large-caps whose pre-~2021 10-Ks tagged a discrete Q4 via the now-discontinued Item
  302 footnote), that fact is used as-is (`is_derived` stays `False`, nothing is synthesized) and
  `_derive_q4` instead cross-checks it against what `FY−(Q1+Q2+Q3)` subtraction would have given,
  recording `{concept}_q4_subtraction_value`/`{concept}_q4_diverges_from_subtraction` — the two
  can genuinely disagree (confirmed on Walmart, Duke Energy, and this project's own MSFT fixture,
  by up to ~6-8% of the FY total) because the FY total's own `filed`/`tag` can come from a later,
  differently-tagged restated comparative filing than the quarters, which are filed together and
  never get that refresh; see NOTES.md. `is_derived`'s own meaning is unchanged by this — it's a
  new, additional signal, not a redefinition. Also handles a real EDGAR data quirk found while
  building this: two duration rows can share the same `period_end` with a one-day-different
  `period_start` (`_dedupe_by_period_end`; see NOTES.md). `total_liabilities` (an *instant*
  concept) gets this module's only other derivation machinery, `_derive_total_liabilities`:
  some filers (confirmed: Walmart, across its whole filing history) never report a rolled-up
  `us-gaap:Liabilities` tag at all, so it falls back, per `period_end`, to `current_liabilities +
  liabilities_noncurrent` when both are present, then to the accounting identity `total_assets -
  stockholders_equity` when both of those are present (deliberately the identity rather than an
  enumerated sum of individual liability line items — an incomplete enumeration could masquerade
  as a full total, the same "refuse rather than guess" reasoning as the Q4-tiling check), refusing
  only when neither fallback's inputs are available. `total_liabilities_derivation_method` records
  which of `"direct_tag"`/`"current_plus_noncurrent_sum"`/`"assets_minus_equity_identity"`/`None`
  applies; when the direct tag was used, `total_liabilities_alt_value`/`_alt_method`/
  `_diverges_from_alt` opportunistically cross-check it against the best available fallback — a
  divergence there reflects the same filing-vintage-mismatch phenomenon as the Q4 case, or an
  NCI-inclusive `stockholders_equity` tag; see NOTES.md.
- **`periods.py`** — `find_prior_period(period_ends, i, quarters_back)`: looks up "N quarters
  back" (only `quarters_back=1`/QoQ and `4`/YoY are supported — the only two offsets used
  anywhere in this codebase) by the calendar date it should land on, within a fixed tolerance
  accounting for real fiscal-quarter length variation (a 52/53-week retail calendar, a
  reclassified long opening quarter), refusing — `(None, reason)`, never the nearest available
  row — when no row actually falls in that window. Replaces the positional `shift(n)`/`rolling(n)`
  this codebase used to rely on in `ratios.py`/`trends.py`, which silently misaligned across a
  missing quarter; see NOTES.md. `chained_trailing_window` builds a several-quarter contiguous
  window (for `ratios._ttm`/`trends.trailing_stats`) by walking single-quarter hops rather than
  guessing at an untested multi-quarter tolerance.
- **`ratios.py`** — margins, growth (QoQ/YoY), free cash flow, leverage, and returns, each a
  function taking the whole statement DataFrame and returning a DataFrame aligned by `period_end`
  with a `value` column plus the named input columns behind it. `value` columns are `dtype=object`
  holding literal `None` (never a silently-propagated `NaN`, never an exception) for anything
  uncomputable — division by zero, a missing input, or not enough history for a growth lag; the
  growth functions also carry a `{column}_growth_reason` column naming which. This
  means they don't behave like normal numeric pandas columns; see NOTES.md before doing vectorized
  math on one. Growth/TTM lookups (`_growth`, `_ttm`) use `periods.py`'s calendar-based lookup, not
  a positional row offset.
- **`trends.py`** — `trailing_stats`/`detect_anomalies` are a generic, domain-agnostic rolling
  z-score primitive (baseline excludes the point itself, to avoid look-ahead bias, via `periods.py`'s
  calendar-based lookup rather than a positional `shift`/`rolling`; a real gap sets the returned
  `trailing_gap` flag). The important piece is `growth_anomalies`: running the primitive on raw
  levels over-flags a company like NVDA, whose real, sustained triple-digit growth is itself
  statistically extreme — `growth_anomalies` runs it on period-over-period growth rates instead,
  so a *consistent* growth rate isn't flagged and only a break from it is. Read the module
  docstring before adding a new caller — it's explicit about which of two different questions each
  function answers.
- **`forecast.py`** — `forecast_metric(stmt, column, periods_ahead, method, lookback)`: the only
  place in the codebase that produces a number the company hasn't filed yet, which is why the
  agent calls it as a tool rather than ever projecting a value itself. Three methods: `"trend"`
  (OLS on the last `lookback` periods), `"growth"` (average period-over-period growth rate,
  compounded forward), `"seasonal"` (the trend plus a fiscal-quarter seasonal offset, needing at
  least 8 quarters — bucketed by row position modulo 4, not calendar month, since
  `get_statement`'s output has no `fiscal_period` to key on and many companies' fiscal quarters
  don't align to calendar ones anyway). Every returned row carries its assumptions (fitted
  slope/growth rate, historical periods used, R²/growth-rate std, seasonal factors) in
  `df.attrs["assumptions"]`, not just the docstring, so a caller has something concrete to relay.
  Refuses (`df.attrs["refused"] = True` plus a `reason`) rather than guess when there's not enough
  history, a gap in `period_end` (its own regularity check, independent of `periods.py`'s
  calendar-offset lookup — see NOTES.md), or a fit too poor to trust; raises
  `ValueError` only for structurally malformed input (unknown method/column, non-positive
  `periods_ahead`/`lookback`).

### Agent layer (`src/agent/`)

- **`tools.py`** — wraps the data/analysis layers as 6 coarse-grained Claude tool definitions
  (`get_financial_statement`, `get_ratios`, `get_market_data`, `detect_anomalies`,
  `forecast_metric`, `get_price_history`). Every tool returns a JSON string, never a raw
  DataFrame, and every value carries provenance (source tag, filed date, `is_derived`) so the
  model can cite it. `get_financial_statement` also conditionally carries `q4_subtraction_value`/
  `q4_diverges_from_subtraction` on a duration concept's entry when that period is a real filed
  Q4 fact being cross-checked against `FY−(Q1+Q2+Q3)` subtraction (see `statements.py` below) —
  present only when applicable, with an explanatory note appended when any period diverges.
  `total_liabilities`'s entry similarly carries `derivation_method` always, and `alt_value`/
  `alt_method`/`diverges_from_alt` when a direct-tag period had a fallback available to
  cross-check against (see `statements.py`'s `_derive_total_liabilities`) — `get_ratios` surfaces
  the same fields in `debt_to_assets`'s provenance and notes. Absence is
  always legible — a concept a company doesn't report (e.g. Ford's `gross_profit`) comes back
  `null` with a plain-English note, never an empty frame or a silent omission. Errors carry a
  typed `error_type` (`data_unavailable` — a fact to relay, e.g. nothing reported or an
  unrecognized ticker; `source_error` — the underlying EDGAR/market request itself failed, not
  evidence the data doesn't exist; `invalid_input` — the tool call was malformed) via the shared
  `_get_statement_or_error`/`_error` helpers, so a network failure can't be mistaken for "not
  reported." `get_financial_statement`/`get_ratios` cap at `MAX_PERIODS` (40) periods even for
  `periods=null`, and `get_price_history` downsamples to weekly bars past `MAX_DAILY_PRICE_ROWS`
  (120) trading days, to bound how much a single tool result can inflate the agent loop's
  resent-every-turn message history. `roa`/`roe` default to a trailing-twelve-month `net_income`
  numerator on a quarterly-cadence statement (see `ratios.py`), not the single quarter's figure,
  so they're comparable to a published annual ROA/ROE rather than roughly a quarter of it.
- **`agent.py`** — `run_agent(question, ...)`: a manual tool-calling loop against the Claude API
  (not the SDK's beta `tool_runner`), chosen deliberately for a hard iteration cap
  (`DEFAULT_MAX_ITERATIONS = 8`) with a legible note rather than a silent failure when hit, and a
  full call-by-call trace returned for Phase 5 (display) and Phase 6 (eval) to consume. The
  system prompt states this project's core constraint explicitly and goes further than "don't
  invent numbers": no arithmetic of the model's own on tool-returned numbers *at all*, including
  a forecast/projection framed as "illustrative math on filed figures" — a forecast question must
  go through the `forecast_metric` tool, with its `assumptions` relayed alongside the projected
  value, never hand-computed. The prompt also tells the
  model how to react to each `error_type` from `tools.py` (relay `data_unavailable`, report
  `source_error` as a failed lookup rather than working around it, fix the call on
  `invalid_input`).

### App layer (`src/app/`)

- **`main.py`** — the Streamlit UI (`streamlit run src/app/main.py` from the repo root), the
  only consumer of `run_agent`'s full return dict. Pure display/orchestration: it renders
  `final_answer`, `figure_check`, `tool_calls`, and `hit_iteration_cap` exactly as returned,
  computing or reformatting nothing itself — the no-model-arithmetic rule extends here as
  "no UI-layer arithmetic either." The figure-check panel is the most prominent result section
  (a `st.success`/`st.warning` banner plus per-figure trace status), ahead of the free-text
  answer's supporting detail, because grounding is this project's differentiator. Charts are
  built by `build_charts` from `get_financial_statement`/`get_ratios` tool results already
  present in the run's `tool_calls` — never a fresh tool/API call from the UI — and capped at
  4, ranked by relevance rather than dumping every tracked concept (a single statement call
  alone carries 12): a concept or ratio named in the question or answer text ranks first, in
  order of first mention; unranked slots fall back to `revenue` plus whatever ratio names were
  actually requested via a `get_ratios` call's `ratio_names` argument, since the model already
  signaled what it cared about when it made that call. `run_agent` has no progress-callback
  hook, so a run (30+ seconds, several sequential Claude API calls) is shown with a static
  `st.spinner` rather than live per-tool-call progress — a deliberate simplicity tradeoff, not
  an oversight; a background-thread-plus-polling upgrade for live tool-name progress was
  considered and skipped as over-building for a single-user tool. `anthropic.AuthenticationError`
  (e.g. missing `ANTHROPIC_API_KEY`) and other `anthropic.APIError` subclasses are caught around
  the `run_agent` call and shown as plain-English `st.error` banners, since `run_agent` itself
  doesn't catch them. No auth, no session persistence across runs beyond the current browser
  tab's `st.session_state`, no multi-user handling — single person, single question, by design.

## Known limitations (see NOTES.md for full detail)

- EDGAR cache (`data/cache/`) has no expiration — delete stale entries manually if needed.
- The EDGAR rate limiter doesn't coordinate across processes/instances.
- `fiscal_year`/`fiscal_period` on EDGAR facts can reflect a later filing's comparative-column
  attribution rather than the period's original context — always key on `period_end` instead.
- `get_concept` can return two duration rows for the same `period_end` differing only by a
  one-day `period_start` — `statements.py` handles this (`_dedupe_by_period_end`), but a caller
  going straight to `get_concept` should be aware duplicates are possible.
- `ratios.py`'s `value` columns are `dtype=object`/`None`, not `float64`/`NaN` — `.astype("float64")`
  before vectorized numeric ops.
- `market.py`/yfinance is not a dependable long-term data source; a production system would
  swap in a licensed market data vendor.
- `operating_cash_flow`/`capex` are commonly filed as fiscal-year-to-date cumulative figures
  rather than discrete quarters, so `ratios.free_cash_flow` is often majority-`None` for
  companies that report cash flow this way.
- The agent layer's no-arithmetic constraint (`src/agent/agent.py`'s system prompt) is enforced
  by instruction only, with no automated check that a given answer's figures actually trace back
  to a tool result.
- `agent/tools.py`'s `get_ratios` attaches provenance to ratio rows positionally (`zip`), not
  joined on `period_end` — correct today only because every `ratios.py` function preserves row
  order and count.
