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

- **The CSV-upload active-business registry (`src/agent/csv_session.py`) is a single,
  process-global slot, not per-browser-session — a concurrency limitation, not just a
  persistence one.** Unlike `st.session_state` (scoped to one browser tab) or the app layer's
  already-documented "no persistence across visits" (CLAUDE.md), `set_active_csv`/
  `get_active_csv` hold exactly one normalized DataFrame per running Python process. If this
  server is ever reachable by more than one person at the same time, one person's uploaded
  financial data can be read back and answered against a *different* person's question — the
  two people's data can cross, not merely fail to carry over between one person's own visits.
  Safe only for a single-user, local-run deployment. Before this is ever run somewhere more
  than one person could access concurrently, the registry needs to be keyed by something
  session-scoped (e.g. Streamlit's own session ID) instead of a bare module-level global — not
  designed here, just flagged so it isn't missed later.

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

- **`total_liabilities` needed a fallback because some filers never report `us-gaap:Liabilities`
  at all — Walmart being the confirmed real case, and the same restatement-vintage divergence
  documented above for Q4 turns out to apply across instant concepts too.** A sweep of the
  project's cached filers (MSFT, NVDA, Ford, WMT, KR, JPMorgan, BofA Finance, Berkshire, GE) plus
  two live EDGAR lookups (Ally Financial, Charles Schwab) found: WMT has **zero**
  `us-gaap:Liabilities` facts across its entire ~18-year filing history (not sparse — the tag is
  entirely absent); `us-gaap:LiabilitiesNoncurrent` is absent from *every one* of those nine
  filers, including the two bank/broker holding companies, which instead report `Liabilities`
  directly but skip a current/noncurrent split altogether (an unclassified balance-sheet
  presentation). `statements._derive_total_liabilities` resolves `total_liabilities` per
  `period_end` through three tiers — the real `Liabilities` tag; `current_liabilities +
  liabilities_noncurrent` when both are present (rarely, given the above); and, as the tier that
  actually recovers WMT, the accounting identity `total_assets - stockholders_equity` — refusing
  outright (no partial sum) rather than falling back to summing an open-ended, unenumerable set of
  individual liability line items (accounts payable, accrued liabilities, long-term debt, deferred
  tax liabilities, ...): there's no way to prove such an enumeration is complete for an arbitrary
  filer, so a partial sum could masquerade as "total liabilities" while understating it — the same
  "refuse rather than guess" principle behind `_derive_q4`'s tiling refusal. `Assets - Equity` is
  definitionally exhaustive instead, a rearrangement of the accounting equation rather than an
  enumeration. Cross-checking that identity against filers that *do* have the direct tag (MSFT,
  NVDA, Ford annual statements) found two distinguishable, real divergence causes, both confirmed,
  not hypothetical: (1) the same filing-vintage mismatch documented above for Q4 — MSFT's
  `period_end=2016-06-30` diverges 9.1% (`total_liabilities`=$121.471B vs.
  `total_assets - stockholders_equity`=$110.378B) because its `stockholders_equity` for that
  period was sourced from a 2018-08-03 filing while `total_assets`/`total_liabilities` for the
  same `period_end` came from a 2017-08-02 filing, each concept independently deduped to its own
  latest-`filed` appearance; NVDA shows the identical pattern at smaller magnitude on two of its
  own annual periods — `period_end=2016-01-31` diverges 3.1% ($2.814B vs. $2.901B) and
  `period_end=2017-01-29` diverges 0.8% ($4.048B vs. $4.079B). This is a real, independent finding
  about the pre-existing per-concept tag-dedup/restatement behavior `get_concept`/
  `_dedupe_by_period_end` already have — not an artifact introduced by this fallback's own new
  code, which only *exposes* a discrepancy that was already latent between `total_assets` and
  `stockholders_equity` for the same `period_end`; (2) an
  NCI-inclusive `stockholders_equity` tag (see the `stockholders_equity_tag` entry below and
  commit `29904d0`'s `roe` note) understates the identity's implied liabilities by the
  noncontrolling-interest amount — Ford mixes both equity tags across its own history, a real case
  of this. Both causes are surfaced (not silently hidden) via `total_liabilities_alt_value`/
  `_alt_method`/`_diverges_from_alt`, computed opportunistically as a cross-check whenever the
  direct tag *was* used and a fallback's inputs are also available, at the same
  `_LIABILITIES_ALT_TOLERANCE = 0.005` (0.5%) `_Q4_RECONCILIATION_TOLERANCE` uses, chosen for the
  identical reason: it clears the confirmed real divergences by a wide margin while not firing on
  the many periods that match to 0.000%.

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
  `ratios.py`. The growth functions (`revenue_growth_qoq`/`_yoy`,
  `earnings_growth_qoq`/`_yoy`) additionally carry a `{column}_growth_reason`
  companion column recording *why* a `None` value is `None` — see the
  positional-shift/rolling entry below.

- **Cash flow items are commonly filed as fiscal-year-to-date cumulative figures rather than
  discrete quarters; `statements.py` now recovers a discrete quarter from that YTD data via
  `Q2 = H1 − Q1`, `Q3 = 9-month − H1`, and, as a last resort, `Q4 = FY − 9-month`
  (`statements._derive_ytd_quarters`).** This is specific to cash-flow-statement items —
  `revenue`/`gross_profit`/`operating_income`/`net_income` also pick up a meaningful share of
  extra YTD-classified ("other") facts, but their `quarterly` coverage already spans essentially
  the entire real history in every company checked, because those get a clean discrete-quarter
  figure filed alongside any YTD comparative, so this derivation is effectively a no-op for them
  (a real fact always wins over deriving one). Measured `operating_cash_flow`/`capex` coverage,
  before vs. after this fix, out of total quarterly periods on file:

  | Ticker | Concept             | Before (real + Q1+Q2+Q3-subtraction) | After (+ YTD chain) |
  |--------|---------------------|---------------------------------------|----------------------|
  | MSFT   | operating_cash_flow | 72/76 (94.7%)                          | 72/76 (94.7%, unchanged — real coverage already near-complete) |
  | MSFT   | capex               | 72/76 (94.7%)                          | 72/76 (94.7%, unchanged) |
  | NVDA   | operating_cash_flow | 19/73 (26.0%)                          | 71/73 (97.3%) |
  | NVDA   | capex               | 15/73 (20.5%)                          | 31/73 (42.5%) |
  | Ford   | operating_cash_flow | 27/72 (37.5%)                          | 71/72 (98.6%) |
  | Ford   | capex               | 20/72 (27.8%)                          | 71/72 (98.6%) |
  | WMT    | operating_cash_flow | 18/72 (25.0%)                          | 71/72 (98.6%) |
  | WMT    | capex               | 18/72 (25.0%)                          | 71/72 (98.6%) |

  NVDA `capex` improves the least in relative terms (only 16 additional quarters recovered)
  because most of its still-missing quarters predate 2015, when NVDA didn't yet file *any*
  capex-related XBRL fact (real, YTD, or otherwise) — nothing to derive from, correctly refused
  rather than guessed.

  Worked example, NVDA `capex` FY2022 (fiscal year ended 2022-01-30): real filed Q1 = $298M; H1
  (YTD, period_end 2021-08-01) = $481M; 9-month (YTD, period_end 2021-10-31) = $703M; FY (annual,
  period_end 2022-01-30) = $976M. None of Q2/Q3/Q4 had a real filed discrete-quarter fact for
  this year. Derived Q2 = H1 − Q1 = $183M; Q3 = 9-month − H1 = $222M; Q4 = FY − 9-month = $273M.

  Precedence for a fiscal year's Q4 value is: (1) a real filed Q4 fact, (2) `FY − (Q1+Q2+Q3)` via
  three real filed quarters (the pre-existing `_derive_q4` path), (3) `FY − 9-month` via this new
  YTD chain, tried only when neither (1) nor (2) already produced a value. Q2/Q3 are derived
  independently of Q4 and of each other, each from its own two adjacent real filed facts (Q3
  uses the real H1 fact, never a derived Q2); a real filed Q2/Q3 fact always wins and derivation
  never overwrites or duplicates it. Every duration concept column set gained a new
  `{concept}_derivation_method` column (`"q1q2q3_subtraction"`, `"ytd_chain"`, or `None`)
  recording which path, if any, produced a given row.

  Deliberately **not** built: a cross-check/reconciliation output for the new path — the
  equivalent of `_derive_q4`'s `q4_subtraction_value`/`q4_diverges_from_subtraction` for the
  real-Q4-vs-subtraction case. That reconciliation exists there because a real Q4 fact and the
  subtraction value are *always* simultaneously computable whenever 4 real quarterly candidates
  exist — free to compute, cheap to expose. There is no equivalent moment here: both the
  Q4-via-9-month path and the Q2/Q3 paths only ever run when the corresponding real slot is
  already confirmed empty (checked explicitly before deriving), so a real fact and a
  YTD-chain-derived value for the same period are never both in hand at once to compare. This is
  a deliberate scope decision, not an oversight — nothing rules out adding it later if a concrete
  case turns up (e.g. a company with 4 real quarterly candidates *and* a redundant 9-month YTD
  fact for the same year), but none has been found yet, and the primary coverage gap this
  feature targets is specifically the case where real quarters are absent, not where they're
  present and merely redundant with a YTD fact.

  `ratios.free_cash_flow` is correspondingly less sparse now for companies that report cash flow
  this way — it still reads `stmt["operating_cash_flow"]`/`stmt["capex"]` unchanged, gated by
  `pd.notna()`, so newly-populated derived values flow through automatically.

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

- **Resolved: the `anthropic<1.0.0` pin (see the superseded entry this replaces) has been lifted;
  `anthropic==1.0.0` is now what `requirements.txt`/`requirements-lock.txt` install.** The pin's
  original justification, re-verified in this pass: `anthropic` 1.0 dropped its `httpx` dependency
  for a Pydantic-maintained fork, `httpx2` (`anthropic.APIConnectionError.__init__`/
  `AuthenticationError.__init__` now type-hint `request`/`response` as `httpx2.Request`/
  `httpx2.Response`), and `tests/test_app.py` constructs exactly those objects with plain
  `httpx.Request(...)`/`httpx.Response(...)` to simulate a connection/auth failure for
  `src/app/main.py`'s error-mapping tests. The specific failure mode is easy to get wrong by
  testing in the wrong environment, which is worth recording explicitly: **in this project's
  long-lived dev `.venv`, simply `pip install --upgrade anthropic` (no other change) and running
  `pytest` reports 304 passed, 0 failed** — no hang, no live network call, no assertion failure,
  nothing. That result is misleading. The dev `.venv` had plain `httpx==0.28.1` already installed
  from before (an old, independent leftover, not a dependency `anthropic` 1.0 pulls back in);
  `pip install --upgrade` never removes packages the new resolution no longer needs, so `import
  httpx` in `test_app.py` kept succeeding, and the resulting `httpx.Request`/`httpx.Response`
  objects still worked with `anthropic`'s exception constructors purely because those constructors
  do no runtime isinstance/pydantic validation on `request`/`response` — the `httpx2` type hint is
  advisory only. Confirmed directly: `anthropic.APIConnectionError(request=httpx.Request(...))`
  builds fine, and `type(e.request)` is genuinely `httpx.Request`, not `httpx2.Request`. Only after
  explicitly `pip uninstall httpx httpcore` (reproducing what a genuinely clean install of
  `requirements.txt` — which never pinned `httpx` directly — actually provides once `anthropic` no
  longer transitively requires it) does the real failure surface: `ModuleNotFoundError: No module
  named 'httpx'` at `test_app.py` collection.

  Also re-verified before deciding on a fix: every *other* place `anthropic` is mocked in this
  test suite (`tests/test_agent.py`, `tests/test_evals_token_tracking.py`'s `TrackedClient`,
  `tests/test_run_evals.py`) mocks at the client-object level — `MagicMock()` standing in for the
  whole `anthropic.Anthropic` instance, `.messages.create` replaced directly — and never
  constructs or imports `httpx` at all. None of it was ever at risk from the `httpx`→`httpx2`
  swap; `test_app.py`'s two real object constructions were the only genuine integration point.
  `src/` itself never imports `httpx` either (`edgar_client.py` uses `requests`, `market.py` goes
  through `yfinance`), so the swap has zero effect on the data layer.

  Given only one file needed a change, and that change is two lines, a process-wide
  `httpx2.alias_httpx()` shim (redirecting `import httpx` for the whole interpreter, including
  anthropic's own internal retry/proxy/timeout code and any transitive `httpx` use in other
  dependencies) was rejected as solving a problem that turned out not to exist at that scope.
  Fixed instead by pointing `test_app.py` at the real thing its objects are standing in for:
  `import httpx2 as httpx` in that file, so `_httpx_request()`/the response it wraps are genuine
  `httpx2.Request`/`httpx2.Response` instances rather than same-shaped-by-luck `httpx` ones. Also
  removed the now-fully-orphaned plain `httpx`/`httpcore` packages from the dev `.venv` and from
  `requirements-lock.txt`, and confirmed the full suite (304 tests) still passes with them
  genuinely absent, plus a deliberate-break sanity check on both mocking styles (flipping an
  assertion in `test_agent.py`'s client-level mock, and in `test_app.py`'s `httpx2`-based one) to
  confirm each is actually being exercised, not passing because nothing hit the network.

  Nothing else from 1.0's removal list applies here: no `temperature`/`top_p`/`top_k` passed to
  `messages.create()` (`src/agent/agent.py`'s single call site passes only `model`, `max_tokens`,
  `system`, `tools`, `messages`), no `isinstance(x, anthropic.Stream)` checks, nothing from the
  legacy Text Completions API (`HUMAN_PROMPT`/`AI_PROMPT`/`.complete()`/`/v1/complete`) — confirmed
  by grepping the whole repo, not assumed. The Python floor also needed no work: CI
  (`.github/workflows/tests.yml`) already runs 3.14, well past 1.0's 3.10 minimum.

- **The no-arithmetic constraint is now structurally checked (`src/agent/guardrails.py`'s
  `check_figures`, wired into `run_agent`'s returned `figure_check`), but it flags rather than
  blocks, and a live verification pass against 8 real `run_agent` calls (the README's target
  questions, `claude-opus-5`) surfaced both real extraction bugs and a genuine limitation worth
  knowing about before leaning on this check.** First pass: 99 of 453 extracted figures (22%)
  came back untraced, but nearly all were extraction bugs, not model violations — Claude's own
  prose doesn't stick to ASCII: negative numbers use a true minus sign (`−`, U+2212, not `-`),
  and dates use a non-breaking hyphen (`‑`, U+2011, not `-`), so an ASCII-only sign/date regex
  missed both and leaked every digit in every date as a spurious "figure." Also found: fiscal-
  quarter labels glue the year on with an apostrophe (`FQ4'24`) rather than a space, quoting a
  tool JSON field name in prose (`` `q4_subtraction_value` ``) leaked its embedded "4" because
  the extraction regex had no word-boundary requirement before a digit, markdown numbered
  headings (`**4. Liquidity keeps eroding.**`) read as a figure "4", and natural-language dates
  ("fiscal year ended June 30, 2025") leaked the day-of-month. All six fixed (Unicode-aware
  sign/dash character classes, a word-boundary before the mantissa, an `FQ'YY` quarter pattern,
  list-ordinal and natural-language-date exclusions), with regression tests reproducing the
  exact live-run text. Re-running the patched checker against the same saved traces (no new API
  calls needed) dropped untraced figures to 8 of 330 (2.4%), breaking down as: one confirmed
  genuine violation (the model differenced two `get_ratios` margin values itself — "Nvidia
  operates roughly 21 points higher on gross margin and ~48 points higher on operating margin,"
  neither number computed by any tool, exactly what the system prompt's "no summing or
  differencing figures" line forbids); one real but minor precision slip (NVDA gross margin
  stated as "74.99%" when the underlying value, 0.749967, rounds to 75.00% at that same
  precision — looks like truncation rather than rounding); two borderline cases (Costco's
  "fiscal Q4 is a 16-17 week quarter" is true general accounting knowledge, not a company
  statistic pulled from a tool — untraced by the letter of the rule, not a fabrication); and
  four false positives by design (a forecast's "R²=0.05" and "95%" confidence interval are both
  genuinely present in `forecast_metric`'s `reason`/`confidence_interval` *string* fields, but
  `check_figures` deliberately never parses numbers out of free-text JSON fields, only real
  numeric leaves, to avoid reintroducing the same fuzzy-extraction problem one layer deeper).

  **The more consequential finding: `check_figures` can produce false negatives — a real
  violation reads as "traced" — via coincidental matching at low precision.** The "21 points"
  half of the margin-gap violation above (the twin of the "48 points" one that *did* get
  flagged) was missed entirely: "21" rounds to a match against `valuation.price_to_sales`
  (20.677921, from an unrelated `get_market_data` call) purely by chance. Undecorated small
  integers normalize to `ndigits=0` — round-to-the-nearest-whole-number — which is coarse
  enough that an unrelated field in a 330-figure candidate pool has a real chance of landing on
  the same integer. `_find_match`'s first-match (not best-match, not type-aware) semantics mean
  once *any* tool value rounds to the stated figure, it's reported as traced, with no check that
  the matched field is even the right kind of thing (a valuation ratio standing in for a margin
  delta). This isn't a bug to patch reactively — matching at the model's own stated precision is
  the whole point (see the module docstring), and a real percentage-point figure genuinely can
  coincide with an unrelated ratio at whole-number precision. It's a structural reason this
  check flags rather than blocks: a "traced" figure at low precision is weaker evidence than one
  at high precision, and this class of finding (arithmetic the model did on two real, correctly-
  cited ratios) is exactly the kind of thing worth a human's second look regardless of what
  `check_figures` reports.

- **Rolling/shift windows in `trends.py` and `ratios.py` were positional, not calendar-aware —
  fixed via a new `src/analysis/periods.py` module.** `trends.trailing_stats`
  (`.rolling(window)`) and `ratios._growth`/`ratios._ttm` (`.shift(lag)` / `.rolling(4)`) used to
  operate on row position within whatever series or statement they were given — "8 trailing
  periods" meant 8 rows back, not 8 calendar quarters — which was safe only when every
  `period_end` in the requested cadence was actually present as a row. This broke in at least one
  confirmed, non-hypothetical way: a fiscal-calendar transition producing a stub period that
  `concepts._classify_period_length` buckets as `"other"` (not `"quarterly"`/`"annual"`), which
  `get_concept`'s `period_length` filter then dropped entirely — so that period_end never appeared
  as a row in `get_statement`'s output, and a rolling/shift window spanning it silently treated
  two non-adjacent periods as adjacent. `trends.py` made this worse by calling `.dropna()` before
  windowing, additionally collapsing any column with real reporting gaps.

  The fix: `periods.find_prior_period(period_ends, i, quarters_back)` looks up "N quarters back"
  by the calendar date it should land on (`period_ends.iloc[i] - pd.DateOffset(months=3 *
  quarters_back)`) instead of by row position, within a fixed tolerance —
  `_QUARTER_STEP_TOLERANCE_DAYS = 40` for `quarters_back=1`, `_YEAR_STEP_TOLERANCE_DAYS = 21` for
  `quarters_back=4` (the only two offsets used anywhere in this codebase — `_growth`'s QoQ/YoY,
  and the agent tool schema's documented `lag` of 1 or 4). These tolerances are sized off this
  codebase's own day-span constants (`concepts._QUARTERLY_DAYS_MIN/MAX`=80/100,
  `_LONG_OPENING_QUARTER_DAYS_MAX`=125, `statements._Q4_SPAN_DAYS_MIN/MAX`=80/125) with slack for
  a real filed quarter's worst-case drift from the ~91-day nominal target, while staying under
  half the ~91-day spacing between adjacent quarters so an adjacent real quarter can never be
  mistaken for the target one — deliberately new, not-unified-with-`concepts.py`/`statements.py`
  constants, matching this codebase's existing precedent that different tolerance problems get
  their own numbers. When no row matches within tolerance, `find_prior_period` refuses —
  `(None, "insufficient_history")` if the target predates the earliest row, `(None,
  "gap_no_prior_period")` if a quarter is genuinely missing — never falling back to the nearest
  available row, the same refuse-rather-than-guess discipline `statements._derive_q4` already
  uses on its own tiling check. `ratios._growth` surfaces this as a new
  `{column}_growth_reason` companion column (`"insufficient_history"` / `"gap_no_prior_period"` /
  `"missing_value"` / `"division_by_zero"` / `None` on success); `trends.trailing_stats` surfaces
  it as a `trailing_gap: bool` column (`True` only when a real gap, not just insufficient leading
  history, broke a point's trailing window). `ratios._ttm` (needs 3 *contiguous* prior quarters,
  not one offset) and `trends.trailing_stats` (needs `window` contiguous prior quarters) don't fit
  a single calendar-offset lookup — instead they chain `quarters_back=1` hops one at a time via
  `periods.chained_trailing_window`, aborting the whole sum/mean if any hop hits a gap, reusing
  only the one tolerance that's actually empirically justified rather than inventing an untested
  tolerance for a 2-, 3-, or N-quarter offset. `growth_anomalies` no longer `.dropna()`s its
  growth series before windowing — a gap/insufficient-history row is now left in place as NaN
  (found via the same calendar lookup) so its position still corresponds to a real calendar
  quarter, rather than being dropped and letting a later row's window silently span it.

  Live-checked against every ticker with cached fixture data (MSFT, NVDA, Ford, WMT, KR) as of
  this change: none currently has an actual `period_end` gap exceeding tolerance in its quarterly
  statement — the two previously-cited historical gap mechanisms (Kroger's ~111-day long Q1;
  WMT's `operating_cash_flow`/`capex` sparsity) were already independently fixed upstream
  (`concepts._reclassify_long_opening_quarters`; `statements._derive_ytd_quarters`) before this
  change landed. So this fix has no live regression case among the tickers this project's test
  suite already tracks — coverage here is synthetic (`tests/test_periods.py`,
  `tests/test_ratios.py`, `tests/test_trends.py`), which is a legitimate, documented outcome
  rather than a shortcut: the bug is real (it's exactly what a positional `shift`/`rolling` does
  on any DataFrame with a missing row, regardless of ticker), it's just that no company currently
  in this project's cache happens to still be tripping it.

  That check ran against the full local `data/cache/` (multi-MB, untrimmed companyfacts
  payloads), not what CI actually exercises. `.github/workflows/tests.yml` instead copies
  `tests/fixtures/edgar_cache/` — a deliberately trimmed set (`scripts/build_edgar_fixtures.py`,
  ~400-900KB per ticker, cut to only `concepts.CONCEPTS`' tags) covering just MSFT/NVDA/F/WMT —
  into `data/cache/` before running tests. Two consequences: CI's four tickers see a narrower
  slice of history than the full local cache this check used, and **KR isn't in the fixture set
  at all**, so CI never exercises KR's data (including its long-Q1 case) in any test. "Tests pass
  in CI" is therefore not itself evidence that KR has no live gap — that claim rests only on this
  session's direct check against the full local cache, not on anything CI runs.

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

- **`MAX_PERIODS` (40) makes anything older than the most recent ~40 periods structurally
  unreachable — there's no way to page backward to an older window.** Confirmed by a live run:
  a question about data from before roughly 2016 can't be answered, for any ticker, no matter
  how the question is phrased. The mechanism is exact, not approximate: `statements.get_statement`'s
  `periods` argument always keeps "only the most recent `periods` rows" (see its docstring) —
  there's no offset, no start/end date, no page number, just a count back from the newest row.
  `agent/tools.py`'s `get_financial_statement`/`get_ratios` inherit this as-is (`_cap_periods`
  clamps any requested `periods`, including `null`/"full history", to `MAX_PERIODS`), and neither
  tool's schema gives the agent any parameter that could shift the window rather than just widen
  or narrow it. So even though `get_concept`/EDGAR itself has the older data, no sequence of tool
  calls the agent can make will ever surface it — asking again, rephrasing, or requesting more
  periods all land on the same most-recent-40 window. At quarterly cadence that's ~10 years back
  from the most recent filed quarter (annual cadence reaches ~40 years back, effectively a
  company's whole EDGAR history for all but the oldest filers, so this mainly bites quarterly
  questions). Left undone deliberately for now rather than adding an offset/date-range parameter:
  no evidence yet that pre-2016 quarterly questions are a real use case worth new surface area on
  `get_statement`/`get_financial_statement`/`get_ratios` (schema, system-prompt guidance, and
  tests) — see the project's general stance against speculative interfaces for phases/features
  not yet motivated by an actual need.

- **JPMorgan Chase (JPM) had zero quarterly revenue candidates for eleven straight years
  (2015–2025) because its tag isn't a bank-specific fallback that was ever added to
  `CONCEPTS["revenue"]["tags"]`.** Found by the same 21-ticker sweep that surfaced the
  Q4-reconciliation findings above. JPM tagged revenue as `Revenues` through fiscal 2013, then
  switched to `RevenuesNetOfInterestExpense` — a bank-specific tag (net interest income plus
  noninterest revenue, the standard top-line presentation for a financial institution) — starting
  with the quarter ended 2014-03-31, and has used it for every quarter since, including every
  quarter filed 2015 through the present. That tag wasn't in `CONCEPTS["revenue"]["tags"]`, so
  `get_concept('JPM', 'revenue', period_length='quarterly')` returned zero rows for that entire
  span — not degraded data, a complete blackout. Fixed by appending `RevenuesNetOfInterestExpense`
  as the lowest-priority entry in `concepts.py`'s revenue tag list (after
  `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet`,
  `SalesRevenueGoodsNet`) — lowest-priority since it's a narrow, bank-specific tag that shouldn't
  preempt the general-purpose tags for any company that also reports one of those. Verified: JPM's
  quarterly revenue now returns 55 rows spanning 2008 through the present (`Revenues` for
  2008–2013, `RevenuesNetOfInterestExpense` for 2014 onward, confirming the switchover date
  exactly), and MSFT/NVDA/Ford/Coca-Cola's quarterly revenue coverage and `tags_used` are
  unchanged — none of the four ever resolves revenue via the new tag, as expected since each
  already has a higher-priority tag with complete coverage. Full test suite (166 tests) still
  passes. Other financial institutions in the original sweep (BAC) may have the same gap; not
  re-checked here since JPM was the specific ticker reported.

- **Generalization check: the pipeline was validated against Lindsay Corporation (LNN), a
  ~$600M small-cap with an August fiscal year-end — well outside anything the codebase or test
  suite was built around.** Every fixture this project's tests and prior NOTES.md findings are
  based on (MSFT, NVDA, Ford, Coca-Cola, Walmart, and the tickers in the 21-ticker sweep above)
  is a large-cap with a calendar or near-calendar fiscal year. LNN is neither: it's a small-cap
  irrigation-equipment manufacturer with a fiscal year ending in August, meaning every quarter
  and every Q4-derivation window falls on dates nothing in `concepts.py`, `statements.py`, or
  `src/app/` was written or tested against. A manual pass through the full pipeline — ticker
  resolution (`cik_lookup.get_cik`), XBRL tag matching (`concepts.get_concept`), Q4 derivation
  across the non-calendar fiscal year (`statements._derive_q4`'s tiling check), ratio computation
  (`ratios.py`), and chart rendering at LNN's much smaller $M scale (`src/app/main.py`'s
  `build_charts`, which every other manually-verified company so far has exercised at $B scale) —
  found 45 of 46 figures traced correctly, with no code changes needed. This is evidence the
  pipeline's design (generic tag-priority lists, `period_end`-keyed joins, day-span-based
  quarter/year classification rather than calendar-month assumptions) genuinely generalizes
  beyond the large-cap companies it happened to be built and tested against, rather than
  incidentally depending on properties specific to that test set.

- **The model-differencing violation the Phase 4 pass found is not a one-off — it recurred 5
  times across the Phase 6 harness's 21-run eval pass, and the Phase 4 system-prompt tightening
  did not eliminate it.** The Phase 4 finding above (`"Nvidia operates roughly 21 points higher
  on gross margin and ~48 points higher on operating margin"`, neither number computed by any
  tool) reads like a single incident from a single trace. Phase 6's harness makes this
  measurable rather than anecdotal: across 21 real `run_agent` runs (`claude-opus-5`, 3
  repetitions of each of README's 7 target questions), auditing every one of the run's 84
  untraced figures by hand found 5 (6% of the untraced, 0.7% of all 710 figures stated) are this
  exact pattern recurring — the model stating a percentage-point gap or a multiplier between two
  real, correctly-cited tool values (`"~20+ points higher on gross margin and ~48 points higher
  on operating margin"`, `"~4-point gross margin expansion"`, `"~2.6 points"`, `"~7x larger
  base"`), computed by the model itself rather than any tool. All 5 occurrences are in the
  `amd_nvda_comparison` and `aapl_operating_margin_trend` questions specifically — both ask for a
  trend or a comparison, the exact shape of question where relaying two tool-cited numbers side
  by side invites silently differencing them, unlike a single-figure lookup. Worth stating
  plainly because it would be easy to conclude otherwise: a single clean `amd_nvda_comparison`
  run inspected by hand the day before this audit showed no such violation, which could read as
  "fixed." The full 21-run pass shows it's still a live, recurring failure mode: 3 of the 6
  relevant runs (`aapl_operating_margin_trend`'s runs 1 and 2, and `amd_nvda_comparison`'s run 3)
  contain at least one such violation — a 50% per-run rate on the two question types that invite
  it, not something the system prompt's existing "no summing or differencing figures" line has
  eliminated. `guardrails.check_figures` still only flags this (via an untraced figure), never
  blocks it — this finding is a measurement of how often that flag fires for the correct reason,
  not a new mitigation.

- **The Phase 6 eval harness's untraced-figure audit found and fixed two more `check_figures`
  extraction gaps, on top of the six the Phase 4 pass found.** Auditing all 84 untraced figures
  from the 21-run eval pass above by hand (not just counting them) found that 40 of the 84 (48%)
  were checker bugs, not ungrounded content — two new, confirmed gaps:
  - **A non-breaking hyphen (U+2011) used as a negative sign, not just U+2212.** The Phase 4 pass
    established that Claude renders dates/quarter labels/form labels with U+2011 and negative
    numbers with the true minus sign U+2212 — two cleanly separate uses, which is why
    `_SIGN_CHARS` deliberately excluded U+2011 (see its module-docstring comment prior to this
    fix). That separation turned out to be incomplete: 37 of the 84 untraced figures (44%, by far
    the single largest cause) were genuinely grounded figures — `deviation_std` values from
    `detect_anomalies`, negative `operating_income`/`net_income`/`free_cash_flow` figures from
    `get_ratios` — that Claude wrote with a U+2011 sign (`"‑2.26 trailing standard deviations"`,
    `"‑$11,557M"`), which the checker read as positive and so failed to match against the real
    (negative) tool value. Fixed by adding U+2011 to `_SIGN_CHARS`; safe despite U+2011 also
    marking word/date hyphenation because `_NUMBER_RE`'s sign group only matches immediately
    adjacent to a number, never a hyphen elsewhere in a compound word. Concentrated entirely in
    the `ford_10q_anomalies` answers (which state far more negative figures than any other
    question, given what they're about) — all 37 are from Ford's three runs (12, 13, and 12 per
    run), none from any other question.
  - **The plural form label ("10-Qs") escaped `_FORM_LABEL_RE`'s trailing `\b`.** The Phase 4 fix
    already handled `10-Q`/`8-K` written with a non-breaking hyphen, but not the plural: `\b`
    requires a non-word/word transition immediately after the label, and there is none between
    "Q" and "s" in "10-Qs" — so the whole match failed and the leading "10" leaked through as an
    untraced-looking bare integer. 3 of the 84 untraced figures (one from each
    `aapl_operating_margin_trend` run, which all cite "the 10-Qs filed on the dates shown"). Fixed
    by allowing an optional trailing "s" before the final `\b` on both the 10-K/10-Q and 8-K
    alternatives.

  Both are regression-tested in `tests/test_guardrails.py` using the actual strings from the
  saved traces, not synthetic examples. Re-scoring the same 21 saved traces with the fixed
  checker (no new API calls) confirms the effect directly: untraced figures drop from 84 to 44
  (of 710-713 candidates — the count itself shifts slightly because the newly-excluded "10-Qs"
  spans are no longer counted as candidates at all), and pooled grounding rises from 88.2% to
  93.8%. The remaining 44 untraced figures break down as: 16 markdown-table cells with no
  per-cell unit marker (a model formatting choice, not a quick regex fix); 13 where the number
  sits inside a tool result's free-text field like a forecast's "reason" string rather than a
  real JSON numeric leaf (already documented as by-design in the Phase 4 entry above); 5 the
  recurring model-differencing violation described in the entry above; 4 a numeric slash-date
  ("6/30/2024") not covered by any existing date exclusion; 3 where the negative sign is conveyed
  by a word ("operating loss of $134M," no minus sign at all) rather than any character the
  checker could catch; 2 a JSON dict key ("quarter position 1" from a forecast's seasonal
  factors) referenced in prose rather than a value; and 1 general accounting knowledge (Costco's
  16/17-week fourth quarter) — the same borderline case the Phase 4 entry above already
  documented for a different company.

- **Kroger's fiscal Q1 genuinely spans ~16 weeks (111 days), which `concepts._classify_period_length`'s
  tight `_QUARTERLY_DAYS_MIN`/`_QUARTERLY_DAYS_MAX` (80-100 days) bucketed as "other" for every one of
  Kroger's ~18 fiscal years — Kroger never had a single Q1 usable via `period_length="quarterly"`.**
  This is the mirror image of the Costco Q4 finding above — a 52/53-week retail calendar giving one
  quarter of the year a genuinely longer span than the rest — but landing on the *opening* quarter
  instead of the closing one, and at the extraction layer (`concepts.py`) rather than the derivation
  layer (`statements.py`), since unlike Costco's Q4, Kroger's Q1 isn't missing — EDGAR reports it
  directly, it was just misclassified. Simply widening `_QUARTERLY_DAYS_MAX` to cover ~111-125 days was
  considered and rejected: that bound is deliberately tight to keep a 6-/9-month year-to-date cumulative
  fact from being mistaken for a real quarter, and a blind widen has no way to tell "Kroger's real Q1"
  apart from "a company's genuine multi-month YTD fact that happens to land in the same day-span range" —
  a real, if currently unobserved, risk (checked empirically: across a 26-ticker sweep — the 21-ticker
  sweep above plus MSFT/NVDA/Ford/Coca-Cola/Costco — every single duration fact of *any* concept with a
  101-140 day span was either one of Kroger's Q1s or one of Costco's Q4s; nothing else came close, but
  that's a property of the current cached data, not a structural guarantee). Fixed with a new
  `concepts._reclassify_long_opening_quarters`, run after the base tight-window classification, that
  promotes an "other"-classified fact to "quarterly" (up to `_LONG_OPENING_QUARTER_DAYS_MAX`, 125 days,
  reusing Costco's own wide bound for symmetry) only when it passes two structural checks instead of
  trusting day-span alone: (a) its `period_start` coincides with an *annual*-classified fact's own
  `period_start` for the same concept — i.e. it opens the fiscal year, rather than closing it, which is
  what actually distinguishes Kroger's long Q1 from Costco's long Q4 (a plain "is this fact contiguous
  with something else's end" check was tried first and rejected: it doesn't discriminate at all, because
  a fiscal calendar has no gaps — the *prior* year's annual fact always ends one day before *every*
  year's Q1 starts, so every Q1 is "contiguous" with something regardless of span); and (b) it's the
  *shortest* duration fact sharing that exact `period_start` — because a 6-month YTD-through-Q2 fact
  also starts at the fiscal-year start (so it also passes check (a)), and only picking the shortest
  member of each start-sharing family is what keeps a long YTD fact from qualifying just because it
  happens to share a start with a genuine long opening quarter. This deliberately leaves Costco's own
  long Q4 classified "other" at this layer — unchanged behavior, confirmed by a before/after diff of
  `period_length` value counts across MSFT, NVDA, Ford, Coca-Cola, Walmart, and Costco (byte-for-byte
  identical) — because Costco's real filed Q4 value is already surfaced quarterly through a different,
  already-correct mechanism (`statements.py`'s discrete-Q4-fact path), and reclassifying it here would
  be redundant with, not an improvement on, that existing path. One known, accepted gap: the *current*,
  not-yet-closed fiscal year's long Q1 stays "other" until that year's 10-K is filed and cached, since
  there's no annual fact yet to confirm it against — refusing rather than guessing, the same stance
  `statements._derive_q4` already takes when it can't confirm a tiling. Verified against Kroger's own
  press release: the extracted quarter ended 2024-05-25 is $45.269B, matching the $45.3B total company
  sales Kroger reported for that quarter. `get_statement("KR", "quarterly")` now returns Kroger's real
  filed Q1 for every fiscal year that has one (16 of 17 historical Q1s; the 17th being the known gap
  above), including years where Kroger's own 10-K tags it via the discrete Item 302 quarterly-data
  footnote (`fiscal_period="FY"`, not "Q1" — another instance of the project's standing "don't trust
  fiscal_period alone" caution) rather than a 10-Q's own `fiscal_period="Q1"`.

- **A third negative-sign character (U+2013, en dash) turned up in ordinary live use of the
  Streamlit app, and the first fix for it was itself wrong — not incomplete like the U+2011/
  U+2212 fixes before it, but wrong in a specific, structural way worth recording separately.**
  Running "What was Ford's gross margin last quarter?" live surfaced 4 untraced figures the
  checker should have caught: Ford's derived Q4 2025 operating loss, written as "operating income
  of –$11.557B (–25.18% margin)" — a real, correctly-cited `get_financial_statement` value
  (`tag: "derived"`, `is_derived: True`), missed only because the model's minus sign was U+2013,
  and `_SIGN_CHARS` (at that point `\-`, U+2212, U+2011 — added one at a time as each was caught
  live, per the entries above) didn't include it. The first fix redefined `_SIGN_CHARS` as a full
  superset of `_DASH_CHARS` (`_SIGN_CHARS = _DASH_CHARS + "−"`) specifically to stop this
  one-at-a-time pattern: any character usable as a date/label-joining dash would now also be
  usable as a sign, so a fourth dash character wouldn't need its own future fix.
  **That fix was wrong, not just risky in theory** — re-scoring the Phase 6 eval harness's 21
  saved traces against it (no new API calls, same traces used throughout this file) surfaced 8
  regressions, dropping pooled grounding from 93.8% to 92.7% (44 untraced became 52). All 8 were
  the same shape: an en dash joining a *range*, not marking a sign, with its second number now
  misread as negative — "$42.4B–$99.9B" (a forecast's 95% confidence band, `costco_revenue_
  forecast`, 3 occurrences across 2 runs) and "0.842–0.846" (consecutive quarters' debt-to-assets
  ratio, `ford_10q_anomalies`, 3 runs). The distinguishing signal was always available and cheap
  to check: a genuine negative sign is preceded by whitespace or punctuation ("was –$11.557B",
  "(–2.26 trailing..."), while a range's second dash sits directly against the tail of the first
  number — a digit, "%", or a scale letter/word ("B" in "42.4B–", "2" in "0.842–") — with no space
  between them. Fixed by wrapping the sign group with a left-context lookbehind,
  `(?:(?<![\w%])(?P<sign>[...]))?`, rather than reverting the `_DASH_CHARS` superset: the
  superset itself was the right instinct (stop adding dash characters one at a time), the missing
  piece was that "is this character a dash" and "does this character mean minus *here*" are
  different questions, and only the second one needed answering per-occurrence. Re-scoring again
  confirms this closes the regression exactly: pooled grounding returns to 93.8% (44/710
  untraced, identical figure-for-figure to the pre-U+2013-fix baseline — zero regressions, zero
  incidental improvements on this saved set), while the live Ford gross-margin trace that
  motivated the whole fix still resolves fully (10/10 traced). Regression tests for both
  directions are in `tests/test_guardrails.py`, built from the actual strings in the saved traces
  rather than synthetic ones, specifically so a future widening of `_SIGN_CHARS` can't silently
  re-break the range case to fix some other sign character, or vice versa.

- **Added the slash-date exclusion the audit table had already named as an open gap ("6/30/2024"
  not covered by any date exclusion, 4 of the 44 remaining untraced figures) — `_SLASH_DATE_RE`,
  covering both a 2-digit and 4-digit year (`\b\d{1,2}/\d{1,2}/\d{2}(?:\d{2})?\b`), added to
  `_EXCLUSION_PATTERNS` alongside `_ISO_DATE_RE`/`_NL_DATE_RE`.** Motivated by yet another live run
  of the same Ford gross-margin question, whose markdown table restated each quarter's end date in
  parentheses next to its label in compact `M/D/YY` form ("Q4 2025 (12/31/25)"); with no slash-date
  exclusion, each date fragmented into up to three separate untraced-looking bare integers (month,
  day, year) — 9 across that table's 3 rows, none of them real ungrounded content. Re-scoring the
  same 21 saved traces confirms the fix does what the audit table predicted: checked candidates
  drop from 710 to 706 (the 4 predicted instances, now excluded rather than counted as untraced
  candidates at all — `msft_fy2025_fcf_run3`'s 4 untraced figures, previously unexplained beyond
  "a numeric slash-date," turn out to be exactly this), untraced drops from 44 to 40, and pooled
  grounding rises from 93.8% to **94.3%**. Regression tests cover both year lengths using the
  actual live-run table (2-digit year) and the audit table's own example string (4-digit year).

- **Three live runs of the identical question ("What was Ford's gross margin last quarter?")
  produced three different raw grounding rates — 17/17 traced, 20/28, and 16/25 — with no change
  in whether the underlying figures were actually grounded.** All three answers cited the same
  real filed and derived figures; what changed each time was purely the model's own formatting
  choice for period labels. One run wrote plain prose with no restated dates at all (17/17
  traced). Another restated each quarter's end date compactly in a markdown table ("Q4 2025
  (12/31/25)"), which fragmented into bare untraced-looking integers until the slash-date fix
  above (16/25 traced without it). A third (the original live report that started this whole
  investigation thread) got 20/28 traced via some other formatting choice never fully captured,
  since `run_agent` isn't deterministic and a later re-run reproduces a different answer shape
  rather than the same one. **This is a property of the measurement, not a bug to eventually
  eliminate: `check_figures` is sensitive to how the model chooses to write a number, not only to
  whether that number is real.** A single run's grounding rate is accordingly noisy in a way that
  has nothing to do with the agent's actual groundedness — which is exactly why the Phase 6 eval
  harness's headline number is the *pooled* rate across all 21 runs (93.8%, now 94.3% with the
  slash-date fix) rather than an average of 21 per-run ratios, and why a single low-scoring run
  (like any one of these three Ford runs) shouldn't be read as evidence of a regression on its
  own without checking what specifically went untraced first.

- **Closed the "sign conveyed by a word" gap: a negative figure stated as an unsigned magnitude
  with the sign carried by surrounding prose instead of a literal sign character** -- "the
  (derived) ~$11B Q4 2025 charge" against a real tool value of -$11,054,000,000, "operating loss
  of $134M" against -134,000,000, and "2.38 trailing standard deviations below its own baseline"
  against a `deviation_std` of -2.38. None of these have anything for `_NUMBER_RE`'s `sign` group
  to match, so three genuinely grounded, correctly cited figures across the 21 saved traces read as
  fabricated. Fixed with a narrow fallback (`_negation_match`) that only runs when the primary,
  unsigned check has already failed: retry the candidate's negation, but only accept it when *both*
  a small financial-statement-specific negation word (`_NEGATION_WORD_RE` -- "loss", "charge",
  "deficit", "negative", "shortfall", "wrote off"/"written off", "down", "fell", "below") sits
  within `_NEGATION_WORD_WINDOW` (40 characters, sized to the farthest of the three live cases
  above) of the candidate, *and* a real tool value matches the negated magnitude at the candidate's
  own stated precision. Either check alone was too weak to ship: the word alone is exactly the
  false-positive case the fix set out to avoid -- "revenue was down 5%" has "down" sitting right
  against "5%", but if 5% is a genuinely positive growth rate (deceleration, not decline) there's
  no matching -5% tool value, so the fallback never fires and the primary unsigned check traces it
  normally; the tool value alone isn't sufficient either, since this codebase's numbers are small
  enough that two unrelated figures can coincide by magnitude, so requiring a nearby word too keeps
  an accidental collision from being misread as a sign flip. Every figure the fallback accepts sets
  a new `sign_inferred: True` field in the report row (`False` otherwise) so the flip stays legible
  to a caller rather than silently changing `normalized_value`'s meaning. Re-scoring the same 21
  saved traces confirms exactly the 3 predicted figures flip from untraced to traced -- no other
  figure's trace status changes in either direction -- and pooled grounding rises from 94.3% to
  **94.8%** (669/706). Regression tests in `tests/test_guardrails.py` use the actual three strings
  above, plus a "down 5%"-shaped test proving the word alone can't fabricate a match and a
  same-shaped test proving a figure that already traces positive is never touched by the fallback
  at all.

- **SEC's ticker-to-CIK mapping can repoint a ticker at a newly registered successor entity
  after a merger, reorganization, or redomiciliation — with none of the predecessor's XBRL
  history — and this looks exactly like a data gap unless it's detected explicitly.** Confirmed
  real case, found investigating a 21-ticker sweep anomaly: XOM came back with only 94 total
  `us-gaap` tags and revenue data starting only in 2025, implausible for a company Exxon's size
  with decades of filings. Root cause traced to `data.sec.gov`: Exxon redomiciled from New Jersey
  to Texas on 2026-07-01, and that reorganization created "ExxonMobil Holdings Corp" (CIK
  `2115436`) as a new SEC registrant — a **successor registrant under SEC Rule 12g-3(a)**, which
  lets a corporate reorganization's new legal entity inherit the predecessor's reporting
  obligations (and, evidently, its ticker) without itself having any filing history. SEC's
  `company_tickers.json` (what `cik_lookup._load_ticker_map` uses) already points `XOM` at CIK
  `2115436` — there's no collision or bug in the ticker map, it's correctly reflecting the
  reorganization — but that CIK's `companyfacts` has only one 10-Q on file (2 real quarterly
  `Revenues` periods, first period ending 2025-06-30) and zero 10-Ks. The company's real,
  15+-year financial history is still on EDGAR, filed under a *different* CIK (`34088`,
  438 `us-gaap` tags, `Revenues` back to 2009) that SEC's ticker map no longer associates with
  `XOM` (`tickers: []` on that CIK's own `submissions` endpoint) even though it's still actively
  filing (a 10-Q dated 2026-08-03, current Forms 3/4, 8-Ks). Confirmed this is XOM-specific, not
  an energy-sector or EDGAR-wide quirk: CVX has a normal, unsplit 622-tag history back to 2007.
  **This is a general class of problem, not an XOM quirk** — any ticker can be repointed this way
  after a corporate restructuring, and the failure mode is silent: `get_concept`/`get_statement`
  return a short-but-real series with no error, indistinguishable from "this company just doesn't
  have much history" without an explicit check. Deliberately not auto-fixed by falling back to a
  predecessor CIK: splicing two distinct legal entities' filings into one series without saying
  so would contradict this project's provenance principle (every number traces to one filing from
  one registrant), even though a fallback would produce a more "complete-looking" series. Instead,
  `statements.get_statement` now flags it: when the resolved CIK's full assembled history has
  fewer periods than a plausible minimum (`statements._MIN_PLAUSIBLE_PERIODS`, 8 quarterly / 3
  annual), it sets `df.attrs["sparse_history"]` and a `df.attrs["sparse_history_note"]` naming the
  resolved entity and CIK, stating how much history actually exists, and noting that a predecessor
  registrant may hold the rest — which `agent/tools.py`'s `get_financial_statement`/`get_ratios`
  relay into their `notes`, so the agent (and, through it, the user) sees this explanation instead
  of silently treating a 2-quarter series as XOM's whole financial history. The threshold is a
  heuristic, not a certainty — a genuinely young company (a recent IPO) can also legitimately have
  under 8 quarters on file, which is exactly why the note is phrased as "may indicate" and names
  the specific entity/CIK rather than asserting a successor-registrant situation outright.

- **Closed the coincidental-match false negative documented above (the "21 points" / `price_to_
  sales` collision): a genuine no-arithmetic violation could read as fully "traced" when its stated
  figure happened to be an undecorated small integer.** The Phase 4 live pass had already surfaced
  the underlying case — "Nvidia operates roughly 21 points higher on gross margin and ~48 points
  higher on operating margin," a real violation since neither number was computed by any tool, but
  only the "48" half got flagged; "21" traced because it rounds to `valuation.price_to_sales`
  (20.677921, an unrelated `get_market_data` field) purely by chance. Two fixes, one insufficient
  alone as predicted at the time: (1) `_find_match` now picks the *closest* qualifying candidate
  by absolute distance to the exact stated value, not merely the first one encountered in
  traversal order — a real improvement to which provenance gets cited whenever more than one tool
  value rounds to the same target, but confirmed not to touch this specific case on its own, since
  20.68 is still the only (and therefore closest) candidate that rounds to 21. (2) A new
  `_WEAK_PRECISION_FORMATS` check: a match whose only supporting evidence is a bare or
  dollar-prefixed whole number (`_format_label` returns `"plain_integer"`/`"dollar_integer"` only
  when there's no percent sign, scale suffix/word, or comma grouping — the one shape that adds no
  precision beyond "nearest integer" and whose small value range is genuinely dense with unrelated
  small numbers across a real run's tool pool) now reports `weak_match: True` and `traced: False`
  instead of being folded silently into "traced." A semantic-plausibility alternative — tagging
  which JSON fields can plausibly ground which *kind* of claim, so a margin-point delta could never
  match a valuation ratio — was considered and rejected: this codebase's tool results have no
  stable field taxonomy to hang that classification on, and a wrong classification would just trade
  one class of false negative for a new class of false exclusion. Re-scoring the same 21 saved
  traces (no new API calls) confirms the fix does exactly what was expected — tightening, not
  loosening: figures newly reported as weak rather than fully traced drop pooled strict grounding
  from 94.8% to **93.9%** (663/706, down from 669/706), while the genuinely-untraced count (no
  candidate matches at all) is unchanged at 37/706 — every one of the 6 newly-downgraded figures
  was previously counted as a full trace and is now a weak one instead, never the reverse. All 6
  are legitimate small-integer coincidences worth a second look, not extraction bugs: 4 are real
  tool-field values that happen to share a bare integer with unrelated prose ("20" in a Costco
  forecast's methodology aside matching `historical_periods_used: 20.0`, "12" matching
  `periods_returned: 12.0`, "11" matching `assumptions.growth_rates_used: 11.0`), and one is a
  genuine coincidence of the same shape as the motivating case — Ford's anomaly answer mentioning
  "growth mode, lag 4" (an anomaly-detection parameter, not a filed figure) landed on an unrelated
  `periods[50].deviation_std` of 4.047 purely because both round to 4. The original "21 points"
  string itself predates this saved-trace corpus (it's from the earlier, non-persisted Phase 4 ad
  hoc pass, not one of the Phase 6 harness's 21 runs), so it doesn't appear in the pooled numbers
  above; a regression test in `tests/test_guardrails.py` reproduces it directly from the values
  recorded in this file, alongside a `$5`-shaped dollar-integer case, a control case proving scaled/
  percent/comma-grouped whole numbers are never downgraded, and a case proving best-match picks the
  numerically closest of two qualifying candidates rather than the first one in traversal order.

- **The last open artifact class from the "44 remaining untraced" breakdown above — a markdown
  table stating its unit once in the column header rather than per cell — is left open, not
  fixed, because it turned out to be a one-off rather than a recurring format.** The case:
  `aapl_operating_margin_trend_run2`'s answer table has `"Revenue ($B)"` / `"Operating income
  ($B)"` column headers and bare per-cell values ("94.930" vs. a real `get_financial_statement`
  value of `94,930,000,000.0`, `tag: "derived"`), so every cell in those two columns normalizes to
  its literal, unscaled value and never matches the tool's raw dollar figure — 16 untraced figures
  (8 rows × 2 columns), all 16 of them genuinely grounded content the checker simply can't scale.
  Before writing a markdown-table parser for this, every table in all 21 saved traces was scanned
  and each numeric column classified as unit-in-header-only, unit-repeated-per-cell, or
  no-unit-indicated: 16 of the 21 traces contain at least one markdown table with a numeric
  column, and 15 of those 16 tables already repeat the unit in every cell (`"$94.930B"`,
  `"31.17%"`) — exactly the shape `_normalize`'s existing suffix/word/percent handling already
  covers with zero table-structure awareness. Only `aapl_operating_margin_trend_run2`'s one table
  does it the header-only way, and not even consistently for its own question: the same question's
  other two runs (`run1`, `run3`) wrote the identical underlying data with per-cell units instead.
  A real fix means giving the checker markdown-table structure awareness it doesn't have today —
  detecting header/separator/body rows, mapping a candidate figure's character span to a column,
  reading a per-column unit off the header text, applying it as a scale without double-counting a
  cell that already carries its own unit (`"$94.930B"` in a `"($B)"` column), and without letting a
  header-scaled bare integer fall into the existing `weak_match` bucket (which exists specifically
  to flag *unscaled* bare integers as coincidence-prone — a header-scaled one no longer is) — real
  surface area to carry for a pattern that appeared once across 706 candidate figures in 21 runs.
  Left undone; pooled strict grounding is unchanged at 93.9% (663/706 traced), and these 16 figures
  remain correctly reported as untraced instances of this specific, now-documented gap rather than
  a checker bug worth chasing further. Revisit if this table shape shows up again in a future eval
  pass — the corpus-wide scan above is what would need re-running to tell a recurrence from another
  one-off.

- **`stockholders_equity`'s two-tag priority list can silently mix noncontrolling-interest-inclusive
  and parent-only equity bases within one company's own history — confirmed for 2 of this project's
  4 fixture companies, not a one-off edge case — now surfaced as a `get_ratios` `roe` note.**
  `CONCEPTS["stockholders_equity"]["tags"]` (`src/data/concepts.py`) lists `StockholdersEquity`
  (parent-only, excludes noncontrolling/minority interests) ahead of
  `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` (includes them);
  `get_concept` fills gaps in the first tag's coverage from the second, recording which tag won
  each period in a `tag` column that `statements.py` carries through as `stockholders_equity_tag`.
  Checked against all 3 tickers with a `conftest.py` quarterly fixture plus WMT (checked directly
  against cached EDGAR data, not fixture-backed here): MSFT and NVDA are single-sourced from
  `StockholdersEquity` only (73 and 71 rows respectively, `mixed_tags=False`) — safe to compare
  directly. Ford and Walmart both mix tags across their own history (Ford: 42 rows
  NCI-inclusive vs. 30 parent-only, majority NCI-inclusive; WMT: 40 rows parent-only vs. 34
  NCI-inclusive) — for these, even the company's own ROE trend isn't on a consistent equity basis
  period to period, and comparing either one's ROE to MSFT/NVDA's (or to each other's) mixes bases
  silently. `ratios.roe` has no awareness of which tag backed each row (it just divides
  net_income_ttm by `stmt["stockholders_equity"]`), and `roa` is unaffected since it never touches
  `stockholders_equity`. Deliberately not fixed at the data layer: per this project's provenance
  principle, `get_concept`'s existing gap-filling behavior (second tag only fills periods the first
  tag has no data for) is correct and unchanged — narrowing the tag list to `StockholdersEquity`
  only would silently drop real periods for Ford/WMT rather than fix a bug, and there's no
  well-defined "right" single tag to prefer given some companies never report the parent-only one
  at all. Fixed instead by making the ambiguity visible where it's consumed: `agent/tools.py`'s
  `get_ratios` now inspects `stmt["stockholders_equity_tag"]` when computing `roe` (only `roe`, not
  `roa`) and appends a top-level note whenever any period's equity came from the NCI-inclusive tag
  — one wording when it's the only tag used (still flags a cross-company comparability issue), a
  more pointed one when tags are mixed within the ticker's own history (flags the within-company
  trend issue too) — alongside the per-row `provenance.stockholders_equity.tag` that already
  existed and is unchanged by this fix.
