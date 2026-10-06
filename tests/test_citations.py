"""
Tests for src/agent/citations.py, fully offline. The tool results are the real
get_csv_statement/get_csv_ratios output for a spreadsheet-sourced statement taken through the
backend's own storage round trip (normalize -> df.to_json/attrs -> statement_from_records), and
statement_raw is the real csv_statements.raw_columns shape (sheet_ingest.raw_csv_to_json, then
a JSON round trip), so read_value is checked against what's actually stored.
"""

import json

import pytest

from src.agent import citations, csv_session, guardrails, tools
from src.analysis import csv_statement
from src.data.sheet_ingest import raw_csv_to_json, rows_to_raw_csv

# Typed in thousands. Header in sheet row 3, so data rows are sheet rows 4-8; revenue is column B.
_ROWS = [
    ["Quarter Ending", "Total Revenue", "Gross Profit", "Net Income", "Total Assets"],
    ["2024-03-31", "1250", "500", "100", "5000"],
    ["2024-06-30", "1300", "546", "110", "5100"],
    ["2024-09-30", "1300", "507", "90", "5200"],
    ["2024-12-31", "1400", "602", "110", "5300"],
    ["2025-03-31", "1500", "660", "120", "5400"],
]
_SOURCE = {
    "platform": "google_sheets", "sheet_name": "P&L", "range": "A3:E8",
    "file_name": "Citations Test", "modified_at": None,
}
_MAPPING = {
    "Quarter Ending": "period_end", "Total Revenue": "revenue", "Gross Profit": "gross_profit",
    "Net Income": "net_income", "Total Assets": "total_assets",
}


@pytest.fixture(autouse=True)
def _reset_csv_session():
    csv_session.set_active_csv(None)
    yield
    csv_session.set_active_csv(None)


def _stored_statement():
    """(statement as /v1/ask reloads it, raw_columns as csv_statements stores it)."""
    raw, error = rows_to_raw_csv(_ROWS, "Citations Test — P&L", source=_SOURCE)
    assert error is None, error
    df, errors, _ = csv_statement.normalize(raw, _MAPPING, entity_name="Cite Co", scale="thousands")
    assert errors == [], errors
    records = json.loads(df.to_json(orient="records", date_format="iso"))
    attrs = json.loads(json.dumps(df.attrs))
    statement = csv_statement.statement_from_records(records, attrs)
    statement_raw = json.loads(json.dumps(raw_csv_to_json(raw)))
    return statement, statement_raw


def _call(tool_name, tool_result, iteration=1):
    return {
        "iteration": iteration, "tool_name": tool_name, "tool_input": {},
        "tool_result": tool_result, "is_error": False,
    }


def _csv_tool_calls():
    return [
        _call("get_csv_statement", tools.get_csv_statement()),
        _call(
            "get_csv_ratios",
            tools.get_csv_ratios(ratio_names=["gross_margin", "revenue_growth_qoq", "roa"]),
        ),
    ]


def _cite(answer, tool_calls, prior_tool_calls=None, statement_raw=None):
    result = {"final_answer": answer, "tool_calls": tool_calls}
    figure_check = guardrails.check_figures(result, prior_tool_calls)
    return figure_check, citations.build_citations(
        figure_check, tool_calls, prior_tool_calls, statement_raw
    )


@pytest.fixture
def stored():
    statement, statement_raw = _stored_statement()
    csv_session.set_active_csv(statement)
    return statement_raw


def test_stored_raw_shape_maps_source_row_and_column_to_the_cell(stored):
    """The raw_columns shape the backend stores: header excluded, data_rows 0-based, so
    source_row indexes data_rows directly and sheet row = range start + 1 + source_row."""
    assert stored["columns"] == _ROWS[0]
    assert stored["data_rows"][0] == _ROWS[1]
    assert stored["source"]["range"] == "A3:E8"
    entry = {"source_row": 0, "source_column": "Total Revenue", "source_cell": "'P&L'!B4"}
    assert citations._read_value(entry, stored) == "1250"
    entry = {"source_row": 2, "source_column": "Net Income", "source_cell": "'P&L'!D6"}
    assert citations._read_value(entry, stored) == "90"


