# Financial Analysis Agent

An FP&A copilot that answers plain-English questions about company financials by pulling
data from SEC filings and market sources, running the analysis in Python, and explaining
the results with sources attached.

**Status:** In development. Building in public — see commit history for progress.

## The problem

Financial analysis work is repetitive: pull the filing, extract the numbers, compute the
ratios, compare across periods, write it up. Most of that is mechanical, but it still eats
hours. Meanwhile, asking an LLM directly is unreliable — it will confidently produce
numbers that aren't in any filing.

This project takes a different approach: the model decides *what* to look up and how to
explain it, but every number is computed in Python from real filing data. The LLM never
does arithmetic.

## Who it's for

Anyone who needs to understand a company's financials without spending an afternoon in
EDGAR — analysts, small business finance teams, individual investors doing their own
research.

Not an investment advisor. It reports and analyzes; the human judges.

## Target questions

These are the questions the agent needs to answer correctly. They double as the eval set.

**Lookups**
- What was Nvidia's revenue and gross margin last quarter?
- How much free cash flow did Microsoft generate in FY2025?

**Trends**
- How has Apple's operating margin trended over the last 8 quarters?
- Is revenue growth accelerating or decelerating for a given company?

**Anomalies**
- Flag anything unusual in Ford's most recent 10-Q.

**Comparison**
- Compare AMD and Nvidia's margin trends — who's better positioned?

**Forecast**
- Project Costco's revenue for the next two quarters and explain the assumptions.

## Evaluation results

Phase 6 built a harness (`evals/`) that scores the agent against the seven questions above,
with checkable per-question criteria (the right figure within tolerance for a lookup, the right
direction for a trend, correctly reporting Ford's gross profit as unavailable, a stated
projection with assumptions or an explained refusal for the forecast) rather than eyeballing the
output. It also reuses the guardrails grounding check (`figure_check`) to report what fraction of
every answer's stated figures trace back to a real tool result.

Latest run: `claude-opus-5`, 21 runs — 3 repetitions of each of the 7 questions above. **21 of 21
runs passed** their question's checks. See
[`evals/results/full_20260821/summary.md`](evals/results/full_20260821/summary.md) for the full
per-question, per-run breakdown, including every check and every traced/untraced figure.

**Grounding, audited rather than just reported.** Of 710 stated figures across all 21 runs, 5
(0.7%) are real ungrounded content — the model differencing or ratioing two real,
correctly-cited tool numbers itself (e.g. "~48 points higher on operating margin"), a known
violation of this project's no-arithmetic rule (see NOTES.md). That's the headline number, not
the raw untraced count: 44 of the 710 figures (6.2%) didn't trace at the model's stated
precision, but auditing every one by hand found 39 of those 44 are checker limitations, not
evidence the underlying numbers are wrong. Pooled grounding rate — traced figures over all
figures checked, not an average of 21 per-run ratios (a simple mean would underweight
figure-dense answers like Ford's anomaly report relative to sparse ones, understating where the
actual gap sits) — is **93.8%**.

| Cause of the 44 remaining untraced figures | Count | Real or artifact |
|---|---|---|
| Model computed a delta/ratio itself (e.g. "~48 points higher on operating margin") | 5 | **Real violation** |
| Markdown table cell with no per-cell unit marker (e.g. "94.930" meaning $94.930B) | 16 | Artifact — model formatting choice |
| Number sits inside a tool result's free-text field (a forecast's "95%"/"R²=0.05"), not a numeric JSON leaf | 13 | Artifact — by design (`check_figures` never parses free text) |
| Numeric slash-date ("6/30/2024") not covered by any date exclusion | 4 | Artifact — regex gap |
| Negative sign conveyed by a word ("operating loss of $134M"), no symbol at all | 3 | Artifact — no character to catch |
| JSON dict key referenced in prose ("quarter position 1"), not a value | 2 | Artifact — structurally ungroundable |
| General accounting knowledge (Costco's 16/17-week fourth quarter) | 1 | Artifact — borderline, not fabrication |

This audit also caught two more checker bugs, now fixed: a non-breaking hyphen (U+2011) Claude
sometimes uses as a negative sign wasn't recognized as one, and the plural "10-Qs" escaped the
form-label exclusion. Together those explained 40 of the original 84 untraced figures (see
NOTES.md for the full before/after); the table above reflects the fixed checker's current
44-untraced state, confirmed by re-scoring these same saved traces — no new API calls needed.

Other aggregate numbers from the same run: **0% iteration-cap hit rate**, ~29s mean wall-clock
time per run, ~883K tokens total across all 21 runs.

Run it yourself with `python -m evals.run_evals` (see the harness's own docstring for flags) --
it makes real Anthropic API calls, so start with `--runs 1 --questions <one id>` to smoke-test
before a fuller pass.

## Test companies

- **Microsoft** — clean, predictable financials. Baseline case.
- **Nvidia** — high growth and volatile. Stresses trend logic.
- **Ford** — different sector, capital-intensive, messier. Stresses generalization.
- **Coca-Cola** — mature, seasonal consumer staple; pre-2018 revenue is tagged under a different
  XBRL tag (`SalesRevenueGoodsNet`), stressing tag-fallback logic.

## Architecture

```
Question
   ↓
Agent layer      — decides which tools to call, in what order
   ↓
Data layer       — SEC EDGAR (XBRL financials, filing text), market data
   ↓
Analysis layer   — ratios, period deltas, anomaly flags, forecasts (Python, deterministic)
   ↓
Guardrails       — source attribution, consistency checks, uncertainty flags
   ↓
Answer + charts + sources
```


## Design decisions

- **All math happens in Python, never in the model.** The LLM chooses what to analyze and
  writes the explanation. Every figure comes from a deterministic computation.
- **Every number carries its source** — which filing, which period, which computation.
  Ungrounded figures aren't shown.
- **Human-on-the-loop.** The agent produces a draft analysis. It doesn't make decisions.
- **Forecasts state their assumptions** and are labeled as projections, not predictions.

## Roadmap

- [x] Phase 1 — Data layer
  - [x] EDGAR (ticker resolution, cached HTTP client, financial concept extraction)
  - [x] Market data (price history, quotes, valuation metrics via yfinance)
- [x] Phase 2 — Analysis layer
  - [x] Statements, ratios, growth, and anomaly detection (deterministic Python, source-attributed)
  - [x] Forecasting (trend/growth/seasonal, deterministic, assumptions surfaced for the agent to relay)
- [x] Phase 3 — Agent layer (tool calling, reasoning loop)
- [x] Phase 4 — Guardrails (source grounding, consistency checks)
- [x] Phase 5 — Streamlit interface
- [x] Phase 6 — Evaluation harness
- [ ] Phase 7 — Documentation and demo

## Stack

Python · SEC EDGAR API · pandas · Streamlit · Anthropic API

## Setup and running

```bash
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY and SEC_USER_AGENT
streamlit run src/app/main.py
```

Needs network access on first use (SEC EDGAR and the Anthropic API); EDGAR responses are
cached to `data/cache/` after that. A single query typically takes 30+ seconds — the agent
may make several tool calls before answering.

## Notes on how this was built

Architecture, data modeling, guardrail design, and evaluation criteria are my own. Claude
Code was used for implementation and debugging. Build decisions are documented in the
commit history and in weekly writeups.

## Disclaimer

This is a research and educational project. Nothing it produces is financial advice.
