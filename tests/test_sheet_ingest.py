"""
Tests for src/data/sheet_ingest.py: the Sheet-range -> RawCsv adapter. Fully offline, no
network -- mirrors tests/test_csv_ingest.py's style (plain functions, no classes).
"""

from datetime import datetime

from src.data.sheet_ingest import (
    find_period_serial_number_value,
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
