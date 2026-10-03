import {
  EMPTY_CELL,
  formatA1Range,
  isoDate,
  normalizeGrid,
  numberCell,
  parseA1Range,
  quoteSheetName,
  textCell,
  type A1Range,
  type Cell,
  type NormalizedGrid,
} from './cellGrid';
import { ProviderApiError } from './connectData';

/**
 * Google Sheets reader (Phase D session 3b, chrome-extension-design.md SS4-SS5). One
 * spreadsheets.get call with grid data returns each cell's raw value, its displayed text and
 * its number-format type together, plus the range's origin, hidden rows and merges.
 *
 * How a cell is encoded (googleCell). Live-verified on the FA Spike Test sheet: plain numbers
 * carry NO numberFormat at all -- unformatted numbers are the common case -- and their
 * formattedValue is truncated (0.6145038167938931 shows as "0.6145038168"). So:
 * - effectiveValue.numberValue present and the format type is not DATE/DATE_TIME/TIME
 *   -> a number, sent as String(numberValue). NUMBER/CURRENCY/PERCENT are never consulted.
 * - DATE/DATE_TIME/TIME is the only thing the format type decides: DATE/DATE_TIME -> ISO from
 *   the serial; TIME -> the displayed text (a time of day isn't a period).
 * - stringValue / boolValue -> as displayed; errorValue -> the error literal.
 */

const SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets';

// The exact field mask live-tested in the session 3b risk check.
const GRID_FIELDS =
  'sheets(properties(sheetId,title),merges,data(startRow,startColumn,' +
  'rowMetadata(hiddenByUser,hiddenByFilter),' +
  'rowData(values(formattedValue,effectiveValue,effectiveFormat/numberFormat/type))))';

// Sheets' date serials count days from 1899-12-30 (there's no 1904 system in Sheets).
const SHEETS_EPOCH_MS = Date.UTC(1899, 11, 30);
const DAY_MS = 86_400_000;

const ERROR_LITERALS: Record<string, string> = {
  DIVIDE_BY_ZERO: '#DIV/0!',
  VALUE: '#VALUE!',
  REF: '#REF!',
  NAME: '#NAME?',
  NUM: '#NUM!',
  N_A: '#N/A',
  NULL_VALUE: '#NULL!',
  LOADING: '#LOADING',
  ERROR: '#ERROR!',
};

export interface GoogleCellData {
  formattedValue?: string;
  effectiveValue?: {
    numberValue?: number;
    stringValue?: string;
    boolValue?: boolean;
    errorValue?: { type?: string; message?: string };
  };
  effectiveFormat?: { numberFormat?: { type?: string } };
}

interface GoogleGridRange {
  startRowIndex?: number;
  endRowIndex?: number;
  startColumnIndex?: number;
  endColumnIndex?: number;
}

interface GoogleGridResponse {
  sheets?: {
    merges?: GoogleGridRange[];
    data?: {
      startRow?: number;
      startColumn?: number;
      rowMetadata?: { hiddenByUser?: boolean; hiddenByFilter?: boolean }[];
      rowData?: { values?: GoogleCellData[] }[];
    }[];
  }[];
}

export function sheetsSerialToIso(serial: number): string {
  // Whole seconds, so float noise in the fraction can't produce 23:59:59.999.
  const d = new Date(SHEETS_EPOCH_MS + Math.round((serial * DAY_MS) / 1000) * 1000);
  return isoDate(
    d.getUTCFullYear(),
    d.getUTCMonth() + 1,
    d.getUTCDate(),
    d.getUTCHours(),
    d.getUTCMinutes(),
    d.getUTCSeconds(),
  );
}

