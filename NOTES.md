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

- **52/53-week retail fiscal calendars give some companies a genuinely longer Q4 than Q1-Q3.**
  Costco (confirmed empirically; Walmart, Target, and Kroger are understood to follow the same
  convention) reports on a 52/53-week fiscal year with roughly 12-week Q1-Q3 quarters, but lets
  Q4 absorb the leftover week(s) — a real ~16-17 week (111-118 day) quarter, not a data error.
  `statements._derive_q4`'s tiling check originally reused `concepts._QUARTERLY_DAYS_MIN`/
  `_QUARTERLY_DAYS_MAX` (80-100 days — tight on purpose, to keep `concepts.py`'s *classification*
  of a reported duration fact as quarterly-vs-YTD-vs-other from mistaking a 6-/9-month cash-flow
  fact for a real quarter) to sanity-check the *implied* Q4 span too, which meant it refused to
  derive Q4 for Costco every single fiscal year in its history — not sporadically, all 18 years —
  even though Q1/Q2/Q3 tiled the fiscal year perfectly. Fixed by giving the implied-Q4-span check
  its own, wider bounds (`statements._Q4_SPAN_DAYS_MIN`/`_Q4_SPAN_DAYS_MAX`, 80-125): an implied
  span is never itself a reported fact that could be confused with a YTD figure, so it can safely
  tolerate the wider range a retail calendar's Q4 needs. Spot-checked against Costco's own
  reported numbers: the derived FY2025 Q4 revenue ($86.156B, which includes membership-fee
  revenue) comes out to 31.3% of FY2025's total filed revenue ($275.235B) — closely matching the
  16-of-52-weeks (30.8%) a 16-week Q4 should carry, and reconciling with the $84.4B Q4 "net sales"
  Costco's press release reports once membership fees (which "net sales" excludes but the tracked
  `revenue` concept includes) are added back in.

- **A real filed Q4 fact and `FY-(Q1+Q2+Q3)` subtraction can genuinely diverge —
  `_derive_q4` now cross-checks them instead of silently no-op'ing on the 4-candidate case.**
  An earlier diagnostic sweep's framing — "the pipeline discards a filed Q4 fact and computes a
  worse subtraction instead" — was **wrong**, and it's worth being explicit about why. When a
  large-cap's pre-~2021 10-K tags a real, discrete Q4 fact via the (now-discontinued) Item 302
  "selected quarterly financial data" footnote, `get_concept(period_length="quarterly")` already
  returns it as an ordinary row — `is_derived=False`, correct tag, correct filed date — via the
  independent quarterly-fact union in `get_statement`'s duration-concept loop, completely
  bypassing `_derive_q4`. `_derive_q4`'s refusal on those fiscal years (exactly 4 tiling
  candidates instead of 3) was a harmless no-op, not data loss: nothing was ever missing or
  discarded. What this fix actually adds is two things: (a) `_derive_q4` now explicitly
  recognizes and validates the 4-real-candidate case (`statements._four_quarters_tile_fiscal_year`)
  instead of silently passing over it, and (b) a genuinely new signal on top — cross-checking the
  real filed Q4 against what `FY-(Q1+Q2+Q3)` subtraction would give for the same fiscal year
  (`{concept}_q4_subtraction_value`/`{concept}_q4_diverges_from_subtraction`, tolerance
  `statements._Q4_RECONCILIATION_TOLERANCE`, 0.5% of the FY total). That cross-check surfaced a
  real, confirmed phenomenon: the two numbers sometimes disagree by a material amount. Root-caused
  precisely for Walmart: FY2010 filed Q4 revenue is $112.826B but `FY-(Q1+Q2+Q3)`=$115.779B (diff
  -$2.95B, ~0.7% of FY revenue); FY2011 diff -$2.90B; FY2017 diff +$4.56B (most other WMT fiscal
  years agree exactly). The three real quarters and the Q4 footnote figure are filed together,
  same vintage, same tag (e.g. all four filed 2011-03-30 as `SalesRevenueNet` for FY2010) — but
  the *annual* FY total's own `filed`/`tag` come from a *later* filing (FY2010's annual total is
  `filed=2012-03-27`, tag `Revenues`, two years after the quarters), because
  `_dedupe_by_period_end` independently keeps the latest-filed appearance of each period_end for
  the quarterly and annual series separately, and the annual total can pick up a later,
  differently-tagged restated comparative column that the already-filed quarters never get
  refreshed with. Confirmed independently on Duke Energy (FY2012 diff +$1.71B, FY2014 +$1.42B)
  and, unexpectedly, on this project's own MSFT test fixture (FY2016 filed Q4 revenue $20.614B vs.
  subtraction $26.448B, diff -$5.834B, ~6.4% of FY2016 revenue — traced to MSFT's FY2016 annual
  total being tagged `RevenueFromContractWithCustomerExcludingAssessedTax` from the FY2018 10-K's
  ASC-606-restated comparative column, filed 2018-08-03, while the FY2016 quarters remain tagged
  `SalesRevenueNet` from the FY2017 10-K, filed 2017-08-02). A 21-ticker sweep (WMT, TGT, KR, HD,
  AAPL, GOOGL, AMZN, ORCL, JPM, BAC, BRK-B, CAT, BA, GE, JNJ, PFE, UNH, XOM, CVX, O, DUK) found
  this new path fires almost as often as the existing subtraction-derivation path for `revenue`
  alone (137 fiscal-year/concept instances reconciled vs. 155 derived by subtraction), and roughly
  1 in 5 of the reconciled ones (28 of 137) diverge past the 0.5%-of-FY-total threshold —
  `q4_diverges_from_subtraction` is a common signal, not a rare edge case. It does not mean either
  number is wrong, only that they were sourced from filings of different vintages.

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
