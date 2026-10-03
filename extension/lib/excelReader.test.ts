import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as XLSX from 'xlsx';
import { formatA1Range, parseA1Range, precheckGrid } from './cellGrid';
import {
  downloadWorkbook,
  excelCell,
  excelFileRefusal,
  ExcelFileError,
  fetchExcelFileMeta,
  MAX_EXCEL_FILE_BYTES,
  readWorksheet,
  workbookSheetNames,
  type ExcelFileMeta,
} from './excelReader';

afterEach(() => {
  vi.unstubAllGlobals();
});

// 1900-system serials; the 1904 system counts 1462 days fewer for the same date.
const MAR_31_2025 = 45747;
const JUN_30_2025 = 45838;
const DAYS_1900_TO_1904 = 1462;

/**
 * The synthetic fixture: a P&L laid out like the FA Spike Test file (title row, blank row,
 * header in row 3), plus the edge cases -- a merged title, an error cell, date-looking text,
 * a hidden row, and a used range (!ref) that runs past the data into formatted blanks.
 * Generated with SheetJS itself, in either date system.
 */
function syntheticWorkbook(date1904 = false): ArrayBuffer {
  const shift = date1904 ? DAYS_1900_TO_1904 : 0;
  const rows: XLSX.RowInfo[] = [];
  rows[4] = { hidden: true }; // sheet row 5
  const sheet: XLSX.WorkSheet = {
    A1: { t: 's', v: 'Acme Co — Quarterly P&L' },
    A3: { t: 's', v: 'Period end' },
    B3: { t: 's', v: 'Revenue' },
    C3: { t: 's', v: 'Margin' },
    D3: { t: 's', v: 'Note' },
    A4: { t: 'n', v: MAR_31_2025 - shift, z: 'm/d/yy' }, // Excel's built-in Short Date
    B4: { t: 'n', v: 1250000, z: '"$"#,##0' },
    C4: { t: 'n', v: 0.6145038167938931, z: '0.0%' },
    D4: { t: 'e', v: 0x07 }, // #DIV/0!
    A5: { t: 'n', v: JUN_30_2025 - shift, z: 'd-mmm-yy' },
    B5: { t: 'n', v: -45000, z: '"$"#,##0;("$"#,##0)' },
    C5: { t: 'n', v: 1e21 },
    D5: { t: 's', v: '3/31/2025' },
    '!ref': 'A1:F9',
    '!merges': [{ s: { r: 0, c: 0 }, e: { r: 0, c: 3 } }],
    '!rows': rows,
  };
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, sheet, 'P&L');
  XLSX.utils.book_append_sheet(workbook, { A1: { t: 's', v: 'n/a' }, '!ref': 'A1:A1' }, 'Notes');
  workbook.Workbook = { WBProps: { date1904 } };
  return XLSX.write(workbook, { type: 'array', bookType: 'xlsx' }) as ArrayBuffer;
}

const values = (grid: ReturnType<typeof readWorksheet>) =>
  grid!.rows.map((r) => r.map((c) => c.value));

describe('readWorksheet', () => {
  it('lists the sheet names for the selector', () => {
    expect(workbookSheetNames(XLSX, syntheticWorkbook())).toEqual(['P&L', 'Notes']);
  });

  it.each([false, true])('sends ISO dates and underlying numbers (date1904=%s)', (date1904) => {
    const grid = readWorksheet(XLSX, syntheticWorkbook(date1904), 'P&L', parseA1Range('A3:D5'));
    expect(values(grid)).toEqual([
      ['Period end', 'Revenue', 'Margin', 'Note'],
      ['2025-03-31', '1250000', '0.6145038167938931', '#DIV/0!'],
      ['2025-06-30', '-45000', '1e+21', '3/31/2025'],
    ]);
    // What Excel shows is kept for the preview's tooltip only.
    expect(grid!.rows[1]!.map((c) => c.display)).toEqual(['3/31/25', '$1,250,000', '61.5%', '#DIV/0!']);
    expect(formatA1Range(grid!.range)).toBe('A3:D5');
  });

  it('notes the hidden row, the error and the date-looking text by address', () => {
    const grid = readWorksheet(XLSX, syntheticWorkbook(), 'P&L', parseA1Range('A3:D5'))!;
    const byKind = Object.fromEntries(grid.notices.map((x) => [x.kind, x.message]));
    expect(byKind['hidden-rows']).toContain('row 5');
    expect(byKind['formula-errors']).toContain('D4');
    expect(byKind['text-dates']).toContain('D5');
    expect(byKind['merged-cells']).toBeUndefined(); // the title merge is above A3
  });

  it('reads the used range when no range is given, trimming the formatted blanks past the data', () => {
    const grid = readWorksheet(XLSX, syntheticWorkbook(), 'P&L', null)!;
    expect(formatA1Range(grid.range)).toBe('A1:D5');
    expect(grid.notices.map((x) => x.kind)).toContain('merged-cells');
    // The title row becomes the header -- the pre-check points the person at the range field.
    expect(precheckGrid(grid)).toMatch(/^Row 1 looks like a title/);
  });

  it('clamps a huge typed range to the used range', () => {
    const grid = readWorksheet(XLSX, syntheticWorkbook(), 'P&L', parseA1Range('A3:XFD1048576'))!;
    expect(formatA1Range(grid.range)).toBe('A3:D5');
  });

  it('returns null for a range with nothing in it', () => {
    expect(readWorksheet(XLSX, syntheticWorkbook(), 'P&L', parseA1Range('H20:J30'))).toBeNull();
  });

  it('refuses a sheet that no longer exists', () => {
    expect(() => readWorksheet(XLSX, syntheticWorkbook(), 'Renamed', null)).toThrow(ExcelFileError);
  });
});

