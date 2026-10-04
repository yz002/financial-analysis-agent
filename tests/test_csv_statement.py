"""
Tests for src/analysis/csv_statement.py: mapping validation and CSV -> get_statement()-shaped
normalization. Most tests build a RawCsv directly (mirroring test_ratios.py's minimal
statement-shaped DataFrame style) rather than round-tripping through csv_ingest.parse_csv,
except where the sample fixture CSV itself is the point.

test_normalized_columns_exactly_match_get_statement_schema and
test_ratios_run_against_normalized_frame_without_crashing are this session's concrete
verification of the design doc's "zero changes needed" claim: they use conftest.py's
msft_quarterly fixture, which (like the rest of this test suite) makes a live SEC EDGAR
request on a cold cache and is offline afterward -- see conftest.py/CLAUDE.md.
"""

from pathlib import Path

import pandas as pd
import pytest

from src.analysis import ratios
from src.analysis.csv_statement import (
    ALL_CONCEPTS,
    DURATION_CONCEPTS,
    find_unparsed_cells,
    normalize,
    validate_mapping,
)
from src.data.csv_ingest import RawCsv, parse_csv

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_small_business.csv"

_UPLOADED_AT = pd.Timestamp("2025-06-01 12:00:00").to_pydatetime()

_MAPPING = {
    "Quarter Ending": "period_end",
    "Total Revenue": "revenue",
    "Net Income": "net_income",
    "Total Assets": "total_assets",
    "Total Liabilities": "total_liabilities",
    "Cash on Hand": "cash",
    "Owner's Equity": "stockholders_equity",
    "Internal Notes": "unmapped",
}

_MAPPED_CONCEPTS = {
    "revenue", "net_income", "total_assets", "total_liabilities", "cash", "stockholders_equity",
}
_UNMAPPED_CONCEPTS = [c for c in ALL_CONCEPTS if c not in _MAPPED_CONCEPTS]


def _load_sample_raw() -> RawCsv:
    raw, error = parse_csv(
        FIXTURE_PATH.read_bytes(), "sample_small_business.csv", uploaded_at=_UPLOADED_AT
    )
    assert error is None, error
    return raw


def _normalized_sample():
    raw = _load_sample_raw()
    df, errors, warnings = normalize(raw, _MAPPING, entity_name="Test Bakery LLC")
    assert errors == [], errors
    return df, warnings


# --- valid CSV -> correct normalized shape --------------------------------------------------


def test_valid_csv_normalizes_with_correct_shape_and_values():
    df, _warnings = _normalized_sample()

    assert len(df) == 8
    expected_periods = pd.to_datetime(
        [
            "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
            "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
        ]
    )
    assert list(df["period_end"]) == list(expected_periods)

    assert df["revenue"].iloc[0] == pytest.approx(125000.0)
    assert df["net_income"].iloc[2] == pytest.approx(-1300.0)  # "($1,300.00)" -> negative
    assert df["total_assets"].iloc[-1] == pytest.approx(378000.0)
    assert df["stockholders_equity"].iloc[0] == pytest.approx(130000.0)
    assert df["revenue_tag"].iloc[0] == "Total Revenue"
    assert df["revenue_filed"].iloc[0] == pd.Timestamp(_UPLOADED_AT)

    assert df.attrs["entity_name"] == "Test Bakery LLC"
    assert df.attrs["cik"] is None
    assert "sparse_history" not in df.attrs
    assert df.attrs["periods_available"] == 8
    assert df.attrs["csv_source"]["cadence"] == "quarterly"
    assert df.attrs["csv_source"]["filename"] == "sample_small_business.csv"
    assert df.attrs["csv_provenance"]["revenue"]["2023-03-31"] == {
        "source_row": 0,
        "source_column": "Total Revenue",
    }


def test_unmapped_concepts_are_entirely_nan():
    df, _warnings = _normalized_sample()
    for concept in _UNMAPPED_CONCEPTS:
        assert df[concept].isna().all(), f"{concept} should be all-NaN, wasn't mapped"
        assert df[f"{concept}_tag"].isna().all()
        assert df[f"{concept}_filed"].isna().all()


