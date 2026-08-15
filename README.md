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

## Test companies

- **Microsoft** — clean, predictable financials. Baseline case.
- **Nvidia** — high growth and volatile. Stresses trend logic.
- **Ford** — different sector, capital-intensive, messier. Stresses generalization.

## Architecture

Question
↓
Agent layer — decides which tools to call, in what order
↓
Data layer — SEC EDGAR (XBRL financials, filing text), market data
↓
Analysis layer — ratios, period deltas, anomaly flags, forecasts (Python, deterministic)
↓
Guardrails — source attribution, consistency checks, uncertainty flags
↓
Answer + charts + sources


## Design decisions

- **All math happens in Python, never in the model.** The LLM chooses what to analyze and
  writes the explanation. Every figure comes from a deterministic computation.
- **Every number carries its source** — which filing, which period, which computation.
  Ungrounded figures aren't shown.
- **Human-on-the-loop.** The agent produces a draft analysis. It doesn't make decisions.
- **Forecasts state their assumptions** and are labeled as projections, not predictions.

## Roadmap

- [ ] Phase 1 — Data layer (EDGAR + market data)
- [ ] Phase 2 — Analysis layer (ratios, deltas, anomalies, forecasting)
- [ ] Phase 3 — Agent layer (tool calling, reasoning loop)
- [ ] Phase 4 — Guardrails (source grounding, consistency checks)
- [ ] Phase 5 — Streamlit interface
- [ ] Phase 6 — Evaluation harness
- [ ] Phase 7 — Documentation and demo

## Stack

Python · SEC EDGAR API · pandas · scikit-learn · Streamlit · Anthropic API

## Notes on how this was built

Architecture, data modeling, guardrail design, and evaluation criteria are my own. Claude
Code was used for implementation and debugging. Build decisions are documented in the
commit history and in weekly writeups.

## Disclaimer

This is a research and educational project. Nothing it produces is financial advice.