describe('the real Excel-authored fixture (FA Spike Test.xlsx, synthetic data)', () => {
  // Saved by Excel for the web, so number formats, cached formula results, the hidden row and
  // the error cell are exactly what Excel writes -- not what SheetJS writes.
  const bytes = readFileSync(new URL('./fixtures/fa-spike-test.xlsx', import.meta.url));
  const data = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  const raw = XLSX.read(data, { type: 'array', cellNF: true, cellFormula: true }).Sheets['P&L']!;

  it('lists the sheets in workbook order', () => {
    expect(workbookSheetNames(XLSX, data)).toEqual(['Notes', 'P&L']);
  });

  it("sends Excel's built-in Short Date (numFmt 14) as ISO", () => {
    expect((XLSX.SSF as { get_table(): Record<number, string> }).get_table()[14]).toBe('m/d/yy');
    expect((raw.A4 as XLSX.CellObject).z).toBe('m/d/yy');
    const grid = readWorksheet(XLSX, data, 'P&L', parseA1Range('A3:H7'))!;
    expect(grid.rows.slice(1).map((r) => r[0]!.value)).toEqual([
      '2025-03-31',
      '2025-06-30',
      '2025-09-30',
      '2025-12-31',
    ]);
    expect(grid.rows[1]![0]!.display).toBe('3/31/25'); // SheetJS's rendering; Excel shows 3/31/2025
    expect(grid.rows[1]![6]!.value).toBe('2025-04-28'); // the custom d-mmm-yy column
  });

  it('sends the cached results of formula cells, at full precision', () => {
    expect((raw.D4 as XLSX.CellObject).f).toBe('B4-C4');
    expect((raw.E5 as XLSX.CellObject).f).toBe('D5/B5');
    const grid = readWorksheet(XLSX, data, 'P&L', parseA1Range('A3:H7'))!;
    expect(grid.rows.map((r) => r[3]!.value)).toEqual(['Gross Profit', '770000', '805000', '750000', '880000']);
    expect(grid.rows.map((r) => r[4]!.value)).toEqual([
      'Gross Margin',
      '0.616',
      '0.6145038167938931',
      '0.5859375',
      '0.6197183098591549',
    ]);
    expect(grid.rows[2]![4]!.display).toBe('61.5%');
  });

  it('sends the parenthesized negative as -45000', () => {
    const grid = readWorksheet(XLSX, data, 'P&L', parseA1Range('A3:H7'))!;
    expect(grid.rows[3]![5]).toMatchObject({ kind: 'number', value: '-45000', display: '($45,000)' });
  });

  it('notes the hidden row, the #DIV/0! cell, and the empty columns trimmed from a wider range', () => {
    const grid = readWorksheet(XLSX, data, 'P&L', parseA1Range('A3:J7'))!;
    expect(formatA1Range(grid.range)).toBe('A3:H7');
    const byKind = Object.fromEntries(grid.notices.map((x) => [x.kind, x.message]));
    expect(byKind['range-trimmed']).toBe('Columns I–J were empty and were left out.');
    expect(byKind['hidden-rows']).toBe('A hidden row is included (row 6).');
    expect(byKind['formula-errors']).toContain('H4');
  });
});

describe('excelCell', () => {
  it('sends a time of day as displayed rather than as a date', () => {
    expect(excelCell(XLSX, { t: 'n', v: 0.5, z: 'h:mm', w: '12:00' }, false)).toMatchObject({
      kind: 'text',
      value: '12:00',
    });
  });

  it('sends booleans as displayed and treats stubs as empty', () => {
    expect(excelCell(XLSX, { t: 'b', v: true, w: 'TRUE' }, false).value).toBe('TRUE');
    expect(excelCell(XLSX, { t: 'z' }, false).kind).toBe('empty');
    expect(excelCell(XLSX, undefined, false).kind).toBe('empty');
  });
});

