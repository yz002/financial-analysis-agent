"""
Sheet-range -> RawCsv adapter for the Sheets Add-on backend (Phase B session 4).

`rows_to_raw_csv` builds a `src.data.csv_ingest.RawCsv` directly from a Google Sheet range's
2D array of display-string cell values (`pd.DataFrame(rows[1:], columns=rows[0])`), rather than
round-tripping through CSV text via `csv_ingest.parse_csv`. `parse_csv` is fundamentally a
bytes/text-in function (decodes utf-8-sig, feeds `csv.reader`/`pd.read_csv`) built for arbitrary
uploaded files; re-serializing already-typed Apps Script values back to CSV text just to
re-parse them would risk lossy formatting round-trips and run an encoding-detection codepath
against data that was never bytes on disk. So this module duplicates `parse_csv`'s three
structural checks (duplicate headers, empty header, zero data rows) directly against the
Sheet's header/data rows, in the same order and with matching wording, then hand-builds a
`RawCsv` -- mirroring `parse_csv`'s exact `(RawCsv, None) | (None, reason)` refusal contract so
`/v1/csv/parse` behaves identically regardless of which ingestion path fed it. A fourth,
Sheets-only safeguard (a ragged 2D array -- a data row whose length doesn't match the header)
is also caught here, since hand-building the DataFrame directly has no `pd.read_csv`-style
tolerance for that shape mismatch the way the CSV-text path does.

The one Sheets-specific concern with no CSV analog: Apps Script date cells can serialize either
as display strings or as Sheets' internal date-serial numbers, depending on how
`getValues()`/`getDisplayValues()` is called on the Apps Script side. The interface contract
requires display-string values for the period-date column specifically (out of scope to build
here -- that's the Apps Script side, a later phase) -- `find_period_serial_number_value` is
this adapter's enforcement of that contract: a clear, distinct rejection rather than silently
letting a serial number either fail `pd.to_datetime` as generic row-level noise or, worse,
coincidentally parse into a wrong date.
"""

import re
from collections import Counter
from datetime import datetime

import pandas as pd

from .csv_ingest import RawCsv

_SERIAL_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def rows_to_raw_csv(
    rows: list[list[str]], filename: str, uploaded_at: datetime | None = None
) -> tuple[RawCsv | None, str | None]:
    """
    Structurally build a RawCsv from a Sheet range's 2D array (`rows[0]` is the header row,
    `rows[1:]` are data rows). Returns (RawCsv, None) on success, or (None, reason) on a
    structural refusal -- never a partially-built result. `uploaded_at` defaults to now();
    passing it explicitly is for deterministic tests.

    Refuses (reason named) for: duplicate column headers, an empty header row, a header row
    but zero data rows, or a data row whose length doesn't match the header's (a ragged 2D
    array `pd.DataFrame(rows[1:], columns=rows[0])` can't hand-build).
    """
    uploaded_at = uploaded_at or datetime.now()

    header = rows[0] if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []

    header_counts = Counter(header)
    dupes = sorted(h for h, count in header_counts.items() if count > 1)
    if dupes:
        return None, (
            f"{filename!r} has duplicate column headers ({', '.join(dupes)}) -- each column "
            "needs a distinct header so it can be mapped unambiguously."
        )

    if len(header) == 0:
        return None, f"{filename!r} has no columns -- no header row was found."
    if len(data_rows) == 0:
        return None, f"{filename!r} has a header row but no data rows."

    try:
        df = pd.DataFrame(data_rows, columns=header)
    except ValueError as e:
        return None, f"{filename!r} could not be parsed as a table: {e}"

    return RawCsv(df=df, filename=filename, uploaded_at=uploaded_at), None


def raw_csv_to_json(raw: RawCsv) -> dict:
    """
    Pack a RawCsv into a JSON-safe dict for persisting to csv_statements.raw_columns (JSONB)
    between /v1/csv/parse and /v1/csv/{id}/propose-mapping|confirm. See raw_csv_from_json for
    the inverse.
    """
    return {
        "columns": raw.df.columns.tolist(),
        "data_rows": raw.df.values.tolist(),
    }


def raw_csv_from_json(data: dict, filename: str, uploaded_at: datetime) -> RawCsv:
    """
    Rebuild a RawCsv from raw_csv_to_json's stored shape. Does not re-run rows_to_raw_csv's
    structural checks -- they already passed when the row was first persisted at parse time.
    """
    df = pd.DataFrame(data["data_rows"], columns=data["columns"])
    return RawCsv(df=df, filename=filename, uploaded_at=uploaded_at)


def find_period_serial_number_value(raw: RawCsv, mapping: dict[str, str]) -> str | None:
    """
    Check the column mapped to "period_end" (if exactly one is mapped -- zero or multiple is an
    ambiguity src.analysis.csv_statement.validate_mapping already reports, so this returns None
    rather than duplicating that error) for values that look like an unconverted Sheets
    date-serial number (a bare integer/decimal string) rather than a display-string date.
    Returns a plain-English refusal reason naming the offending value on a match, else None.
    """
    period_columns = [column for column, role in mapping.items() if role == "period_end"]
    if len(period_columns) != 1:
        return None

    column = period_columns[0]
    if column not in raw.df.columns:
        return None

    for value in raw.df[column]:
        if isinstance(value, str) and _SERIAL_NUMBER_RE.match(value.strip()):
            return (
                f"The {column!r} column (mapped as the period/date column) contains the raw "
                f"numeric value {value!r} instead of a date string. The Sheet must send "
                "display-string dates for the period column (e.g. via Apps Script's "
                "getDisplayValues()), not raw typed values or Sheets' internal date-serial "
                "numbers."
            )
    return None
