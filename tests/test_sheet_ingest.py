"""
Tests for src/data/sheet_ingest.py: the Sheet-range -> RawCsv adapter. Fully offline, no
network -- mirrors tests/test_csv_ingest.py's style (plain functions, no classes).
"""

from datetime import datetime

from src.data.sheet_ingest import (
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    MAX_DATA_ROWS,
    MAX_TOTAL_CELLS,
    cell_reference,
    column_letter,
    find_period_serial_number_value,
    parse_a1_range,
    raw_csv_from_json,
    raw_csv_to_json,
    rows_to_raw_csv,
)

_UPLOADED_AT = datetime(2025, 6, 1, 12, 0, 0)


def _sample_rows():
    return [
        ["Quarter Ending", "Total Revenue", "Net Income"],
        ["2024-01-01", "100000", "12000"],
        ["2024-04-01", "110000", "13000"],
    ]


# --- rows_to_raw_csv ------------------------------------------------------------------------


def test_valid_rows_build_raw_csv():
    raw, error = rows_to_raw_csv(_sample_rows(), "sheet.csv", uploaded_at=_UPLOADED_AT)
    assert error is None
    assert raw is not None
    assert list(raw.df.columns) == ["Quarter Ending", "Total Revenue", "Net Income"]
    assert len(raw.df) == 2
    assert raw.filename == "sheet.csv"
    assert raw.uploaded_at == _UPLOADED_AT


def test_empty_rows_refuses():
    raw, error = rows_to_raw_csv([], "empty.csv")
    assert raw is None
    assert "no header row" in error.lower()


def test_empty_header_refuses():
    raw, error = rows_to_raw_csv([[], ["100000"]], "emptyheader.csv")
    assert raw is None
    assert "no header row" in error.lower()


def test_header_only_no_data_rows_refuses():
    raw, error = rows_to_raw_csv([["Quarter Ending", "Total Revenue"]], "headeronly.csv")
    assert raw is None
    assert "no data rows" in error.lower()


def test_duplicate_headers_refuse():
    rows = [
        ["Quarter Ending", "Total Revenue", "Total Revenue"],
        ["2024-01-01", "100000", "100000"],
    ]
    raw, error = rows_to_raw_csv(rows, "dup.csv")
    assert raw is None
    assert "duplicate" in error.lower()


def test_ragged_row_refuses():
    rows = [
        ["Quarter Ending", "Total Revenue", "Net Income"],
        ["2024-01-01", "100000"],  # one cell short of the header
    ]
    raw, error = rows_to_raw_csv(rows, "ragged.csv")
    assert raw is None
    assert "could not be parsed" in error.lower()


# --- raw_csv_to_json / raw_csv_from_json -----------------------------------------------------


def test_raw_csv_json_round_trip():
    raw, error = rows_to_raw_csv(_sample_rows(), "sheet.csv", uploaded_at=_UPLOADED_AT)
    assert error is None

    packed = raw_csv_to_json(raw)
    assert packed == {
        "columns": ["Quarter Ending", "Total Revenue", "Net Income"],
        "data_rows": [
            ["2024-01-01", "100000", "12000"],
            ["2024-04-01", "110000", "13000"],
        ],
    }

    rebuilt = raw_csv_from_json(packed, "sheet.csv", _UPLOADED_AT)
    assert list(rebuilt.df.columns) == list(raw.df.columns)
    assert rebuilt.df.values.tolist() == raw.df.values.tolist()
    assert rebuilt.filename == "sheet.csv"
    assert rebuilt.uploaded_at == _UPLOADED_AT


# --- find_period_serial_number_value ---------------------------------------------------------


def test_serial_number_in_period_column_is_caught():
    rows = [
        ["Quarter Ending", "Total Revenue"],
        ["45292", "100000"],  # a Sheets date-serial number, not a display string
        ["45383", "110000"],
    ]
    raw, error = rows_to_raw_csv(rows, "serial.csv")
    assert error is None
    reason = find_period_serial_number_value(raw, {"Quarter Ending": "period_end"})
    assert reason is not None
    assert "45292" in reason
    assert "Quarter Ending" in reason


def test_display_string_dates_pass():
    raw, error = rows_to_raw_csv(_sample_rows(), "sheet.csv")
    assert error is None
    reason = find_period_serial_number_value(
        raw, {"Quarter Ending": "period_end", "Total Revenue": "revenue"}
    )
    assert reason is None


def test_no_unambiguous_period_column_returns_none():
    raw, error = rows_to_raw_csv(_sample_rows(), "sheet.csv")
    assert error is None
    # Zero columns mapped as period_end -- validate_mapping's job to report, not this function's.
    assert find_period_serial_number_value(raw, {"Total Revenue": "revenue"}) is None


# --- Phase D session 3b: empty headers, size caps, spreadsheet source --------------------------