def test_unmapped_recommended_concepts_produce_warnings():
    _df, warnings = _normalized_sample()
    joined = " ".join(warnings)
    for concept in ["current_assets", "current_liabilities", "operating_cash_flow", "capex"]:
        assert concept in joined


def test_stubbed_edgar_only_columns_are_always_literal_defaults():
    df, _warnings = _normalized_sample()
    for concept in DURATION_CONCEPTS:
        assert (df[f"{concept}_is_derived"] == False).all()  # noqa: E712
        assert df[f"{concept}_derivation_method"].isna().all()
        assert df[f"{concept}_q4_subtraction_value"].isna().all()
        assert (df[f"{concept}_q4_diverges_from_subtraction"] == False).all()  # noqa: E712

    assert (df["total_liabilities_is_derived"] == False).all()  # noqa: E712
    # total_liabilities was mapped and has a real value every period -> "direct_tag" every row.
    assert (df["total_liabilities_derivation_method"] == "direct_tag").all()
    assert df["total_liabilities_alt_value"].isna().all()
    assert df["total_liabilities_alt_method"].isna().all()
    assert (df["total_liabilities_diverges_from_alt"] == False).all()  # noqa: E712


# --- refusal gates ---------------------------------------------------------------------------


def test_missing_date_column_refuses():
    raw = _load_sample_raw()
    mapping = dict(_MAPPING)
    mapping["Quarter Ending"] = "unmapped"
    df, errors, _warnings = normalize(raw, mapping, entity_name="Test Bakery LLC")
    assert df is None
    assert any("date/period" in e.lower() for e in errors)


def test_missing_revenue_refuses():
    raw = _load_sample_raw()
    mapping = dict(_MAPPING)
    mapping["Total Revenue"] = "unmapped"
    df, errors, _warnings = normalize(raw, mapping, entity_name="Test Bakery LLC")
    assert df is None
    assert any("revenue" in e.lower() for e in errors)


def test_ambiguous_mapping_two_columns_to_same_concept_refuses():
    raw = _load_sample_raw()
    mapping = dict(_MAPPING)
    mapping["Internal Notes"] = "revenue"  # now two columns both claim "revenue"
    df, errors, _warnings = normalize(raw, mapping, entity_name="Test Bakery LLC")
    assert df is None
    assert any("revenue" in e.lower() and "more than one column" in e.lower() for e in errors)


def test_ambiguous_mapping_two_period_columns_refuses():
    raw = _load_sample_raw()
    mapping = dict(_MAPPING)
    mapping["Internal Notes"] = "period_end"
    df, errors, _warnings = normalize(raw, mapping, entity_name="Test Bakery LLC")
    assert df is None
    assert any("date/period" in e.lower() for e in errors)


def test_monthly_cadence_refuses():
    df_raw = pd.DataFrame(
        {
            "Date": [
                "2024-01-31", "2024-02-29", "2024-03-31",
                "2024-04-30", "2024-05-31", "2024-06-30",
            ],
            "Revenue": [10000, 11000, 10500, 12000, 11500, 13000],
        }
    )
    raw = RawCsv(df=df_raw, filename="monthly.csv", uploaded_at=_UPLOADED_AT)
    mapping = {"Date": "period_end", "Revenue": "revenue"}
    df, errors, _warnings = normalize(raw, mapping, entity_name="Monthly Co")
    assert df is None
    assert any("spacing" in e.lower() for e in errors)


def test_duplicate_period_end_refuses():
    df_raw = pd.DataFrame(
        {
            "Date": ["2024-01-31", "2024-01-31", "2024-04-30"],
            "Revenue": ["10000", "10500", "11000"],
        }
    )
    raw = RawCsv(df=df_raw, filename="dup.csv", uploaded_at=_UPLOADED_AT)
    mapping = {"Date": "period_end", "Revenue": "revenue"}
    df, errors, _warnings = normalize(raw, mapping, entity_name="Dup Co")
    assert df is None
    assert any("more than one row" in e.lower() for e in errors)