describe('Graph metadata, refusal and download', () => {
  const meta: ExcelFileMeta = {
    name: 'FA Spike Test.xlsx',
    size: 20_000,
    lastModified: '2026-09-30T12:17:57Z',
    downloadUrl: 'https://my.microsoftpersonalcontent.com/download?token=secret',
  };

  const DRIVE_TAB = { kind: 'microsoft' as const, driveId: 'D', itemId: 'D!s1' };
  const SHARES_TAB = { kind: 'microsoft' as const, url: 'https://onedrive.live.com/personal/x/doc.aspx' };

  it('requests the whole item (no $select, which drops the download URL), for both URL shapes', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            name: 'FA Spike Test.xlsx',
            size: 20_000,
            lastModifiedDateTime: '2026-09-30T12:17:57Z',
            '@microsoft.graph.downloadUrl': meta.downloadUrl,
          }),
        ),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(fetchExcelFileMeta('tok', DRIVE_TAB)).resolves.toEqual(meta);
    await fetchExcelFileMeta('tok', SHARES_TAB);

    const urls = fetchMock.mock.calls.map((call) => (call as unknown as [string])[0]);
    expect(urls[0]).toBe('https://graph.microsoft.com/v1.0/drives/D/items/D!s1');
    expect(urls[1]).toMatch(/^https:\/\/graph\.microsoft\.com\/v1\.0\/shares\/u![^?]+\/driveItem$/);
  });

  it('reports a missing download URL as null rather than refusing the file', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ name: 'FA Spike Test.xlsx', size: 20_000 }))),
    );
    const noUrl = await fetchExcelFileMeta('tok', DRIVE_TAB);
    expect(noUrl.downloadUrl).toBeNull();
    expect(excelFileRefusal(noUrl)).toBeNull();
  });

  it('refuses non-workbooks and files over 10 MB before downloading', () => {
    expect(excelFileRefusal(meta)).toBeNull();
    expect(excelFileRefusal({ ...meta, name: 'notes.docx' })).toMatch(/isn't an Excel workbook/);
    expect(excelFileRefusal({ ...meta, size: MAX_EXCEL_FILE_BYTES + 1 })).toMatch(/over 10 MB/);
  });

  it('primary path: downloads from the download URL without credentials or an Authorization header', async () => {
    const fetchMock = vi.fn(async () => new Response(new Uint8Array([1, 2, 3])));
    vi.stubGlobal('fetch', fetchMock);
    const data = await downloadWorkbook(meta, 'tok', DRIVE_TAB);
    expect(data.byteLength).toBe(3);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(meta.downloadUrl, { credentials: 'omit' });
  });

  it.each([
    [DRIVE_TAB, 'https://graph.microsoft.com/v1.0/drives/D/items/D!s1/content'],
    [SHARES_TAB, /^https:\/\/graph\.microsoft\.com\/v1\.0\/shares\/u![^?]+\/driveItem\/content$/],
  ])('fallback: with no download URL, GETs .../content with the token and follows the redirect (%#)', async (tab, expected) => {
    const fetchMock = vi.fn(async () => new Response(new Uint8Array([1, 2, 3, 4])));
    vi.stubGlobal('fetch', fetchMock);
    const data = await downloadWorkbook({ ...meta, downloadUrl: null }, 'tok', tab);
    expect(data.byteLength).toBe(4);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    if (typeof expected === 'string') expect(url).toBe(expected);
    else expect(url).toMatch(expected);
    expect(init.redirect).toBe('follow');
    expect(init.credentials).toBe('omit');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
  });

  it('fallback: a failed /content download is reported plainly', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })));
    await expect(downloadWorkbook({ ...meta, downloadUrl: null }, 'tok', DRIVE_TAB)).rejects.toThrow(
      `Couldn't download "FA Spike Test.xlsx" (404). Try again.`,
    );
  });

  it('refuses a download that turns out to be over the cap', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(new Uint8Array(MAX_EXCEL_FILE_BYTES + 1))),
    );
    await expect(downloadWorkbook(meta, 'tok', DRIVE_TAB)).rejects.toThrow(/over 10 MB/);
  });

  it('reports a failed download without the URL', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 403 })));
    const error = await downloadWorkbook(meta, 'tok', DRIVE_TAB).then(
      () => null,
      (e: unknown) => e as Error,
    );
    expect(error).toBeInstanceOf(ExcelFileError);
    expect(error!.message).not.toContain('secret');
  });
});