def _source(range_="A3:C5", sheet="P&L"):
    return {
        "platform": "excel",
        "sheet_name": sheet,
        "range": range_,
        "file_name": "book.xlsx",
        "modified_at": "2026-09-30T12:17:57Z",
    }


def test_single_empty_header_refuses_naming_its_column():
    rows = [["Quarter Ending", "", "Net Income"], ["2024-01-01", "1", "2"]]
    raw, error = rows_to_raw_csv(rows, "blank.csv")
    assert raw is None
    assert "empty header (column 2)" in error


def test_whitespace_only_header_counts_as_empty():
    raw, error = rows_to_raw_csv([["Quarter Ending", "   "], ["2024-01-01", "1"]], "ws.csv")
    assert raw is None
    assert "empty header" in error


def test_two_empty_headers_get_the_empty_message_not_an_empty_duplicate_list():
    raw, error = rows_to_raw_csv([["", ""], ["1", "2"]], "two.csv")
    assert raw is None
    assert "empty header (column 1)" in error
    assert "duplicate" not in error


def test_empty_header_with_source_names_the_sheet_cell():
    rows = [["Quarter Ending", "", "Net Income"], ["2024-01-01", "1", "2"]]
    raw, error = rows_to_raw_csv(rows, "blank.csv", source=_source("B3:D4"))
    assert raw is None
    assert "empty header (cell C3)" in error


def test_column_cap_refuses():
    rows = [[f"c{i}" for i in range(MAX_COLUMNS + 1)], ["1"] * (MAX_COLUMNS + 1)]
    raw, error = rows_to_raw_csv(rows, "wide.csv")
    assert raw is None
    assert f"limit is {MAX_COLUMNS}" in error


def test_row_cap_refuses_one_over_and_accepts_at_the_cap():
    at_cap = [["Date"]] + [["2024-01-01"]] * MAX_DATA_ROWS
    raw, error = rows_to_raw_csv(at_cap, "tall.csv")
    assert error is None
    raw, error = rows_to_raw_csv(at_cap + [["2024-01-01"]], "tall.csv")
    assert raw is None
    assert f"limit is {MAX_DATA_ROWS}" in error


def test_total_cell_cap_refuses_one_over_and_accepts_at_the_cap():
    # 100 columns x 500 rows (header included) = exactly MAX_TOTAL_CELLS; one more row is over.
    columns = 100
    header = [f"c{i}" for i in range(columns)]
    rows = [header] + [["1"] * columns] * (MAX_TOTAL_CELLS // columns - 1)
    assert sum(len(r) for r in rows) == MAX_TOTAL_CELLS
    raw, error = rows_to_raw_csv(rows, "big.csv")
    assert error is None
    raw, error = rows_to_raw_csv(rows + [["1"] * columns], "big.csv")
    assert raw is None
    assert f"limit is {MAX_TOTAL_CELLS}" in error


def test_cell_length_cap_refuses():
    rows = [["Note"], ["x" * (MAX_CELL_CHARS + 1)]]
    raw, error = rows_to_raw_csv(rows, "long.csv")
    assert raw is None
    assert f"longer than {MAX_CELL_CHARS}" in error


def test_source_is_kept_and_round_trips_through_json():
    raw, error = rows_to_raw_csv(_sample_rows(), "sheet.csv", uploaded_at=_UPLOADED_AT, source=_source())
    assert error is None
    assert raw.source == _source()
    packed = raw_csv_to_json(raw)
    assert packed["source"] == _source()
    assert raw_csv_from_json(packed, "sheet.csv", _UPLOADED_AT).source == _source()


def test_no_source_means_no_source_key_in_json():
    raw, _ = rows_to_raw_csv(_sample_rows(), "sheet.csv")
    assert raw.source is None
    assert "source" not in raw_csv_to_json(raw)


def test_source_range_must_match_the_rows_sent():
    raw, error = rows_to_raw_csv(_sample_rows(), "sheet.csv", source=_source("A3:C9"))
    assert raw is None
    assert "7 rows x 3 columns" in error
    assert "3 rows x 3 columns were sent" in error


def test_reversed_source_range_refuses():
    raw, error = rows_to_raw_csv(_sample_rows(), "sheet.csv", source=_source("C5:A3"))
    assert raw is None
    assert "isn't a valid A1 range" in error


def test_a1_helpers():
    assert [column_letter(i) for i in (0, 25, 26, 51, 701, 702)] == ["A", "Z", "AA", "AZ", "ZZ", "AAA"]
    assert parse_a1_range("A3:G7") == (3, 0, 7, 6)
    assert parse_a1_range("AA10:AB12") == (10, 26, 12, 27)
    assert parse_a1_range("a3:g7") is None
    assert parse_a1_range("A3") is None
    assert cell_reference("P&L", 4, 1) == "'P&L'!B4"
    assert cell_reference("Bob's Q1", 4, 0) == "'Bob''s Q1'!A4"
