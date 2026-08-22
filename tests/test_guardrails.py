"""
Tests for src/agent/guardrails.py's check_figures, fully offline: no network, no Anthropic
client. Synthetic run_agent-shaped result dicts are built by hand, with tool_result as a real
json.dumps() string round-tripped through json.loads() inside check_figures itself, matching
how tests/test_agent.py exercises the rest of the agent layer.
"""

import json

from src.agent import guardrails


def _tool_call(tool_name, payload, iteration=1, is_error=False):
    return {
        "iteration": iteration,
        "tool_name": tool_name,
        "tool_input": {},
        "tool_result": json.dumps(payload),
        "is_error": is_error,
    }


def _result(final_answer, tool_calls):
    return {"final_answer": final_answer, "tool_calls": tool_calls}


def _statement_call(revenue_field, period_end="2025-06-30"):
    return _tool_call(
        "get_financial_statement",
        {
            "ticker": "MSFT",
            "period_length": "annual",
            "periods_returned": 1,
            "concepts_unavailable": [],
            "notes": [],
            "periods": [{"period_end": period_end, "period_start": None, "revenue": revenue_field}],
        },
    )


def test_dollar_billion_word_form_traces_at_stated_precision():
    call = _statement_call({"value": 90007000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    report = guardrails.check_figures(_result("Revenue was $90.0 billion last year.", [call]))

    assert report["figures_checked"] == 1
    assert report["all_traced"] is True
    fig = report["figures"][0]
    assert fig["raw_text"] == "$90.0 billion"
    assert fig["precision_ndigits"] == -8
    assert fig["format"] == "dollar_scale_word"
    assert fig["traced"] is True
    assert fig["match"]["json_path"] == "periods[0].revenue.value"
    assert fig["match"]["tool_name"] == "get_financial_statement"


def test_finer_precision_statement_does_not_trace_to_insufficiently_precise_value():
    # 90,050,000,000 rounds to $90.0B (satisfies the coarse statement) but not to $90.007B.
    call = _statement_call({"value": 90050000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    report = guardrails.check_figures(_result("Revenue was $90.007 billion last year.", [call]))

    assert report["figures_checked"] == 1
    fig = report["figures"][0]
    assert fig["precision_ndigits"] == -6
    assert fig["traced"] is False
    assert fig["match"] is None
    assert report["all_traced"] is False
    assert report["figures_untraced"] == 1


def test_comma_grouped_million_word_form_traces():
    call = _statement_call({"value": 90007000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    report = guardrails.check_figures(_result("Revenue was $90,007 million.", [call]))

    fig = report["figures"][0]
    assert fig["format"] == "dollar_scale_word"
    assert fig["traced"] is True


def test_raw_comma_grouped_integer_traces():
    call = _statement_call({"value": 57006000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    report = guardrails.check_figures(_result("Revenue was 57,006,000,000.", [call]))

    fig = report["figures"][0]
    assert fig["format"] == "raw_comma_grouped"
    assert fig["traced"] is True
    assert fig["normalized_value"] == 57006000000.0


def _ratios_call(value):
    return _tool_call(
        "get_ratios",
        {
            "ticker": "MSFT",
            "period_length": "quarterly",
            "notes": [],
            "ratios": {
                "gross_margin": [
                    {
                        "period_end": "2025-06-30",
                        "value": value,
                        "inputs": {"gross_profit": 1.0, "revenue": 1.0},
                        "provenance": {},
                    }
                ]
            },
        },
    )


def test_percent_form_traces_to_decimal_fraction():
    report = guardrails.check_figures(_result("Gross margin was 45.1%.", [_ratios_call(0.451)]))

    fig = report["figures"][0]
    assert fig["format"] == "percent"
    assert fig["traced"] is True
    assert abs(fig["normalized_value"] - 0.451) < 1e-9


def test_bare_decimal_form_traces():
    report = guardrails.check_figures(_result("Gross margin was 0.451.", [_ratios_call(0.451)]))

    fig = report["figures"][0]
    assert fig["format"] == "bare_decimal"
    assert fig["traced"] is True


def test_dollar_suffix_form_traces():
    call = _tool_call(
        "get_market_data",
        {
            "ticker": "MSFT",
            "source": "yfinance",
            "as_of": "2025-08-01",
            "quote": {"price": 450.0, "market_cap": 1234000000.0, "shares_outstanding": 2.7e9},
            "valuation": None,
        },
    )
    report = guardrails.check_figures(_result("Market cap is $1.2B.", [call]))

    fig = report["figures"][0]
    assert fig["format"] == "dollar_suffix"
    assert fig["traced"] is True
    assert fig["match"]["json_path"] == "quote.market_cap"


def test_trillion_dollar_suffix_form_traces():
    # Confirmed by a live run_agent trace: "T" (trillion) wasn't in the suffix scale map, so
    # "$5.241T" normalized to 5.241 instead of 5.241e12 and failed to trace against
    # get_market_data's real market_cap/enterprise_value figures. Now that market cap is in the
    # tool set, trillion-scale figures (mega-cap tickers) come up routinely, not as an edge case.
    call = _tool_call(
        "get_market_data",
        {
            "ticker": "AAPL",
            "source": "yfinance",
            "as_of": "2026-08-20",
            "quote": {"price": 216.39, "market_cap": 5241000000000.0, "shares_outstanding": 2.4e10},
            "valuation": {"enterprise_value": 5225000000000.0},
        },
    )
    report = guardrails.check_figures(
        _result("Market cap is $5.241T and enterprise value is $5.225T.", [call])
    )

    assert report["figures_checked"] == 2
    assert report["all_traced"] is True
    assert report["figures"][0]["format"] == "dollar_suffix"
    assert report["figures"][0]["normalized_value"] == 5241000000000.0
    assert report["figures"][0]["match"]["json_path"] == "quote.market_cap"
    assert report["figures"][1]["match"]["json_path"] == "valuation.enterprise_value"


def test_trillion_word_form_traces():
    call = _statement_call({"value": 5241000000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    report = guardrails.check_figures(_result("Revenue was $5.241 trillion.", [call]))

    fig = report["figures"][0]
    assert fig["format"] == "dollar_scale_word"
    assert fig["traced"] is True
    assert fig["normalized_value"] == 5241000000000.0


def test_fabricated_figure_does_not_trace():
    call = _statement_call({"value": 90007000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    report = guardrails.check_figures(_result("Revenue was $77.7 billion.", [call]))

    assert report["figures_untraced"] == 1
    assert report["all_traced"] is False
    assert report["figures"][0]["traced"] is False
    assert report["figures"][0]["match"] is None


def test_excludes_bare_year():
    report = guardrails.check_figures(_result("In 2026, revenue grew significantly.", []))
    assert report["figures_checked"] == 0


def test_excludes_fy_prefixed_year_but_keeps_adjacent_dollar_figure():
    call = _statement_call({"value": 90007000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    report = guardrails.check_figures(_result("FY2025 revenue was $90.0 billion.", [call]))

    assert report["figures_checked"] == 1
    assert report["figures"][0]["raw_text"] == "$90.0 billion"
    assert report["figures"][0]["traced"] is True


def test_excludes_quarter_labels():
    report = guardrails.check_figures(
        _result("In Q3, and again in Q1 2025, revenue rose.", [])
    )
    assert report["figures_checked"] == 0


def test_excludes_period_count_phrases():
    text = "Over the past 8 quarters and roughly 12 months, across 3 companies, trends held."
    report = guardrails.check_figures(_result(text, []))
    assert report["figures_checked"] == 0


def test_excludes_hyphenated_week_count_phrase():
    # Confirmed by a live run_agent trace comparing retailers on non-calendar fiscal years:
    # Costco's 52/53-week retail calendar (see NOTES.md) was described with a hyphenated count
    # ("12-week quarters", "16-week Q4"). The count-word exclusion only recognized a whitespace
    # separator and didn't recognize "week" at all, so both "12" and "16" leaked.
    text = "**Costco (fiscal year ends late Aug/early Sept; 12-week quarters plus a 16-week Q4)**"
    report = guardrails.check_figures(_result(text, []))
    assert report["figures_checked"] == 0


def test_excludes_sec_form_label_adjacent_number():
    report = guardrails.check_figures(
        _result("Ford's most recent 10-Q flagged unusual capex.", [])
    )
    assert report["figures_checked"] == 0


def test_excludes_sec_form_label_written_with_non_breaking_hyphen():
    # Confirmed by a live run_agent trace: Claude renders "10-Q"/"8-K" with a non-breaking
    # hyphen (U+2011), not ASCII "-" -- an ASCII-only exclusion pattern let the leading "10"
    # leak through as a bare, untraced-looking figure even though it's just the form label.
    report = guardrails.check_figures(
        _result("Nothing unusual stood out in Ford's most recent 10‑Q.", [])
    )
    assert report["figures_checked"] == 0


def test_excludes_sec_form_label_plural():
    # Confirmed by the Phase 6 eval harness auditing real run_agent traces: the plural form
    # ("10-Qs") escaped the exclusion regex's trailing \b -- there's no word/non-word boundary
    # between "Q" and "s", so the whole match failed outright and "10" leaked through as an
    # untraced-looking bare integer. Real phrasing from a saved AAPL trace.
    report = guardrails.check_figures(
        _result(
            "RevenueFromContractWithCustomerExcludingAssessedTax values in the 10-Qs filed on "
            "the dates shown.",
            [],
        )
    )
    assert report["figures_checked"] == 0


def test_bare_suffix_without_dollar_not_treated_as_scaled_figure():
    report = guardrails.check_figures(_result("3M was mentioned as a peer.", []))
    assert report["figures_checked"] == 0


def test_excludes_negative_number_written_with_unicode_minus_sign():
    # Confirmed by a live run_agent trace: the model writes negative numbers with a true minus
    # sign (U+2212), not ASCII "-". A candidate must still be extracted, sign and all.
    call = _ratios_call(-0.0083)
    report = guardrails.check_figures(_result("Growth was −0.83%.", [call]))

    fig = report["figures"][0]
    assert fig["normalized_value"] == -0.0083
    assert fig["traced"] is True


def test_negative_number_written_with_non_breaking_hyphen_traces():
    # Confirmed by the Phase 6 eval harness auditing 84 untraced figures across 21 real runs:
    # Claude sometimes writes its negative sign as a non-breaking hyphen (U+2011), not just the
    # true minus sign (U+2212) the Phase 4 pass found -- 37 of those 84 (44%, the single largest
    # cause) turned out to be exactly this: genuinely grounded figures the checker had misread as
    # positive. Real phrasing from a saved Ford trace.
    call = _tool_call(
        "detect_anomalies",
        {
            "ticker": "F",
            "metric": "cash",
            "mode": "level",
            "periods": [
                {
                    "period_end": "2025-12-31",
                    "value": 17649000000.0,
                    "trailing_mean": 25000000000.0,
                    "trailing_std": 3200000000.0,
                    "deviation_std": -2.26,
                    "is_anomaly": True,
                }
            ],
        },
    )
    text = (
        "**1. Cash — flagged anomaly.** $17,649M, which is **‑2.26 trailing standard "
        "deviations** below its trailing baseline."
    )
    report = guardrails.check_figures(_result(text, [call]))

    matches = [f for f in report["figures"] if abs(f["normalized_value"] - (-2.26)) < 1e-9]
    assert len(matches) == 1
    assert matches[0]["traced"] is True


def test_negative_number_written_with_en_dash_traces():
    # Confirmed by a live run_agent trace (Streamlit, "What was Ford's gross margin last
    # quarter?"): Claude sometimes writes its negative sign as an en dash (U+2013), a third
    # dash-like character distinct from both the true minus sign (U+2212, Phase 4) and the
    # non-breaking hyphen (U+2011, Phase 6) previously found. Real phrasing from that run,
    # describing Ford's derived Q4 2025 operating loss.
    call = _tool_call(
        "get_financial_statement",
        {
            "ticker": "F",
            "period_length": "quarterly",
            "periods_returned": 1,
            "concepts_unavailable": [],
            "notes": [],
            "periods": [
                {
                    "period_end": "2025-12-31",
                    "period_start": "2025-10-01",
                    "operating_income": {
                        "value": -11557000000.0,
                        "tag": "derived",
                        "filed": "2026-02-11",
                        "is_derived": True,
                    },
                }
            ],
        },
    )
    text = "Q4 2025 showed a large loss, with operating income of –$11.557B (–25.18% margin)."
    report = guardrails.check_figures(_result(text, [call]))

    dollar_matches = [f for f in report["figures"] if abs(f["normalized_value"] - (-11.557e9)) < 1]
    assert len(dollar_matches) == 1
    assert dollar_matches[0]["traced"] is True


def test_en_dash_range_does_not_flip_sign_of_second_number():
    # Confirmed by re-scoring the Phase 6 eval harness's 21 saved traces after the U+2013 fix
    # above: making _SIGN_CHARS a full superset of _DASH_CHARS was too broad, because the same
    # en dash also joins a *range* ("$42.4B to $99.9B"), not just a negative sign, and the second
    # number in a range sits directly against the tail of the first with no space -- unlike a
    # real negative sign, which is preceded by whitespace or punctuation. Real phrasing from a
    # saved costco_revenue_forecast trace; real forecast_metric confidence-interval bounds.
    call = _tool_call(
        "forecast_metric",
        {
            "ticker": "COST",
            "column": "revenue",
            "projections": [
                {
                    "period_end": "2026-05-31",
                    "value": 71138010905.56412,
                    "lower": 42397269497.72855,
                    "upper": 99878752313.3997,
                }
            ],
        },
    )
    text = "and the resulting 95% band is enormous ($42.4B–$99.9B)."
    report = guardrails.check_figures(_result(text, [call]))

    upper_matches = [f for f in report["figures"] if abs(f["normalized_value"] - 99.9e9) < 0.1e9]
    assert len(upper_matches) == 1
    assert upper_matches[0]["normalized_value"] > 0
    assert upper_matches[0]["traced"] is True


def test_en_dash_range_of_ratios_does_not_flip_sign_of_second_number():
    # Same regression as above, confirmed independently on a saved ford_10q_anomalies trace: a
    # dash-joined range of ratios ("0.842 to 0.846"), not a negative sign. Real phrasing and real
    # debt_to_assets values from that trace.
    call = _tool_call(
        "get_ratios",
        {
            "ticker": "F",
            "period_length": "quarterly",
            "notes": [],
            "ratios": {
                "debt_to_assets": [
                    {"period_end": "2024-06-30", "value": 0.8423817546802803, "inputs": {}, "provenance": {}},
                    {"period_end": "2024-09-30", "value": 0.8455340066260926, "inputs": {}, "provenance": {}},
                ]
            },
        },
    )
    text = "Debt-to-assets **0.867** (vs. 0.842–0.846 in every quarter from Q2 2024 through Q3 2025)."
    report = guardrails.check_figures(_result(text, [call]))

    second_matches = [f for f in report["figures"] if abs(f["normalized_value"] - 0.846) < 1e-9]
    assert len(second_matches) == 1
    assert second_matches[0]["normalized_value"] > 0
    assert second_matches[0]["traced"] is True


def test_excludes_iso_date_written_with_non_breaking_hyphen():
    # Confirmed by a live run_agent trace: dates are rendered with a non-breaking hyphen
    # (U+2011), not ASCII "-" -- the exclusion must still catch the whole date.
    report = guardrails.check_figures(
        _result("The quarter ended 2026‑01‑25 was strong.", [])
    )
    assert report["figures_checked"] == 0


def test_excludes_slash_date_with_two_digit_year():
    # Confirmed by a live run_agent trace ("What was Ford's gross margin last quarter?"): a
    # markdown table restated each quarter's end date next to its label in compact M/D/YY form.
    # Before this exclusion existed, each date fragmented into three separate untraced-looking
    # bare integers (month, day, year) -- 9 across the 3 rows of this exact table.
    text = (
        "| Q2 2025 (6/30/25) | $50,184M |\n"
        "| Q3 2025 (9/30/25) | $50,534M |\n"
        "| Q4 2025 (12/31/25) | $45,890M |"
    )
    call = _tool_call(
        "get_financial_statement",
        {
            "ticker": "F",
            "period_length": "quarterly",
            "periods_returned": 3,
            "concepts_unavailable": [],
            "notes": [],
            "periods": [
                {"period_end": "2025-06-30", "revenue": {"value": 50184000000.0, "tag": "Revenues", "filed": "2025-08-01"}},
                {"period_end": "2025-09-30", "revenue": {"value": 50534000000.0, "tag": "Revenues", "filed": "2025-11-01"}},
                {"period_end": "2025-12-31", "revenue": {"value": 45890000000.0, "tag": "derived", "filed": "2026-02-11"}},
            ],
        },
    )
    report = guardrails.check_figures(_result(text, [call]))

    # Only the three dollar figures should be extracted -- no bare "6"/"30"/"25"/"9"/"12"/"31".
    assert report["figures_checked"] == 3
    assert report["all_traced"] is True


def test_excludes_slash_date_with_four_digit_year():
    # The four-digit-year form ("6/30/2024") documented as an untracked gap by the Phase 6 eval
    # harness's audit (4 of the 44 remaining untraced figures) before this pattern existed.
    report = guardrails.check_figures(
        _result("The quarter ended 6/30/2024 was the strongest of the year.", [])
    )
    assert report["figures_checked"] == 0


def test_excludes_abbreviated_month_day_without_year():
    # Confirmed by a live run_agent trace: fiscal quarter-end dates were stated with an
    # abbreviated month and no year at all (the year was established once, earlier in the same
    # sentence, via "fiscal year ends Jan 31") -- the natural-language-date exclusion required a
    # trailing year to match at all, so every bare day-of-month ("31", "30", "31", "31") leaked.
    text = "**Walmart (fiscal year ends Jan 31; quarters end Apr 30 / Jul 31 / Oct 31)**"
    report = guardrails.check_figures(_result(text, []))
    assert report["figures_checked"] == 0


def test_excludes_dash_joined_abbreviated_month_day():
    # Confirmed by a live run_agent trace: the same quarter-end dates were later restated
    # dash-joined ("Jul-31", "Oct-31", "Apr-30") rather than space-joined. Besides missing the
    # date exclusion (which only recognized whitespace between month and day), the dash was
    # separately misread by _NUMBER_RE's sign character as a minus sign, extracting "-31"/"-30"
    # as if they were negative financial figures. The real percent figures on the same line must
    # still trace correctly once the date fragments are excluded.
    call = _tool_call(
        "get_ratios",
        {
            "ticker": "WMT",
            "period_length": "quarterly",
            "notes": [],
            "ratios": {
                "net_margin": [
                    {"period_end": "2024-07-31", "value": 0.0268, "inputs": {}, "provenance": {}},
                    {"period_end": "2025-07-31", "value": 0.0400, "inputs": {}, "provenance": {}},
                    {"period_end": "2024-10-31", "value": 0.0272, "inputs": {}, "provenance": {}},
                    {"period_end": "2025-10-31", "value": 0.0346, "inputs": {}, "provenance": {}},
                    {"period_end": "2025-04-30", "value": 0.0274, "inputs": {}, "provenance": {}},
                    {"period_end": "2026-04-30", "value": 0.0303, "inputs": {}, "provenance": {}},
                ]
            },
        },
    )
    text = (
        "Comparing like quarters: Jul-31 went from 2.68% to 4.00%, Oct-31 from 2.72% to 3.46%, "
        "and Apr-30 from 2.74% to 3.03%."
    )
    report = guardrails.check_figures(_result(text, [call]))

    assert report["figures_checked"] == 6
    assert report["all_traced"] is True
    assert all(fig["raw_text"] not in ("-31", "-30") for fig in report["figures"])


def test_excludes_fiscal_quarter_label_with_apostrophe_year():
    # Confirmed by a live run_agent trace: "FQ4'24" (fiscal-quarter-apostrophe-year) is not
    # caught by a plain "Q1"-"Q4" pattern -- the leading "F" breaks the word boundary and the
    # apostrophe-glued year needs its own allowance.
    report = guardrails.check_figures(_result("This was FQ4'24 for Apple.", []))
    assert report["figures_checked"] == 0


def test_digit_embedded_in_identifier_is_not_extracted():
    # Confirmed by a live run_agent trace: quoting a JSON field name like
    # `q4_subtraction_value` in prose must not leak its embedded "4" as a bare figure.
    report = guardrails.check_figures(
        _result("The tool returned no `q4_subtraction_value` field for this period.", [])
    )
    assert report["figures_checked"] == 0


def test_excludes_markdown_numbered_list_ordinal():
    # Confirmed by a live run_agent trace: "**4. Liquidity keeps eroding.**" is a heading
    # number, not a financial figure -- but a real figure later in the same answer still counts.
    call = _statement_call({"value": 90007000000.0, "tag": "Revenues", "filed": "2025-08-01"})
    text = "**4. Liquidity keeps eroding.** Revenue was $90.0 billion."
    report = guardrails.check_figures(_result(text, [call]))

    assert report["figures_checked"] == 1
    assert report["figures"][0]["raw_text"] == "$90.0 billion"


def test_excludes_natural_language_date():
    # Confirmed by a live run_agent trace: "fiscal year ended June 30, 2025" leaked "30" as a
    # bare figure when only ISO-format dates were excluded.
    report = guardrails.check_figures(
        _result("Fiscal year ended June 30, 2025.", [])
    )
    assert report["figures_checked"] == 0


def test_no_figures_in_answer_returns_empty_trivial_report():
    report = guardrails.check_figures(
        _result("Ford does not report gross profit for this period.", [])
    )
    assert report["figures_checked"] == 0
    assert report["figures"] == []
    assert report["all_traced"] is True


def test_walks_q4_subtraction_value_as_independent_candidate():
    call = _statement_call(
        {
            "value": 90007000000.0,
            "tag": "Revenues",
            "filed": "2025-08-01",
            "is_derived": False,
            "q4_subtraction_value": 5000000000.0,
            "q4_diverges_from_subtraction": True,
        }
    )
    report = guardrails.check_figures(
        _result("The FY2025 Q4 revenue subtraction estimate was $5.0 billion.", [call])
    )

    fig = report["figures"][0]
    assert fig["traced"] is True
    assert fig["match"]["json_path"] == "periods[0].revenue.q4_subtraction_value"


def test_skips_null_ratio_values_without_crash_or_spurious_match():
    report = guardrails.check_figures(
        _result("No margin figure is stated here.", [_ratios_call(None)])
    )
    assert report["figures_checked"] == 0
    assert report["figures"] == []