# --- soft, row-level and single-period behavior -----------------------------------------------


def test_single_period_csv_is_valid_not_refused():
    df_raw = pd.DataFrame({"Date": ["2024-06-30"], "Revenue": ["50000"]})
    raw = RawCsv(df=df_raw, filename="single.csv", uploaded_at=_UPLOADED_AT)
    mapping = {"Date": "period_end", "Revenue": "revenue"}
    df, errors, _warnings = normalize(raw, mapping, entity_name="Single Period Co")
    assert errors == []
    assert df is not None
    assert len(df) == 1
    assert df.attrs["csv_source"]["cadence"] is None  # can't classify cadence from 1 period


def test_bad_date_row_is_dropped_with_named_reason_and_good_rows_survive():
    """A CSV where one row's date cell is unparseable ("not-a-date") alongside three
    otherwise-valid rows: (a) the bad row is dropped individually, not a whole-file refusal,
    (b) the three surviving rows normalize with their correct values, in the correct order --
    not just a right row *count* by coincidence, and (c) the drop is reported with a specific,
    named reason: which row, and the actual unparseable value -- not a generic warning."""
    df_raw = pd.DataFrame(
        {
            "Date": ["2024-01-31", "not-a-date", "2024-04-30", "2024-07-31"],
            "Revenue": ["10000", "9999", "11000", "12000"],
        }
    )
    raw = RawCsv(df=df_raw, filename="baddate.csv", uploaded_at=_UPLOADED_AT)
    mapping = {"Date": "period_end", "Revenue": "revenue"}
    df, errors, warnings = normalize(raw, mapping, entity_name="Bad Date Co")

    assert errors == []
    assert df is not None
    assert len(df) == 3  # the "not-a-date" row is dropped, not the whole file

    assert list(df["period_end"]) == list(
        pd.to_datetime(["2024-01-31", "2024-04-30", "2024-07-31"])
    )
    # the bad row's own revenue value (9999) never appears anywhere in the surviving data
    assert list(df["revenue"]) == [10000.0, 11000.0, 12000.0]

    # warnings also carries the (expected, separate) unmapped-recommended-concept notes since
    # this test only maps Date/Revenue -- isolate the row-drop warning specifically.
    drop_warnings = [w for w in warnings if "row 1" in w.lower()]
    assert len(drop_warnings) == 1
    reason = drop_warnings[0]
    assert "row 1" in reason.lower()  # the dropped row's own index, not just "some row"
    assert "not-a-date" in reason  # the actual unparseable value, named verbatim
    assert "date" in reason.lower()  # explains *why*, not a bare "row 1 was dropped"


def test_bad_date_row_amid_realistic_quarterly_history_still_normalizes():
    """The audit found that dropping a bad-date row can widen the gap around it enough to fail
    _detect_cadence's all-gaps check, rejecting the whole file -- test_bad_date_row_is_dropped_
    with_named_reason_and_good_rows_survive above doesn't catch this because its "bad" row isn't
    positioned between two rows that would otherwise be a normal quarter apart (removing it
    doesn't widen anything). Here the bad row genuinely sits mid-series among 7 other real
    quarters, so dropping it leaves a real ~183-day (2-quarter) gap between 2023-06-30 and
    2023-12-31 -- this must still normalize as quarterly, not refuse the whole file."""
    df_raw = pd.DataFrame(
        {
            "Date": [
                "2023-03-31", "2023-06-30", "not-a-date", "2023-12-31",
                "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
            ],
            "Revenue": [
                "125000", "138500", "999999", "160000",
                "148000", "155000", "150000", "172000",
            ],
        }
    )
    raw = RawCsv(df=df_raw, filename="realistic.csv", uploaded_at=_UPLOADED_AT)
    mapping = {"Date": "period_end", "Revenue": "revenue"}
    df, errors, warnings = normalize(raw, mapping, entity_name="Realistic Co")

    assert errors == []
    assert df is not None
    assert len(df) == 7  # only the "not-a-date" row is dropped
    assert df.attrs["csv_source"]["cadence"] == "quarterly"

    drop_warnings = [w for w in warnings if "row 2" in w.lower()]
    assert len(drop_warnings) == 1
    assert "not-a-date" in drop_warnings[0]