def test_direct_value_cites_one_cell_with_its_read_value_and_scale(stored):
    _, cites = _cite("Revenue was 1.25 million in the first quarter.", _csv_tool_calls(), statement_raw=stored)

    [cite] = cites
    assert cite["status"] == "traced"
    assert cite["raw_text"] == "1.25 million"
    [match] = cite["matches"]  # the same cell via the statement and the ratio inputs counts once
    assert match["kind"] == "cell"
    assert match["value"] == 1250000.0
    assert match["turn_id"] is None
    assert match["source"] == {
        "cell": "'P&L'!B4",
        "concept": "revenue",
        "column": "Total Revenue",
        "period_end": "2024-03-31",
        "read_value": "1250",  # what was read, never recomputed from 1250000
        "sheet_scale": "thousands",
    }


def test_derived_margin_cites_its_formula_and_both_input_cells(stored):
    _, cites = _cite("Gross margin was 40.0% in Q1 2024.", _csv_tool_calls(), statement_raw=stored)

    [cite] = cites
    assert cite["status"] == "traced"
    [match] = cite["matches"]
    assert match["kind"] == "derived"
    assert "source" not in match  # never collapsed into a single cell
    computation = match["computation"]
    assert computation["name"] == "gross_margin"
    assert computation["formula"] == "gross_profit / revenue"
    cells = {i["role"]: (i["value"], i["source"]["cell"], i["source"]["read_value"]) for i in computation["inputs"]}
    assert cells == {
        "gross_profit": (500000.0, "'P&L'!C4", "500"),
        "revenue": (1250000.0, "'P&L'!B4", "1250"),
    }


def test_growth_rate_cites_the_prior_period_cell_it_compares_against(stored):
    _, cites = _cite("Revenue grew 4.0% quarter over quarter.", _csv_tool_calls(), statement_raw=stored)

    [match] = cites[0]["matches"]
    computation = match["computation"]
    assert computation["name"] == "revenue_growth_qoq"
    assert computation["period_end"] == "2024-06-30"
    by_role = {i["role"]: i for i in computation["inputs"]}
    assert by_role["revenue"]["source"]["cell"] == "'P&L'!B5"
    prior = by_role["revenue_prior"]
    assert prior["value"] == 1250000.0
    assert prior["source"]["cell"] == "'P&L'!B4"
    assert prior["source"]["period_end"] == "2024-03-31"


def test_ttm_sum_cites_all_four_quarters(stored):
    _, cites = _cite(
        "Net income over the trailing twelve months was 410,000.", _csv_tool_calls(), statement_raw=stored
    )

    [match] = cites[0]["matches"]
    computation = match["computation"]
    assert computation["name"] == "net_income_ttm"
    assert computation["period_end"] == "2024-12-31"
    assert [i["source"]["cell"] for i in computation["inputs"]] == [
        "'P&L'!D4", "'P&L'!D5", "'P&L'!D6", "'P&L'!D7",
    ]
    assert [i["source"]["read_value"] for i in computation["inputs"]] == ["100", "110", "90", "110"]


def test_roa_formula_names_the_ttm_numerator(stored):
    _, cites = _cite("ROA was 7.74% at the end of 2024.", _csv_tool_calls(), statement_raw=stored)

    [match] = cites[0]["matches"]
    computation = match["computation"]
    assert computation["formula"] == "net_income_ttm / total_assets"
    ttm_input = next(i for i in computation["inputs"] if i["role"] == "net_income_ttm")
    assert ttm_input["source"] is None
    assert len(ttm_input["computation"]["inputs"]) == 4


def test_value_in_two_cells_is_ambiguous_and_lists_both(stored):
    _, cites = _cite("Revenue was 1.3 million.", _csv_tool_calls(), statement_raw=stored)

    [cite] = cites
    assert cite["status"] == "ambiguous"
    assert sorted(m["source"]["cell"] for m in cite["matches"]) == ["'P&L'!B5", "'P&L'!B6"]


def test_read_value_is_null_when_the_stored_rows_are_another_statements(stored):
    other = json.loads(json.dumps(stored))
    other["source"]["range"] = "A10:E15"  # same shape, different place: not the cited cell
    _, cites = _cite("Revenue was 1.25 million.", _csv_tool_calls(), statement_raw=other)

    assert cites[0]["matches"][0]["source"]["read_value"] is None


def test_read_value_is_null_without_stored_rows(stored):
    _, cites = _cite("Revenue was 1.25 million.", _csv_tool_calls())
    assert cites[0]["matches"][0]["source"]["read_value"] is None


