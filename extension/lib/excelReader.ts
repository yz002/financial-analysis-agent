import type * as XLSXTypes from 'xlsx';
import {
  EMPTY_CELL,
  isoDate,
  normalizeGrid,
  numberCell,
  textCell,
  type A1Range,
  type Cell,
  type NormalizedGrid,
} from './cellGrid';
import { fetchDriveItem, type MicrosoftTab } from './connectData';

/**
 * Excel reader (Phase D session 3b, chrome-extension-design.md SS4 "download + parse (B2)").
 * The file is resolved through Graph (both URL shapes, see connectData.ts), downloaded from
 * its preauthenticated @microsoft.graph.downloadUrl, and parsed here with SheetJS 0.20.3.
 * Only the resulting rows ever reach the backend.
 *
 * SheetJS is pinned at install time to the cdn.sheetjs.com tarball (package.json) and loaded
 * with a dynamic import() only when an Excel file is read: MV3 forbids loading remote code at
 * runtime, and the ~490 kB chunk shouldn't load for Google-only use.
 *
 * How a cell is encoded (excelCell): a numeric cell whose number format is a date format ->
 * ISO via SSF.parse_date_code, which honors the workbook's 1900/1904 date system and returns
 * plain date parts (no JS Date, so no timezone shift); any other numeric cell -> String(v);
 * everything else as Excel displays it. Formula cells use their cached value.
 */

export const MAX_EXCEL_FILE_BYTES = 10 * 1024 * 1024;
const EXCEL_EXTENSIONS = ['.xlsx', '.xlsm', '.xlsb', '.xls'];

type Xlsx = typeof XLSXTypes;

// SheetJS types SSF as `any`; these are the two helpers used, as documented.
interface Ssf {
  is_date(format: string): boolean;
  parse_date_code(
    value: number,
    opts?: { date1904?: boolean },
  ): { y: number; m: number; d: number; H: number; M: number; S: number } | null;
}

export class ExcelFileError extends Error {}

export interface ExcelFileMeta {
  name: string;
  size: number;
  lastModified: string | null;
  /** Preauthenticated: a credential. Never logged, stored or shown. */
  downloadUrl: string;
}

export async function fetchExcelFileMeta(
  accessToken: string,
  tab: MicrosoftTab,
): Promise<ExcelFileMeta> {
  const json = await fetchDriveItem<{
    name?: string;
    size?: number;
    lastModifiedDateTime?: string;
    '@microsoft.graph.downloadUrl'?: string;
  }>(accessToken, tab, 'id,name,size,lastModifiedDateTime,eTag,@microsoft.graph.downloadUrl');
  return {
    name: json.name ?? '(unnamed file)',
    size: json.size ?? 0,
    lastModified: json.lastModifiedDateTime ?? null,
    downloadUrl: json['@microsoft.graph.downloadUrl'] ?? '',
  };
}

/** Why this file won't be downloaded, or null when it can be. */
export function excelFileRefusal(meta: ExcelFileMeta): string | null {
  const lower = meta.name.toLowerCase();
  if (!EXCEL_EXTENSIONS.some((ext) => lower.endsWith(ext))) {
    return `"${meta.name}" isn't an Excel workbook (.xlsx, .xlsm, .xlsb or .xls).`;
  }
  if (meta.size > MAX_EXCEL_FILE_BYTES) {
    return `"${meta.name}" is ${(meta.size / (1024 * 1024)).toFixed(1)} MB; files over 10 MB can't be read.`;
  }
  if (!meta.downloadUrl) return `"${meta.name}" can't be downloaded with this account's access.`;
  return null;
}

export async function downloadWorkbook(meta: ExcelFileMeta): Promise<ArrayBuffer> {
  // No Authorization header: the URL itself carries the access (Graph's documented JS path).
  const response = await fetch(meta.downloadUrl, { credentials: 'omit' });
  if (!response.ok) {
    throw new ExcelFileError(`Couldn't download "${meta.name}" (${response.status}). Try again.`);
  }
  const data = await response.arrayBuffer();
  if (data.byteLength > MAX_EXCEL_FILE_BYTES) {
    throw new ExcelFileError(`"${meta.name}" is over 10 MB and can't be read.`);
  }
  return data;
}