def test_two_consecutive_bad_dates_still_refuses_cadence():
    """The one-missing-period tolerance added above has a real limit: two consecutive
    unparseable dates widen the surviving gap to ~3 quarters, which must still be refused --
    confirms the fix doesn't just accept any gap, only a single missing period's worth."""
    df_raw = pd.DataFrame(
        {
            "Date": [
                "2023-03-31", "2023-06-30", "not-a-date", "also-bad",
                "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
            ],
            "Revenue": ["125000", "138500", "0", "0", "148000", "155000", "150000", "172000"],
        }
    )
    raw = RawCsv(df=df_raw, filename="tworows.csv", uploaded_at=_UPLOADED_AT)
    mapping = {"Date": "period_end", "Revenue": "revenue"}
    df, errors, _warnings = normalize(raw, mapping, entity_name="Two Bad Co")

    assert df is None
    assert any("spacing" in e.lower() for e in errors)


def test_unparseable_numeric_cell_becomes_nan_not_zero():
    """A revenue cell that's still not a number after $/comma/parens cleanup (blank, "N/A",
    whitespace-only) must become NaN for that period -- never silently coerced to 0, which
    would look like a real reported zero rather than "no value given". All four rows have
    valid dates, so this exercises the numeric-cleanup path specifically, independent of the
    date-parsing path covered above."""
    df_raw = pd.DataFrame(
        {
            "Date": ["2024-01-31", "2024-04-30", "2024-07-31", "2024-10-31"],
            "Revenue": ["$10,000.00", "N/A", "", "   "],
        }
    )
    raw = RawCsv(df=df_raw, filename="badnumeric.csv", uploaded_at=_UPLOADED_AT)
    mapping = {"Date": "period_end", "Revenue": "revenue"}
    df, errors, warnings = normalize(raw, mapping, entity_name="Bad Numeric Co")

    assert errors == []
    assert df is not None
    assert len(df) == 4  # all 4 rows have valid dates; only the numeric cells are bad

    assert df["revenue"].iloc[0] == pytest.approx(10000.0)
    assert df["revenue_tag"].iloc[0] == "Revenue"

    for i, raw_value in enumerate(["N/A", "", "   "], start=1):
        value = df["revenue"].iloc[i]
        assert pd.isna(value), f"row {i} ({raw_value!r}) should be NaN, got {value!r}, not 0"
        assert pd.isna(df["revenue_tag"].iloc[i])  # no real value -> no tag either, per convention


def test_exponent_form_numbers_parse_exactly():
    """The Chrome extension sends a numeric cell's underlying value as JavaScript's String(v)
    (Phase D session 3b contract amendment), which switches to exponent form at the extremes:
    String(1e21) is "1e+21", String(1e-7) is "1e-7". The numeric cleaner must read those
    exact strings as the same floats, never as None."""
    df_raw = pd.DataFrame(
        {
            "Date": ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"],
            "Revenue": ["1e+21", "1e-7", "-1e-7", "1.5e+300"],
        }
    )
    raw = RawCsv(df=df_raw, filename="exp.csv", uploaded_at=_UPLOADED_AT)
    df, errors, _ = normalize(raw, {"Date": "period_end", "Revenue": "revenue"}, entity_name="Exp Co")
    assert errors == []
    assert df["revenue"].tolist() == [1e21, 1e-7, -1e-7, 1.5e300]


