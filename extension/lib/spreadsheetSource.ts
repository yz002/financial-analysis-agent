import type { AuthProvider } from './authConfig';
import type { A1Range, CsvParseSource, NormalizedGrid } from './cellGrid';
import type { MicrosoftTab } from './connectData';
import {
  downloadWorkbook,
  ExcelFileError,
  excelFileRefusal,
  fetchExcelFileMeta,
  loadXlsx,
  readWorksheet,
  workbookSheetNames,
} from './excelReader';
import { fetchSpreadsheetInfo, parseSheetsLocation, readSheetGrid } from './sheetsReader';

/**
 * One connected spreadsheet, whichever platform it's on (Phase D session 3b): what the read
 * panel needs to fill its sheet selector and range field, and how to read a range. Lives
 * only in the side panel's memory -- nothing here is stored.
 */
export interface ConnectedFile {
  provider: AuthProvider;
  platform: CsvParseSource['platform'];
  name: string;
  /** Excel's lastModifiedDateTime ("data as of last save"); null for Sheets. */
  modifiedAt: string | null;
  sheets: string[];
  defaultSheet: string;
  /** A range= link's range; it applies to defaultSheet only. */
  defaultRange: string | null;
  /** Google reads need a fresh data token; Excel reads use the downloaded copy. */
  read(
    sheet: string,
    range: A1Range | null,
    accessToken: string | null,
  ): Promise<NormalizedGrid | null>;
}

export async function connectGoogleSheet(
  accessToken: string,
  spreadsheetId: string,
  tabUrl: string,
): Promise<ConnectedFile> {
  const info = await fetchSpreadsheetInfo(accessToken, spreadsheetId);
  const location = parseSheetsLocation(tabUrl);
  const gidSheet = info.sheets.find((s) => s.sheetId === location.gid);
  return {
    provider: 'google',
    platform: 'google_sheets',
    name: info.title,
    modifiedAt: null,
    sheets: info.sheets.map((s) => s.title),
    defaultSheet: gidSheet?.title ?? info.sheets[0]?.title ?? '',
    // A range= link refers to the gid sheet; without that match it isn't safe to pre-fill.
    defaultRange: gidSheet ? location.range : null,
    read: async (sheet, range, token) => {
      if (!token) throw new Error('A Google read needs a data token.');
      return readSheetGrid(token, spreadsheetId, sheet, range);
    },
  };
}

export async function connectExcelFile(
  accessToken: string,
  tab: MicrosoftTab,
): Promise<ConnectedFile> {
  const meta = await fetchExcelFileMeta(accessToken, tab);
  const refusal = excelFileRefusal(meta);
  if (refusal) throw new ExcelFileError(refusal);
  // The download URL is used once, here, and not kept: only the bytes stay in memory, so a
  // different sheet or range is re-parsed without downloading again.
  const data = await downloadWorkbook(meta, accessToken, tab);
  const XLSX = await loadXlsx();
  const sheets = workbookSheetNames(XLSX, data);
  return {
    provider: 'microsoft',
    platform: 'excel',
    name: meta.name,
    modifiedAt: meta.lastModified,
    sheets,
    defaultSheet: sheets[0] ?? '',
    defaultRange: null,
    read: async (sheet, range) => readWorksheet(XLSX, data, sheet, range),
  };
}