def test_weak_and_untraced_figures_have_no_matches(stored):
    _, cites = _cite("Over 5 rows, revenue was 9.99 million.", _csv_tool_calls(), statement_raw=stored)

    by_text = {c["raw_text"]: c for c in cites}
    assert by_text["5"]["status"] == "weak"
    assert by_text["5"]["matches"] == []
    assert by_text["9.99 million"]["status"] == "untraced"
    assert by_text["9.99 million"]["matches"] == []


def test_figure_from_a_replayed_earlier_turn_traces_to_that_turn(stored):
    prior = [{**call, "turn_id": "turn-1"} for call in _csv_tool_calls()]
    figure_check, cites = _cite("As before, revenue was 1.25 million.", [], prior, stored)

    assert figure_check["all_traced"] is True
    assert figure_check["figures"][0]["match"]["turn_id"] == "turn-1"
    [match] = cites[0]["matches"]
    assert match["turn_id"] == "turn-1"
    assert match["source"]["cell"] == "'P&L'!B4"


def test_filing_value_cites_ticker_tag_filed_and_period():
    payload = {
        "ticker": "MSFT", "period_length": "annual", "periods_returned": 1,
        "concepts_unavailable": [], "notes": [],
        "periods": [{
            "period_end": "2025-06-30", "period_start": None,
            "revenue": {"value": 281724000000.0, "tag": "Revenues", "filed": "2025-07-30", "is_derived": False},
        }],
    }
    calls = [_call("get_financial_statement", json.dumps(payload))]
    _, cites = _cite("Revenue was $281.7 billion.", calls)

    [match] = cites[0]["matches"]
    assert match["kind"] == "filing"
    assert match["source"] == {
        "ticker": "MSFT", "concept": "revenue", "tag": "Revenues", "filed": "2025-07-30",
        "period_end": "2025-06-30", "is_derived": False,
    }


def test_value_with_no_cell_or_filing_is_a_tool_match():
    calls = [_call("forecast_metric", json.dumps({"forecast": [{"period": 1, "value": 1300000.0}]}))]
    _, cites = _cite("The projection is 1.3 million.", calls)

    [match] = cites[0]["matches"]
    assert match["kind"] == "tool"
    assert match["source"] == {"tool_name": "forecast_metric", "json_path": "forecast[0].value"}


def test_citations_follow_figure_order_and_offsets(stored):
    answer = "Revenue was 1.25 million and gross margin was 40.0%."
    figure_check, cites = _cite(answer, _csv_tool_calls(), statement_raw=stored)

    assert [c["figure_index"] for c in cites] == [0, 1]
    for cite, figure in zip(cites, figure_check["figures"]):
        assert answer[cite["start"]:cite["end"]] == cite["raw_text"] == figure["raw_text"]



def test_a_quoted_display_percentage_traces_to_the_raw_ratio_as_derived(stored):
    calls = _csv_tool_calls()
    growth = json.loads(calls[1]["tool_result"])["ratios"]["revenue_growth_qoq"]
    row = next(r for r in growth if r["period_end"] == "2024-06-30")
    assert row["display"] == "4.0%"
    _, cites = _cite(f"Revenue grew {row['display']} quarter over quarter.", calls, statement_raw=stored)
    [match] = cites[0]["matches"]
    assert match["kind"] == "derived"
    assert match["computation"]["name"] == "revenue_growth_qoq"


def test_a_negative_and_a_huge_display_percentage_trace():
    payload = {"ratios": {"earnings_growth_qoq": [
        {"period_end": "2025-06-30", "value": -0.04580152671755725, "display": "-4.6%", "inputs": {}, "provenance": {}},
        {"period_end": "2025-09-30", "value": -384.4961832061069, "display": "-38,449.6%", "inputs": {}, "provenance": {}},
    ]}}
    calls = [_call("get_ratios", json.dumps(payload))]
    figure_check, cites = _cite("Earnings fell -4.6%, then -38,449.6%.", calls)
    assert figure_check["all_traced"] is True
    assert [c["matches"][0]["kind"] for c in cites] == ["derived", "derived"]


def test_no_citation_lands_inside_a_sheet_name(stored):
    answer = "In 'P&L'!B4 revenue was 1.25 million; the P&L (000s) tab agrees."
    result = {"final_answer": answer, "tool_calls": _csv_tool_calls()}
    figure_check = guardrails.check_figures(result, None, ["P&L (000s)"])
    cites = citations.build_citations(figure_check, result["tool_calls"], None, stored)
    assert [c["raw_text"] for c in cites] == ["1.25 million"]