export function googleCell(data: GoogleCellData | undefined): Cell {
  const value = data?.effectiveValue;
  if (!value) return EMPTY_CELL;
  const formatType = data.effectiveFormat?.numberFormat?.type;
  const display = data.formattedValue ?? '';

  if (value.errorValue) {
    const literal = display || ERROR_LITERALS[value.errorValue.type ?? ''] || '#ERROR!';
    return { kind: 'error', value: literal, display: literal };
  }
  if (value.numberValue !== undefined) {
    if (formatType === 'DATE' || formatType === 'DATE_TIME') {
      return { kind: 'date', value: sheetsSerialToIso(value.numberValue), display };
    }
    if (formatType === 'TIME') return textCell(display);
    return numberCell(value.numberValue, display || String(value.numberValue));
  }
  if (value.boolValue !== undefined) {
    const shown = display || (value.boolValue ? 'TRUE' : 'FALSE');
    return { kind: 'bool', value: shown, display: shown };
  }
  if (value.stringValue !== undefined) {
    const shown = display || value.stringValue;
    return shown === '' ? EMPTY_CELL : textCell(shown);
  }
  return EMPTY_CELL;
}

/** The sheet tab and "Get link to this range" selection the tab URL currently shows. */
export function parseSheetsLocation(url: string): { gid: number | null; range: string | null } {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return { gid: null, range: null };
  }
  const hash = new URLSearchParams(parsed.hash.replace(/^#/, ''));
  const gidText = hash.get('gid') ?? parsed.searchParams.get('gid');
  const gid = gidText !== null && /^\d+$/.test(gidText) ? Number(gidText) : null;
  const rangeText = hash.get('range');
  const range = rangeText ? parseA1Range(rangeText) : null;
  return { gid, range: range ? formatA1Range(range) : null };
}

export interface SpreadsheetInfo {
  title: string;
  sheets: { sheetId: number; title: string }[];
}

async function getJson<T>(accessToken: string, url: string): Promise<T> {
  const response = await fetch(url, { headers: { Authorization: `Bearer ${accessToken}` } });
  if (!response.ok) throw new ProviderApiError(response.status);
  return (await response.json()) as T;
}

export async function fetchSpreadsheetInfo(
  accessToken: string,
  spreadsheetId: string,
): Promise<SpreadsheetInfo> {
  const json = await getJson<{
    properties?: { title?: string };
    sheets?: { properties?: { sheetId?: number; title?: string } }[];
  }>(
    accessToken,
    `${SHEETS_API}/${encodeURIComponent(spreadsheetId)}?fields=${encodeURIComponent('properties.title,sheets.properties(sheetId,title)')}`,
  );
  return {
    title: json.properties?.title ?? '(untitled spreadsheet)',
    sheets: (json.sheets ?? []).map((s) => ({
      sheetId: s.properties?.sheetId ?? 0,
      title: s.properties?.title ?? '',
    })),
  };
}

/**
 * Reads `range` of `sheetTitle` (or, with no range, the sheet's whole used range: a range
 * given as just the sheet name). Null when there's nothing non-empty to read.
 */
export async function readSheetGrid(
  accessToken: string,
  spreadsheetId: string,
  sheetTitle: string,
  range: A1Range | null,
): Promise<NormalizedGrid | null> {
  const a1 = range
    ? `${quoteSheetName(sheetTitle)}!${formatA1Range(range)}`
    : quoteSheetName(sheetTitle);
  const json = await getJson<GoogleGridResponse>(
    accessToken,
    `${SHEETS_API}/${encodeURIComponent(spreadsheetId)}?ranges=${encodeURIComponent(a1)}&fields=${encodeURIComponent(GRID_FIELDS)}`,
  );
  const sheet = json.sheets?.[0];
  const data = sheet?.data?.[0];
  const originRow = (data?.startRow ?? 0) + 1;
  const originCol = data?.startColumn ?? 0;

  const hiddenRows: number[] = [];
  (data?.rowMetadata ?? []).forEach((meta, i) => {
    if (meta.hiddenByUser || meta.hiddenByFilter) hiddenRows.push(originRow + i);
  });

  return normalizeGrid(sheetTitle, {
    originRow,
    originCol,
    // Rows and trailing cells come back ragged/absent when empty; normalizeGrid pads them.
    cells: (data?.rowData ?? []).map((row) => (row.values ?? []).map(googleCell)),
    hiddenRows,
    ...(range ? { requested: range } : {}),
    merges: (sheet?.merges ?? []).map((m) => ({
      startRow: (m.startRowIndex ?? 0) + 1,
      endRow: m.endRowIndex ?? 0,
      startCol: m.startColumnIndex ?? 0,
      endCol: (m.endColumnIndex ?? 1) - 1,
    })),
  });
}
