# Notes

## Known limitations / future work

- **EDGAR cache has no TTL.** `EdgarClient`'s disk cache
  (`src/data/edgar_client.py`) is presence-based: once a URL's response is
  written to `data/cache/`, it's served from disk indefinitely with no
  expiration or invalidation. Fine for development, but before relying on
  this for real use it needs a TTL (e.g. re-fetch if the cache file is older
  than N hours/days) or an explicit invalidation mechanism — otherwise stale
  data (like an outdated ticker-to-CIK mapping) can persist silently forever.

- **Rate limiter doesn't coordinate across instances or processes.**
  `EdgarClient._throttle` (`src/data/edgar_client.py`) tracks
  `_last_request_time` per instance, in-process, with no locking. It keeps a
  single `EdgarClient` under 10 requests/second, but multiple instances or
  processes hitting EDGAR concurrently could collectively exceed the limit.
  Fine at current scope, but would need rethinking (e.g. a shared/external
  rate limiter) if requests are parallelized.

- **`concepts.get_concept` doesn't synthesize a Q4 value.** No company
  files a standalone Q4 report — 10-Qs cover Q1–Q3, and the 10-K reports
  the full fiscal year (`fiscal_period="FY"`), not a discrete fourth
  quarter. `get_concept` returns exactly what EDGAR reports rather than
  deriving Q4 = FY − (Q1+Q2+Q3), since a subtracted number isn't itself a
  filed fact. Callers that need an implied Q4 value have to compute it
  themselves from the `"FY"` and `"Q1"`/`"Q2"`/`"Q3"` rows of the same
  fiscal year — this is analysis-layer work, not extraction.

- **`fiscal_year`/`fiscal_period` reflect the filing's own attribution, not
  necessarily the period's "true" fiscal year.** When the same period is
  reported again as a comparative column in a later filing, EDGAR's `fy`/
  `fp` fields can shift to match that later filing's context (e.g. NVDA's
  fiscal-2022 full-year revenue shows `fiscal_year=2024` when the winning
  row — by latest `filed` date — came from the FY2024 10-K's comparative
  column). `get_concept` passes these fields through as-is; don't assume
  `fiscal_year` alone reliably groups a period to when it originally
  occurred without cross-checking `period_end`.

- **`get_concept` can return two duration rows for the same `period_end`
  with a one-day-different `period_start`.** Confirmed in real MSFT data:
  the quarter ending 2016-09-30 is tagged with `period_start=2016-07-01`
  in one filing and `period_start=2016-07-02` in another. `concepts.py`'s
  own dedup doesn't catch this because it keys duration facts on the pair
  `(period_start, period_end)`, not `period_end` alone, so these count as
  two distinct periods there. `src/analysis/statements.py`
  (`get_statement`) joins on `period_end` alone, so this surfaced there as
  a crash in the `period_start`-coalescing merge. Fixed by
  `statements._dedupe_by_period_end`, collapsing to one row per
  `period_end` with the latest `filed` date winning — the same tie-break
  rule `concepts._merge_and_dedupe` already uses for its own (finer-grained)
  dedup. Instant concepts (`total_assets`, `stockholders_equity`, etc.)
  aren't at risk of this: `concepts.py` keys their dedup on `period_end`
  alone (an instant fact has no `period_start` to begin with), so two
  instant rows can never survive concepts.py's own dedup with the same
  `period_end` — confirmed empirically across all six instant concepts for
  MSFT, NVDA, and Ford.

- **`ratios.py`'s `value` columns are `dtype=object`, not a normal numeric
  pandas column.** They hold literal Python `None` (not `NaN`) for
  anything uncomputable, so "missing" and "genuinely zero" stay
  distinguishable — but this means the column doesn't behave like a
  regular `float64` column. Vectorized numeric operations (`.sum()`,
  `.mean()`, comparisons, `.rolling()`, etc.) either raise or silently
  misbehave on an `object` column holding a mix of `float` and `None`.
  Consumers that need to do math on a ratio's `value` column must
  `.astype("float64")` first — which correctly turns any surviving `None`
  into `NaN`, pandas' own idiom once you're in a numeric-computation
  context rather than the source-attribution context where `None`
  matters. `src/analysis/trends.py` avoids this entirely by pulling raw
  numeric statement columns directly rather than going through
  `ratios.py`.

