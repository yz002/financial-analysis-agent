import { afterEach, describe, expect, it, vi } from 'vitest';
import * as XLSX from 'xlsx';
import { ExcelFileError } from './excelReader';
import { connectExcelFile, connectGoogleSheet } from './spreadsheetSource';

afterEach(() => {
  vi.unstubAllGlobals();
});

const SHEET_INFO = {
  properties: { title: 'FA Spike Test' },
  sheets: [
    { properties: { sheetId: 0, title: 'Summary' } },
    { properties: { sheetId: 42, title: 'P&L' } },
  ],
};

function stubJson(body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body))));
}

describe('connectGoogleSheet', () => {
  it('defaults to the live gid sheet and pre-fills its range= link', async () => {
    stubJson(SHEET_INFO);
    const file = await connectGoogleSheet(
      'tok',
      'abc',
      'https://docs.google.com/spreadsheets/d/abc/edit#gid=42&range=A3:G7',
    );
    expect(file).toMatchObject({
      provider: 'google',
      platform: 'google_sheets',
      name: 'FA Spike Test',
      modifiedAt: null,
      sheets: ['Summary', 'P&L'],
      defaultSheet: 'P&L',
      defaultRange: 'A3:G7',
    });
  });

  it("doesn't pre-fill a range when the gid matches no sheet", async () => {
    stubJson(SHEET_INFO);
    const file = await connectGoogleSheet(
      'tok',
      'abc',
      'https://docs.google.com/spreadsheets/d/abc/edit#gid=999&range=A3:G7',
    );
    expect(file.defaultSheet).toBe('Summary');
    expect(file.defaultRange).toBeNull();
  });
});

describe('connectExcelFile', () => {
  const tab = { kind: 'microsoft' as const, driveId: 'D', itemId: 'D!s1' };

  it('refuses an oversize file without downloading it', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            name: 'big.xlsx',
            size: 50 * 1024 * 1024,
            '@microsoft.graph.downloadUrl': 'https://my.microsoftpersonalcontent.com/x',
          }),
        ),
    );
    vi.stubGlobal('fetch', fetchMock);
    await expect(connectExcelFile('tok', tab)).rejects.toThrow(ExcelFileError);
    expect(fetchMock).toHaveBeenCalledTimes(1); // metadata only
  });

  it('downloads once, lists the sheets, and keeps no download URL', async () => {
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([['Period'], ['x']]), 'P&L');
    const bytes = XLSX.write(workbook, { type: 'array', bookType: 'xlsx' }) as ArrayBuffer;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            name: 'FA Spike Test.xlsx',
            size: bytes.byteLength,
            lastModifiedDateTime: '2026-09-30T12:17:57Z',
            '@microsoft.graph.downloadUrl': 'https://my.microsoftpersonalcontent.com/secret',
          }),
        ),
      )
      .mockResolvedValueOnce(new Response(bytes));
    vi.stubGlobal('fetch', fetchMock);

    const file = await connectExcelFile('tok', tab);

    expect(file).toMatchObject({
      provider: 'microsoft',
      platform: 'excel',
      name: 'FA Spike Test.xlsx',
      modifiedAt: '2026-09-30T12:17:57Z',
      sheets: ['P&L'],
      defaultSheet: 'P&L',
      defaultRange: null,
    });
    expect(JSON.stringify(file)).not.toContain('secret');
    await file.read('P&L', null, null);
    expect(fetchMock).toHaveBeenCalledTimes(2); // reading again doesn't download again
  });
});