export async function loadXlsx(): Promise<Xlsx> {
  return import('xlsx');
}

export function workbookSheetNames(XLSX: Xlsx, data: ArrayBuffer): string[] {
  return XLSX.read(data, { type: 'array', bookSheets: true }).SheetNames;
}

export function excelCell(
  XLSX: Xlsx,
  cell: XLSXTypes.CellObject | undefined,
  date1904: boolean,
): Cell {
  if (!cell || cell.t === 'z') return EMPTY_CELL;
  const display = cell.w ?? (cell.v === undefined ? '' : String(cell.v));
  switch (cell.t) {
    case 'n': {
      const v = cell.v as number;
      const ssf = XLSX.SSF as Ssf;
      if (typeof cell.z === 'string' && ssf.is_date(cell.z)) {
        // Under 1 is a time of day, not a date: send what Excel shows, like Sheets' TIME.
        const parts = v >= 1 ? ssf.parse_date_code(v, { date1904 }) : null;
        if (!parts) return textCell(display);
        return {
          kind: 'date',
          value: isoDate(parts.y, parts.m, parts.d, parts.H, parts.M, Math.floor(parts.S)),
          display,
        };
      }
      return numberCell(v, display);
    }
    case 'b':
      return { kind: 'bool', value: display || (cell.v ? 'TRUE' : 'FALSE'), display };
    case 'e':
      return { kind: 'error', value: display || '#ERROR!', display: display || '#ERROR!' };
    default:
      return display === '' ? EMPTY_CELL : textCell(display);
  }
}

/**
 * Reads `range` of `sheetName` (or its used range, `!ref`, when range is null). The loop is
 * clamped to the used range -- everything outside it is empty -- so a huge typed range can't
 * walk millions of cells. Null when there's nothing non-empty to read.
 */
export function readWorksheet(
  XLSX: Xlsx,
  data: ArrayBuffer,
  sheetName: string,
  range: A1Range | null,
): NormalizedGrid | null {
  const workbook = XLSX.read(data, {
    type: 'array',
    cellNF: true, // keeps each cell's number format in .z, which is what identifies a date
    cellStyles: true, // needed for '!rows' hidden flags (verified in the 3b risk check)
    cellDates: false, // dates stay serials; parse_date_code converts them without a JS Date
    sheets: [sheetName],
  });
  const sheet = workbook.Sheets[sheetName];
  if (!sheet) {
    throw new ExcelFileError(`The sheet "${sheetName}" wasn't found. Read the file again.`);
  }
  if (!sheet['!ref']) return null;
  const date1904 = Boolean(workbook.Workbook?.WBProps?.date1904);

  const used = XLSX.utils.decode_range(sheet['!ref']);
  const target = range ?? {
    startRow: used.s.r + 1,
    startCol: used.s.c,
    endRow: used.e.r + 1,
    endCol: used.e.c,
  };
  const lastRow = Math.min(target.endRow, used.e.r + 1);
  const lastCol = Math.min(target.endCol, used.e.c);

  const cells: Cell[][] = [];
  for (let row = target.startRow; row <= lastRow; row++) {
    const cellRow: Cell[] = [];
    for (let col = target.startCol; col <= lastCol; col++) {
      const address = XLSX.utils.encode_cell({ r: row - 1, c: col });
      cellRow.push(excelCell(XLSX, sheet[address] as XLSXTypes.CellObject | undefined, date1904));
    }
    cells.push(cellRow);
  }

  const hiddenRows: number[] = [];
  (sheet['!rows'] ?? []).forEach((info, r) => {
    if (info?.hidden) hiddenRows.push(r + 1);
  });

  return normalizeGrid(sheetName, {
    originRow: target.startRow,
    originCol: target.startCol,
    cells,
    hiddenRows,
    merges: (sheet['!merges'] ?? []).map((m) => ({
      startRow: m.s.r + 1,
      startCol: m.s.c,
      endRow: m.e.r + 1,
      endCol: m.e.c,
    })),
  });
}