- **Cash flow items are commonly filed as fiscal-year-to-date cumulative
  figures rather than discrete quarters, so `get_concept`'s
  `period_length="quarterly"` filter correctly excludes them — and
  `statements.py` has no way to recover a discrete quarter from that YTD
  data.** Confirmed across MSFT, NVDA, Ford, and Coca-Cola:
  `operating_cash_flow`/`capex`'s `quarterly`-classified coverage runs
  25–75% below the same company's income-statement concepts (e.g. NVDA
  `capex`: only 15 of ~66 real quarters have a discrete-quarter fact at
  all; the rest exist only as 6-/9-/12-month cumulative figures). This is
  specific to cash-flow-statement items — `revenue`/`gross_profit`/
  `operating_income`/`net_income` also pick up a meaningful share of
  extra YTD-classified ("other") facts, but their `quarterly` coverage
  still spans essentially the entire real history in every company
  checked, because those get a clean discrete-quarter figure filed
  alongside any YTD comparative. `statements.get_statement` only derives
  a *missing Q4* from a fiscal year's Q1+Q2+Q3 (see its module docstring);
  it does not derive discrete quarters from mid-year YTD figures (e.g.
  Q2 = H1 − Q1, Q3 = 9-month − H1) — that's a different, not-yet-built
  piece of analysis-layer work. Until then, `ratios.free_cash_flow` is
  sparse — often majority-`None` — for companies that report cash flow
  this way.

- **`market.py` depends on yfinance, an unofficial API.** yfinance scrapes
  Yahoo Finance rather than calling a supported, licensed API — it can
  break without warning whenever Yahoo changes something on their end, and
  its failure modes for invalid data aren't consistently documented (see
  the differing behavior across `.history()`, `.fast_info`, and `.info` in
  `market.py`'s module docstring, each confirmed empirically rather than
  from official docs). This is an accepted tradeoff for a portfolio
  project — it's free and requires no API key — but it isn't a dependable
  production data source. A real deployment would use a licensed market
  data vendor instead.

- **The no-arithmetic constraint on the agent layer is enforced only by the system prompt,
  with no verification.** `src/agent/agent.py`'s system prompt tells the model never to
  compute, estimate, or recall a figure from its own knowledge — every number must come from
  a tool result — but nothing in the current code checks that this actually held for a given
  answer. A model that ignores the instruction (does arithmetic in its head, or restates a
  number with a transcription error) would currently go undetected. Phase 4 (guardrails) needs
  a check that walks the trace `run_agent` returns and verifies every numeric figure in
  `final_answer` traces back to a value that actually appears in one of that run's
  `tool_calls[].tool_result` payloads, flagging (or blocking) anything that doesn't.

- **Rolling/shift windows in `trends.py` and `ratios.py` are positional, not calendar-aware.**
  `trends.trailing_stats` (`.rolling(window)`) and `ratios._growth`/`ratios._ttm`
  (`.shift(lag)` / `.rolling(4)`) all operate on row position within whatever series or
  statement they're given — "8 trailing periods" means 8 rows back, not 8 calendar quarters.
  This is safe only when every `period_end` in the requested cadence is actually present as a
  row. It breaks in at least one confirmed, non-hypothetical way: a fiscal-calendar transition
  produces a stub period that `concepts._classify_period_length` buckets as `"other"` (not
  `"quarterly"`/`"annual"`), which `get_concept`'s `period_length` filter then drops entirely —
  so that period_end never appears as a row in `get_statement`'s output, and a rolling/shift
  window that spans it silently treats two non-adjacent periods as adjacent. `trends.py` makes
  this worse by calling `.dropna()` before windowing (in `growth_anomalies` and any caller doing
  `stmt.set_index("period_end")[metric].dropna()`), which additionally collapses any column with
  real reporting gaps — `operating_cash_flow`/`capex` are the concrete case already documented
  above (majority-`None` for companies that file cash flow as YTD-only). Fixing this properly
  means either asserting the window's `period_end` span matches its row count (and refusing like
  `statements._derive_q4` does on a tiling check) or switching to a calendar-aware rolling join
  keyed on `period_end` deltas rather than row position — not yet done anywhere in the codebase.

- **`agent/tools.py`'s `get_ratios` provenance attachment is positional, not joined on
  `period_end`.** It does `zip(stmt.itertuples(), ratio_df.itertuples())` to pair each ratio
  row with the statement row it draws tag/filed/is_derived provenance from. This is correct
  today only because every function in `ratios.py` builds its result via the shared
  `_ratio_frame` helper, which copies `stmt["period_end"]` and `stmt.index` directly — same
  length, same order, same rows, by construction. Nothing enforces that invariant at the
  `tools.py` call site, though: a future ratio function that filters or reorders rows (e.g. one
  that only returns periods where the underlying computation is meaningful) would silently
  misattribute provenance to the wrong period, with no error to catch it. Joining on
  `period_end` explicitly instead of trusting position would make that class of bug structurally
  impossible; worth doing before any new ratio function is added that doesn't go through
  `_ratio_frame` unchanged.