def test_spreadsheet_source_adds_sheet_qualified_source_cell():
    """With a spreadsheet source (sheet_ingest's `source`, range C3:D5 here), each provenance
    entry also names the exact cell: data row i sits at sheet row 3 + 1 + i, and the column
    letter is offset from the range's first column (Revenue is the 2nd column -> D)."""
    df_raw = pd.DataFrame({"Date": ["2024-03-31", "2024-06-30"], "Revenue": ["100", "200"]})
    source = {
        "platform": "google_sheets", "sheet_name": "P&L", "range": "C3:D5",
        "file_name": "Book", "modified_at": None,
    }
    raw = RawCsv(df=df_raw, filename="Book — P&L", uploaded_at=_UPLOADED_AT, source=source)
    df, errors, _ = normalize(raw, {"Date": "period_end", "Revenue": "revenue"}, entity_name="Co")
    assert errors == []
    prov = df.attrs["csv_provenance"]["revenue"]
    assert prov["2024-03-31"] == {"source_row": 0, "source_column": "Revenue", "source_cell": "'P&L'!D4"}
    assert prov["2024-06-30"]["source_cell"] == "'P&L'!D5"


_SHEET_SOURCE = {
    "platform": "google_sheets", "sheet_name": "P&L", "range": "A3:C9",
    "file_name": "FA Spike Test", "modified_at": None,
}


def _sheet_raw(revenue, net_income=None, dates=None):
    dates = dates or ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30"]
    net_income = net_income or ["1"] * len(dates)
    df_raw = pd.DataFrame({"Date": dates, "Revenue": revenue, "Net Income": net_income})
    return RawCsv(df=df_raw, filename="FA Spike Test — P&L", uploaded_at=_UPLOADED_AT, source=_SHEET_SOURCE)


_SHEET_MAPPING = {"Date": "period_end", "Revenue": "revenue", "Net Income": "net_income"}


def test_unparsed_cells_are_listed_by_cell_address_and_blanks_are_not():
    raw = _sheet_raw(["61.5%", "#DIV/0!", "TRUE", "€1,250", "1 250 000", ""])
    cells = find_unparsed_cells(raw, _SHEET_MAPPING)
    assert [(c["cell"], c["value"]) for c in cells] == [
        ("'P&L'!B4", "61.5%"),
        ("'P&L'!B5", "#DIV/0!"),
        ("'P&L'!B6", "TRUE"),
        ("'P&L'!B7", "€1,250"),
        ("'P&L'!B8", "1 250 000"),
    ]  # the blank B9 is "not reported", not a parse failure
    assert cells[0] == {
        "cell": "'P&L'!B4", "source_row": 0, "column": "Revenue", "role": "revenue",
        "period_end": "2024-03-31", "value": "61.5%",
    }

    df, errors, warnings = normalize(raw, _SHEET_MAPPING, entity_name="Spike Co")
    assert errors == []
    assert df["revenue"].isna().all()
    assert any("revenue ('Revenue') is blank for 1 of 6" in w for w in warnings)


def test_unparsed_cells_skip_rows_dropped_for_a_bad_date_and_need_a_valid_mapping():
    raw = _sheet_raw(
        ["100", "oops", "300", "400", "500", "600"],
        dates=["2024-03-31", "not-a-date", "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30"],
    )
    assert find_unparsed_cells(raw, _SHEET_MAPPING) == []  # "oops" sits in a dropped row
    assert find_unparsed_cells(raw, {"Date": "period_end"}) == []  # no revenue: invalid mapping


def test_unparsed_cell_value_is_capped_at_50_characters():
    raw = _sheet_raw(["x" * 80, "1", "2", "3", "4", "5"])
    assert find_unparsed_cells(raw, _SHEET_MAPPING)[0]["value"] == "x" * 50


def test_validate_mapping_refuses_unknown_columns_and_roles_instead_of_crashing():
    raw = _sheet_raw(["1", "2", "3", "4", "5", "6"])
    errors = validate_mapping(raw, {"Nope": "period_end", "Revenue": "revenue", "Date": "reveneu"})
    joined = " ".join(errors)
    assert "'Nope'" in joined and "aren't in this data" in joined
    assert "'reveneu'" in joined
    # normalize() refuses in-band rather than raising KeyError on the unknown column.
    df, errors, _ = normalize(raw, {"Nope": "period_end", "Revenue": "revenue"}, entity_name="Co")
    assert df is None and errors


