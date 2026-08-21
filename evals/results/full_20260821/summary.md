# Evaluation results

Generated 2026-08-21T17:44:00.695983+00:00 - 21 total runs

## Aggregate

- Overall pass rate: 100%
- Mean grounding rate: 91%
- Iteration-cap hit rate: 0%
- Mean wall-clock time per run: 28.7s
- Total tokens: 846,884 in / 36,609 out
- Tool-call distribution: get_financial_statement=11, get_ratios=18, detect_anomalies=26, get_market_data=6, forecast_metric=12

## Per-question detail

### nvda_revenue_margin (lookup)

> What was Nvidia's revenue and gross margin last quarter?

Pass rate: 100% (3 runs) - Mean grounding rate: 100%

- Run 1: **PASS** - 11/11 figures traced, 2 iterations, 15.5s
  - [x] nvda_data_pulled: found a successful get_financial_statement call for NVDA
  - [x] revenue_figure: found 81,615,000,000.00 within 0.5% of expected 81,615,000,000.00
  - [x] gross_margin_figure: found 74.93% within 0.15pp of expected 74.93%
- Run 2: **PASS** - 13/13 figures traced, 2 iterations, 14.2s
  - [x] nvda_data_pulled: found a successful get_financial_statement call for NVDA
  - [x] revenue_figure: found 81,615,000,000.00 within 0.5% of expected 81,615,000,000.00
  - [x] gross_margin_figure: found 74.93% within 0.15pp of expected 74.93%
- Run 3: **PASS** - 16/16 figures traced, 2 iterations, 20.0s
  - [x] nvda_data_pulled: found a successful get_financial_statement call for NVDA
  - [x] revenue_figure: found 81,615,000,000.00 within 0.5% of expected 81,615,000,000.00
  - [x] gross_margin_figure: found 74.93% within 0.15pp of expected 74.93%

### msft_fy2025_fcf (lookup)

> How much free cash flow did Microsoft generate in FY2025?

Pass rate: 100% (3 runs) - Mean grounding rate: 90%

- Run 1: **PASS** - 9/9 figures traced, 2 iterations, 12.3s
  - [x] msft_ratios_pulled: found a successful get_ratios call for MSFT
  - [x] fcf_figure: found 71,611,000,000.00 within 0.5% of expected 71,611,000,000.00
- Run 2: **PASS** - 15/15 figures traced, 2 iterations, 9.3s
  - [x] msft_ratios_pulled: found a successful get_ratios call for MSFT
  - [x] fcf_figure: found 71,611,000,000.00 within 0.5% of expected 71,611,000,000.00
- Run 3: **PASS** - 10/14 figures traced, 2 iterations, 9.5s
  - [x] msft_ratios_pulled: found a successful get_ratios call for MSFT
  - [x] fcf_figure: found 71,611,000,000.00 within 0.5% of expected 71,611,000,000.00

### aapl_operating_margin_trend (trend)

> How has Apple's operating margin trended over the last 8 quarters?

Pass rate: 100% (3 runs) - Mean grounding rate: 85%

- Run 1: **PASS** - 50/52 figures traced, 2 iterations, 26.9s
  - [x] aapl_margin_pulled: found a successful get_ratios call for AAPL
  - [x] direction_matches: expected=up, up_word_count=5, down_word_count=1, flat_word_count=0
  - [x] mentions_8_quarters: looked for a '8 quarters/periods' style mention
- Run 2: **PASS** - 29/47 figures traced, 2 iterations, 20.1s
  - [x] aapl_margin_pulled: found a successful get_ratios call for AAPL
  - [x] direction_matches: expected=up, up_word_count=3, down_word_count=0, flat_word_count=0
  - [x] mentions_8_quarters: looked for a '8 quarters/periods' style mention
- Run 3: **PASS** - 53/54 figures traced, 2 iterations, 19.3s
  - [x] aapl_margin_pulled: found a successful get_ratios call for AAPL
  - [x] direction_matches: expected=up, up_word_count=4, down_word_count=0, flat_word_count=0
  - [x] mentions_8_quarters: looked for a '8 quarters/periods' style mention

### revenue_growth_direction (trend)

> Is revenue growth accelerating or decelerating for a given company?

Pass rate: 100% (3 runs) - Mean grounding rate: 100%

- Run 1: **PASS** - 0/0 figures traced, 1 iterations, 8.3s
  - [x] handles_ambiguous_company_question: asked which company/ticker to analyze
- Run 2: **PASS** - 0/0 figures traced, 1 iterations, 7.2s
  - [x] handles_ambiguous_company_question: asked which company/ticker to analyze
- Run 3: **PASS** - 0/0 figures traced, 1 iterations, 3.6s
  - [x] handles_ambiguous_company_question: asked which company/ticker to analyze

### ford_10q_anomalies (anomaly)

> Flag anything unusual in Ford's most recent 10-Q.

Pass rate: 100% (3 runs) - Mean grounding rate: 75%

- Run 1: **PASS** - 35/47 figures traced, 4 iterations, 64.7s
  - [x] ford_gross_profit_unavailable: mentions_required_terms=True, states_unavailable=True; trace_confirms_unavailable=True
- Run 2: **PASS** - 35/49 figures traced, 3 iterations, 61.6s
  - [x] ford_gross_profit_unavailable: mentions_required_terms=True, states_unavailable=True; trace_confirms_unavailable=True
- Run 3: **PASS** - 46/58 figures traced, 4 iterations, 68.7s
  - [x] ford_gross_profit_unavailable: mentions_required_terms=True, states_unavailable=True; trace_confirms_unavailable=True

### amd_nvda_comparison (comparison)

> Compare AMD and Nvidia's margin trends — who's better positioned?

Pass rate: 100% (3 runs) - Mean grounding rate: 98%

- Run 1: **PASS** - 79/79 figures traced, 3 iterations, 37.7s
  - [x] amd_margins_pulled: found a successful get_ratios call for AMD
  - [x] nvda_margins_pulled: found a successful get_ratios call for NVDA
  - [x] mentions_all_entities: all entities mentioned
  - [x] comparative_conclusion: found a comparative-verdict phrase
- Run 2: **PASS** - 59/60 figures traced, 3 iterations, 44.2s
  - [x] amd_margins_pulled: found a successful get_ratios call for AMD
  - [x] nvda_margins_pulled: found a successful get_ratios call for NVDA
  - [x] mentions_all_entities: all entities mentioned
  - [x] comparative_conclusion: found a comparative-verdict phrase
- Run 3: **PASS** - 73/77 figures traced, 3 iterations, 53.0s
  - [x] amd_margins_pulled: found a successful get_ratios call for AMD
  - [x] nvda_margins_pulled: found a successful get_ratios call for NVDA
  - [x] mentions_all_entities: all entities mentioned
  - [x] comparative_conclusion: found a comparative-verdict phrase

### costco_revenue_forecast (forecast)

> Project Costco's revenue for the next two quarters and explain the assumptions.

Pass rate: 100% (3 runs) - Mean grounding rate: 86%

- Run 1: **PASS** - 33/39 figures traced, 3 iterations, 33.0s
  - [x] forecast_assumptions_or_refusal: forecast_available=true; projection_stated=True; mentions_assumptions=True
- Run 2: **PASS** - 28/33 figures traced, 3 iterations, 35.1s
  - [x] forecast_assumptions_or_refusal: forecast_available=true; projection_stated=True; mentions_assumptions=True
- Run 3: **PASS** - 35/40 figures traced, 3 iterations, 38.4s
  - [x] forecast_assumptions_or_refusal: forecast_available=true; projection_stated=True; mentions_assumptions=True
