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


def test_excludes_iso_date_written_with_non_breaking_hyphen():
    # Confirmed by a live run_agent trace: dates are rendered with a non-breaking hyphen
    # (U+2011), not ASCII "-" -- the exclusion must still catch the whole date.
    report = guardrails.check_figures(
        _result("The quarter ended 2026‑01‑25 was strong.", [])
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