def test_scale_converts_to_ones_exactly_and_is_recorded():
    raw = _sheet_raw(["1250", "0.1", "(45)", "1e+3", "2.5", "3"])
    df, errors, _ = normalize(raw, _SHEET_MAPPING, entity_name="Co", scale="thousands", currency="USD")
    assert errors == []
    assert df["revenue"].tolist() == [1250000.0, 100.0, -45000.0, 1000000.0, 2500.0, 3000.0]
    assert df.attrs["csv_source"]["scale"] == "thousands"
    assert df.attrs["csv_source"]["currency"] == "USD"

    df, _, _ = normalize(raw, _SHEET_MAPPING, entity_name="Co", scale="billions")
    assert df["revenue"].iloc[0] == 1.25e12
    assert df.attrs["csv_source"]["currency"] is None


def test_default_scale_is_ones_for_callers_that_predate_it():
    raw = _sheet_raw(["1250", "1", "2", "3", "4", "5"])
    df, errors, _ = normalize(raw, _SHEET_MAPPING, entity_name="Co")
    assert errors == []
    assert df["revenue"].iloc[0] == 1250.0
    assert df.attrs["csv_source"]["scale"] == "ones"


def test_unknown_scale_is_refused():
    raw = _sheet_raw(["1", "2", "3", "4", "5", "6"])
    df, errors, _ = normalize(raw, _SHEET_MAPPING, entity_name="Co", scale="hundreds")
    assert df is None
    assert "hundreds" in errors[0]


def test_dropped_date_row_names_its_cell_when_the_data_came_from_a_sheet():
    raw = _sheet_raw(
        ["1", "2", "3", "4", "5", "6"],
        dates=["2024-03-31", "2024-06-30", "Q3?", "2024-12-31", "2025-03-31", "2025-06-30"],
    )
    _, errors, warnings = normalize(raw, _SHEET_MAPPING, entity_name="Co")
    assert errors == []
    drop = [w for w in warnings if "dropped" in w]
    assert drop == ["Row 2 ('P&L'!A6) was dropped: 'Date' value 'Q3?' could not be parsed as a date."]


def test_no_source_means_no_source_cell():
    df, _ = _normalized_sample()
    for by_period in df.attrs["csv_provenance"].values():
        for entry in by_period.values():
            assert "source_cell" not in entry


# --- verification: exact schema match against get_statement(), and a live ratios.py smoke run -


def test_normalized_columns_exactly_match_get_statement_schema(msft_quarterly):
    df, _warnings = _normalized_sample()
    assert list(df.columns) == list(msft_quarterly.columns)


def test_ratios_run_against_normalized_frame_without_crashing():
    df, _warnings = _normalized_sample()

    gm = ratios.gross_margin(df)  # gross_profit unmapped -> every value None, no crash
    assert gm["value"].apply(lambda v: v is None).all()

    nm = ratios.net_margin(df)
    latest = nm["value"].iloc[-1]
    assert isinstance(latest, float)
    assert -1 < latest < 1

    cr = ratios.current_ratio(df)  # current_assets/current_liabilities unmapped -> None
    assert cr["value"].apply(lambda v: v is None).all()

    dta = ratios.debt_to_assets(df)
    latest = dta["value"].iloc[-1]
    assert isinstance(latest, float)
    assert 0 < latest < 1

    growth = ratios.revenue_growth_qoq(df)
    assert growth["value"].iloc[0] is None  # no prior quarter yet
    assert isinstance(growth["value"].iloc[-1], float)

    roe = ratios.roe(df, period_length="quarterly")
    assert roe["value"].iloc[0] is None  # no full TTM window yet
    assert isinstance(roe["value"].iloc[-1], float)
