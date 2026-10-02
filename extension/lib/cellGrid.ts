/**
 * The shared shape both spreadsheet readers produce (Phase D session 3b), and everything done
 * to it before it reaches POST /v1/csv/parse: padding, trimming, notices, pre-checks and the
 * request itself. Cell encoding follows backend/EXTENSION_INTEGRATION.md SS6 (amended 3b):
 *
 * - a date cell -> ISO-8601 (YYYY-MM-DD, or YYYY-MM-DDTHH:MM:SS with a time)
 * - a numeric cell -> its underlying value as String(value): full precision, no currency or
 *   percent formatting, exponent form at the extremes (1e+21), which the backend parses
 * - everything else -> as displayed (text, TRUE/FALSE, error literals like #DIV/0!)
 *
 * Each reader decides which of those a cell is; nothing here re-guesses it.
 */

// Mirrors src/data/sheet_ingest.py and backend/app/main.py (MAX_CSV_PARSE_BODY_BYTES). The
// backend refuses anything over these anyway; checking here gives a message that names cells.
export const MAX_COLUMNS = 200;
export const MAX_DATA_ROWS = 2_000;
export const MAX_CELL_CHARS = 1_000;
export const MAX_TOTAL_CELLS = 50_000;
export const MAX_BODY_BYTES = 2 * 1024 * 1024;

export type CellKind = 'empty' | 'number' | 'date' | 'text' | 'bool' | 'error';

export interface Cell {
  kind: CellKind;
  /** What's sent to the backend. */
  value: string;
  /** What the spreadsheet shows -- for the preview's tooltip only, never sent. */
  display: string;
}

export const EMPTY_CELL: Cell = { kind: 'empty', value: '', display: '' };

export function numberCell(value: number, display: string): Cell {
  return { kind: 'number', value: String(value), display };
}

export function textCell(display: string): Cell {
  return { kind: 'text', value: display, display };
}

/** A1 range; rows are 1-based sheet rows, columns 0-based indexes. */
export interface A1Range {
  startRow: number;
  startCol: number;
  endRow: number;
  endCol: number;
}

export function columnLetter(index: number): string {
  let letters = '';
  let n = index + 1;
  while (n > 0) {
    const rem = (n - 1) % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    n = Math.floor((n - 1) / 26);
  }
  return letters;
}

function columnIndex(letters: string): number {
  let index = 0;
  for (const ch of letters) index = index * 26 + (ch.charCodeAt(0) - 64);
  return index - 1;
}

const A1_RANGE = /^([A-Z]{1,3})([1-9][0-9]*):([A-Z]{1,3})([1-9][0-9]*)$/;

/** Parses a closed range like "A3:G7" (case-insensitive, surrounding spaces ignored). */
export function parseA1Range(text: string): A1Range | null {
  const m = A1_RANGE.exec(text.trim().toUpperCase());
  if (!m) return null;
  const range = {
    startCol: columnIndex(m[1]!),
    startRow: Number(m[2]),
    endCol: columnIndex(m[3]!),
    endRow: Number(m[4]),
  };
  if (range.endRow < range.startRow || range.endCol < range.startCol) return null;
  return range;
}

export function formatA1Range(r: A1Range): string {
  return `${columnLetter(r.startCol)}${r.startRow}:${columnLetter(r.endCol)}${r.endRow}`;
}

export function cellAddress(row: number, col: number): string {
  return `${columnLetter(col)}${row}`;
}

/** A sheet name as A1 notation needs it: always quoted, embedded quotes doubled. */
export function quoteSheetName(name: string): string {
  return `'${name.replace(/'/g, "''")}'`;
}

/** Date parts -> ISO-8601; the time is only included when it isn't midnight. */
export function isoDate(y: number, m: number, d: number, H = 0, M = 0, S = 0): string {
  const pad = (n: number, w = 2) => String(n).padStart(w, '0');
  const date = `${pad(y, 4)}-${pad(m)}-${pad(d)}`;
  return H === 0 && M === 0 && S === 0 ? date : `${date}T${pad(H)}:${pad(M)}:${pad(S)}`;
}

