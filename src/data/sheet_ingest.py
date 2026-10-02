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

Two Sheets-path checks have no `parse_csv` analog, by design (Phase D session 3b contract
amendment, backend/EXTENSION_INTEGRATION.md SS6):
- Size caps (MAX_DATA_ROWS/MAX_COLUMNS/MAX_CELL_CHARS/MAX_TOTAL_CELLS), checked before any
  other work. The whole grid is persisted as JSONB, so an unbounded grid is a storage problem,
  not just a slow request.
- An empty header cell is refused by position. `pd.read_csv` invents an "Unnamed: N" header
  for one; hand-building the DataFrame would otherwise silently accept a column named "", and
  two blank headers used to surface as a confusing empty "duplicate column headers ()".

`source` (optional) records where in a spreadsheet the rows came from -- platform, sheet name,
the A1 range sent, file name, and the file's modified time when known -- so normalize() can
cite a figure as a real cell address like 'P&L'!B4 rather than "data row 0". The range must
cover the rows exactly (row 0 = header), or the request is refused: a mismatched range would
produce wrong cell addresses, which is worse than none.
"""

import re
from collections import Counter
from datetime import datetime

import pandas as pd

from .csv_ingest import RawCsv

_SERIAL_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_A1_RANGE_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*):([A-Z]{1,3})([1-9][0-9]*)$")

# Size caps (contract amendment, Phase D session 3b). Generous for a financial statement --
# 40 quarters x 30 columns is ~1,200 cells -- while bounding what one request can persist.
MAX_DATA_ROWS = 2_000
MAX_COLUMNS = 200
MAX_CELL_CHARS = 1_000
MAX_TOTAL_CELLS = 50_000  # header row included


def column_letter(index: int) -> str:
    """0-based column index -> A1 column letters (0 -> "A", 26 -> "AA")."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _column_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def parse_a1_range(a1: str) -> tuple[int, int, int, int] | None:
    """"A3:G7" -> (start_row, start_col, end_row, end_col), rows 1-based, columns 0-based.
    None for anything else, including a reversed range."""
    m = _A1_RANGE_RE.match(a1)
    if m is None:
        return None
    start_col, start_row = _column_index(m.group(1)), int(m.group(2))
    end_col, end_row = _column_index(m.group(3)), int(m.group(4))
    if end_row < start_row or end_col < start_col:
        return None
    return start_row, start_col, end_row, end_col


def cell_reference(sheet_name: str, row: int, col: int) -> str:
    """A sheet-qualified cell address, e.g. 'P&L'!B4 (row 1-based, col 0-based). The sheet
    name is always quoted -- valid in both Sheets and Excel for any name -- with embedded
    quotes doubled."""
    quoted = sheet_name.replace("'", "''")
    return f"'{quoted}'!{column_letter(col)}{row}"


def _size_refusal(rows: list[list[str]], filename: str) -> str | None:
    columns = max((len(r) for r in rows), default=0)
    data_rows = max(len(rows) - 1, 0)
    if columns > MAX_COLUMNS:
        return f"{filename!r} has {columns} columns -- the limit is {MAX_COLUMNS}."
    if data_rows > MAX_DATA_ROWS:
        return f"{filename!r} has {data_rows} data rows -- the limit is {MAX_DATA_ROWS}."
    total = sum(len(r) for r in rows)
    if total > MAX_TOTAL_CELLS:
        return (
            f"{filename!r} has {total} cells -- the limit is {MAX_TOTAL_CELLS}. Select a "
            "smaller range."
        )
    for r_index, row in enumerate(rows):
        for c_index, cell in enumerate(row):
            if len(cell) > MAX_CELL_CHARS:
                where = "the header row" if r_index == 0 else f"data row {r_index}"
                return (
                    f"{filename!r} has a cell longer than {MAX_CELL_CHARS} characters "
                    f"({where}, column {c_index + 1})."
                )
    return None


def rows_to_raw_csv(
    rows: list[list[str]],
    filename: str,
    uploaded_at: datetime | None = None,
    source: dict | None = None,
) -> tuple[RawCsv | None, str | None]:
    """
    Structurally build a RawCsv from a Sheet range's 2D array (`rows[0]` is the header row,
    `rows[1:]` are data rows). Returns (RawCsv, None) on success, or (None, reason) on a
    structural refusal -- never a partially-built result. `uploaded_at` defaults to now();
    passing it explicitly is for deterministic tests.

    Refuses (reason named) for: a grid over the size caps, an empty header cell, duplicate
    column headers, an empty header row, a header row but zero data rows, a data row whose
    length doesn't match the header's (a ragged 2D array `pd.DataFrame(rows[1:],
    columns=rows[0])` can't hand-build), or a `source` whose range doesn't cover the rows
    exactly. `source` is kept on the RawCsv as given (see the module docstring).
    """
    uploaded_at = uploaded_at or datetime.now()

    size_error = _size_refusal(rows, filename)
    if size_error:
        return None, size_error

    header = rows[0] if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []

    origin = parse_a1_range(source["range"]) if source else None
    for c_index, h in enumerate(header):
        if h.strip() == "":
            where = (
                f"cell {column_letter(origin[1] + c_index)}{origin[0]}"
                if origin
                else f"column {c_index + 1}"
            )
            return None, (
                f"{filename!r} has an empty header ({where}) -- the first row of the range must "
                "be the column headers, with a header for every column."
            )

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

    if source is not None:
        if origin is None:
            return None, f"{filename!r}: source range {source.get('range')!r} isn't a valid A1 range."
        start_row, start_col, end_row, end_col = origin
        if (end_row - start_row + 1, end_col - start_col + 1) != (len(rows), len(header)):
            return None, (
                f"{filename!r}: source range {source['range']} is "
                f"{end_row - start_row + 1} rows x {end_col - start_col + 1} columns, but "
                f"{len(rows)} rows x {len(header)} columns were sent."
            )

    return RawCsv(df=df, filename=filename, uploaded_at=uploaded_at, source=source), None


def raw_csv_to_json(raw: RawCsv) -> dict:
    """
    Pack a RawCsv into a JSON-safe dict for persisting to csv_statements.raw_columns (JSONB)
    between /v1/csv/parse and /v1/csv/{id}/propose-mapping|confirm. See raw_csv_from_json for
    the inverse.
    """
    packed = {
        "columns": raw.df.columns.tolist(),
        "data_rows": raw.df.values.tolist(),
    }
    if raw.source is not None:
        packed["source"] = raw.source
    return packed


def raw_csv_from_json(data: dict, filename: str, uploaded_at: datetime) -> RawCsv:
    """
    Rebuild a RawCsv from raw_csv_to_json's stored shape. Does not re-run rows_to_raw_csv's
    structural checks -- they already passed when the row was first persisted at parse time.
    """
    df = pd.DataFrame(data["data_rows"], columns=data["columns"])
    return RawCsv(df=df, filename=filename, uploaded_at=uploaded_at, source=data.get("source"))


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