// Text that reads as a date but isn't stored as one, so its day/month order is only a guess:
// numeric d/m/y-style dates and month-name dates. Unambiguous ISO text (2025-03-31) is left out.
const NUMERIC_DATE_TEXT = /^\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}$/;
const ISO_DATE_TEXT = /^\d{4}-\d{2}-\d{2}$/;
const MONTH_NAME_DATE_TEXT =
  /^(\d{1,2}[\s-])?(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?([\s-]\d{1,2},?)?[\s-]\d{2,4}$/i;

export function looksLikeDateText(text: string): boolean {
  const t = text.trim();
  if (ISO_DATE_TEXT.test(t)) return false;
  return NUMERIC_DATE_TEXT.test(t) || MONTH_NAME_DATE_TEXT.test(t);
}

/** What a reader hands over: cells from the origin onward (possibly ragged), plus sheet facts. */
export interface RawGrid {
  /** Sheet row (1-based) and column (0-based) of cells[0][0]. */
  originRow: number;
  originCol: number;
  cells: Cell[][];
  /** 1-based sheet rows hidden by the user or a filter. */
  hiddenRows: number[];
  merges: A1Range[];
}

export interface Notice {
  kind: 'hidden-rows' | 'merged-cells' | 'formula-errors' | 'text-dates' | 'periods-across-columns';
  message: string;
}

export interface NormalizedGrid {
  sheetName: string;
  /** The exact rectangle `rows` covers, header row first. */
  range: A1Range;
  rows: Cell[][];
  notices: Notice[];
}

const isEmpty = (c: Cell | undefined) => c === undefined || c.value === '';

/** Lists items, shortened past `max` ("A4, A5, A6 and 4 more"). */
function shortList(items: string[], max = 8): string {
  if (items.length <= max) return items.join(', ');
  return `${items.slice(0, max).join(', ')} and ${items.length - max} more`;
}

/**
 * Pads ragged rows to a rectangle and trims trailing all-empty rows and columns -- the only
 * trimming, so the result is still one contiguous block starting at the origin and every
 * cell keeps its address. Returns null when nothing non-empty is left.
 */
export function normalizeGrid(sheetName: string, raw: RawGrid): NormalizedGrid | null {
  let height = raw.cells.length;
  while (height > 0 && raw.cells[height - 1]!.every(isEmpty)) height--;
  let width = 0;
  for (let r = 0; r < height; r++) {
    const row = raw.cells[r]!;
    for (let c = row.length - 1; c >= width; c--) {
      if (!isEmpty(row[c])) {
        width = c + 1;
        break;
      }
    }
  }
  if (height === 0 || width === 0) return null;

  const rows: Cell[][] = [];
  for (let r = 0; r < height; r++) {
    const source = raw.cells[r]!;
    const row: Cell[] = [];
    for (let c = 0; c < width; c++) row.push(source[c] ?? EMPTY_CELL);
    rows.push(row);
  }
  const range: A1Range = {
    startRow: raw.originRow,
    startCol: raw.originCol,
    endRow: raw.originRow + height - 1,
    endCol: raw.originCol + width - 1,
  };
  return { sheetName, range, rows, notices: buildNotices(rows, range, raw) };
}

function buildNotices(rows: Cell[][], range: A1Range, raw: RawGrid): Notice[] {
  const notices: Notice[] = [];
  const at = (r: number, c: number) => cellAddress(range.startRow + r, range.startCol + c);

  const hidden = raw.hiddenRows.filter((r) => r >= range.startRow && r <= range.endRow);
  if (hidden.length > 0) {
    notices.push({
      kind: 'hidden-rows',
      message:
        `${hidden.length === 1 ? 'A hidden row is' : `${hidden.length} hidden rows are`} ` +
        `included (row ${shortList(hidden.map(String))}).`,
    });
  }

  const merges = raw.merges.filter(
    (m) =>
      m.startRow <= range.endRow &&
      m.endRow >= range.startRow &&
      m.startCol <= range.endCol &&
      m.endCol >= range.startCol,
  );
  if (merges.length > 0) {
    notices.push({
      kind: 'merged-cells',
      message:
        `Merged cells (${shortList(merges.map(formatA1Range))}): only each merge's top-left ` +
        'cell has a value; the rest are sent empty.',
    });
  }

  const errors: string[] = [];
  const textDates: string[] = [];
  rows.forEach((row, r) =>
    row.forEach((cell, c) => {
      if (cell.kind === 'error') errors.push(at(r, c));
      if (cell.kind === 'text' && looksLikeDateText(cell.value)) textDates.push(at(r, c));
    }),
  );
  if (errors.length > 0) {
    notices.push({
      kind: 'formula-errors',
      message: `Formula errors are sent as shown and will count as missing values: ${shortList(errors)}.`,
    });
  }
  if (textDates.length > 0) {
    notices.push({
      kind: 'text-dates',
      message:
        'These cells look like dates but carry no date format, so they are sent as displayed ' +
        `text, and day/month order may be misread: ${shortList(textDates)}.`,
    });
  }

  const datedHeaders = rows[0]!
    .slice(1)
    .filter((c) => c.kind === 'date' || (c.kind === 'text' && looksLikeDateText(c.value)));
  if (datedHeaders.length >= 2) {
    notices.push({
      kind: 'periods-across-columns',
      message:
        'The header row looks like dates. Periods laid out across columns are not supported ' +
        'yet: each row needs to be one period, with the dates in a single column.',
    });
  }
  return notices;
}

/**
 * The backend's structural refusals, checked first so the message can name the cell and
 * point at the range field. Returns null when the grid can be sent.
 */
export function precheckGrid(grid: NormalizedGrid): string | null {
  const { rows, range } = grid;
  const width = rows[0]!.length;
  const dataRows = rows.length - 1;

  if (width > MAX_COLUMNS) {
    return `The range has ${width} columns; the limit is ${MAX_COLUMNS}. Select a narrower range.`;
  }
  if (dataRows > MAX_DATA_ROWS) {
    return `The range has ${dataRows} data rows; the limit is ${MAX_DATA_ROWS}. Select a smaller range.`;
  }
  if (width * rows.length > MAX_TOTAL_CELLS) {
    return `The range has ${width * rows.length} cells; the limit is ${MAX_TOTAL_CELLS}. Select a smaller range.`;
  }
  for (let r = 0; r < rows.length; r++) {
    for (let c = 0; c < width; c++) {
      if (rows[r]![c]!.value.length > MAX_CELL_CHARS) {
        return `Cell ${cellAddress(range.startRow + r, range.startCol + c)} is longer than ${MAX_CELL_CHARS} characters.`;
      }
    }
  }

  const header = rows[0]!.map((c) => c.value);
  const firstEmpty = header.findIndex((h) => h.trim() === '');
  if (firstEmpty !== -1) {
    // One filled cell across a wider row is the classic title row ("Acme Co — P&L").
    const filled = header.filter((h) => h.trim() !== '').length;
    if (filled <= 1 && header.length > 2) {
      return (
        `Row ${range.startRow} looks like a title, not column headers. Set the range to start ` +
        'at your header row (for example A3:G7).'
      );
    }
    return (
      `Header cell ${cellAddress(range.startRow, range.startCol + firstEmpty)} is empty. The ` +
      'first row of the range must be your column headers. Set the range to start at the ' +
      'header row, or give every column a header.'
    );
  }
  const seen = new Map<string, number>();
  for (let c = 0; c < header.length; c++) {
    const first = seen.get(header[c]!);
    if (first !== undefined) {
      return (
        `Columns ${columnLetter(range.startCol + first)} and ${columnLetter(range.startCol + c)} ` +
        `both have the header "${header[c]}". Each column needs a distinct header.`
      );
    }
    seen.set(header[c]!, c);
  }
  if (dataRows === 0) {
    return 'The range has a header row but no data rows below it.';
  }
  return null;
}

export interface CsvParseSource {
  platform: 'google_sheets' | 'excel';
  sheet_name: string;
  range: string;
  file_name: string;
  modified_at: string | null;
}

export interface CsvParseRequest {
  rows: string[][];
  filename: string;
  source: CsvParseSource;
}

export function buildParseRequest(
  grid: NormalizedGrid,
  file: { name: string; platform: CsvParseSource['platform']; modifiedAt: string | null },
): CsvParseRequest {
  return {
    rows: grid.rows.map((row) => row.map((c) => c.value)),
    filename: `${file.name} — ${grid.sheetName}`,
    source: {
      platform: file.platform,
      sheet_name: grid.sheetName,
      range: formatA1Range(grid.range),
      file_name: file.name,
      modified_at: file.modifiedAt,
    },
  };
}

/** Null when the request fits MAX_BODY_BYTES on the wire, else a message to show. */
export function bodySizeError(request: CsvParseRequest): string | null {
  const bytes = new TextEncoder().encode(JSON.stringify(request)).length;
  return bytes > MAX_BODY_BYTES
    ? `This selection is ${(bytes / (1024 * 1024)).toFixed(1)} MB; the limit is 2 MB. Select a smaller range.`
    : null;
}
