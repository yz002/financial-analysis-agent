import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatA1Range, parseA1Range } from './cellGrid';
import { ProviderApiError } from './connectData';
import {
  fetchSpreadsheetInfo,
  googleCell,
  parseSheetsLocation,
  readSheetGrid,
  sheetsSerialToIso,
  type GoogleCellData,
} from './sheetsReader';

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const firstUrl = (fetchMock: ReturnType<typeof stubFetch>) =>
  new URL((fetchMock.mock.calls[0] as unknown as [string])[0]);

const num = (numberValue: number, formattedValue?: string, type?: string): GoogleCellData => ({
  effectiveValue: { numberValue },
  ...(formattedValue === undefined ? {} : { formattedValue }),
  ...(type ? { effectiveFormat: { numberFormat: { type } } } : {}),
});
const str = (s: string): GoogleCellData => ({ effectiveValue: { stringValue: s }, formattedValue: s });

describe('googleCell: numeric when numberValue is present and the format is not DATE/DATE_TIME/TIME', () => {
  it('sends the full-precision String(numberValue) for an unformatted number whose formattedValue is truncated', () => {
    // Live shape from the FA Spike Test sheet (E5): no numberFormat at all, truncated display.
    const cell = googleCell(num(0.6145038167938931, '0.6145038168'));
    expect(cell).toEqual({ kind: 'number', value: '0.6145038167938931', display: '0.6145038168' });
  });

  it.each(['NUMBER', 'CURRENCY', 'PERCENT', 'SCIENTIFIC', 'TEXT'])(
    'treats %s-formatted numbers as numbers too, sending the underlying value',
    (type) => {
      expect(googleCell(num(-45000, '($45,000)', type)).value).toBe('-45000');
    },
  );

  it('uses the exponent form String() produces at the extremes', () => {
    expect(googleCell(num(1e21, '1E+21')).value).toBe('1e+21');
    expect(googleCell(num(1e-7, '0.0000001')).value).toBe('1e-7');
  });
});

describe('googleCell: the format type only decides DATE/DATE_TIME/TIME', () => {
  it('converts a DATE serial to ISO', () => {
    expect(googleCell(num(45747, '3/31/2025', 'DATE'))).toEqual({
      kind: 'date',
      value: '2025-03-31',
      display: '3/31/2025',
    });
  });

  it('keeps a DATE_TIME time of day', () => {
    expect(googleCell(num(45747.75, '3/31/2025 18:00:00', 'DATE_TIME')).value).toBe(
      '2025-03-31T18:00:00',
    );
  });

  it('sends a TIME as displayed', () => {
    expect(googleCell(num(0.5, '12:00:00 PM', 'TIME'))).toMatchObject({
      kind: 'text',
      value: '12:00:00 PM',
    });
  });

  it('sends a date-looking string as text (flagged later by a notice, never parsed)', () => {
    expect(googleCell(str('31/03/2025'))).toMatchObject({ kind: 'text', value: '31/03/2025' });
  });
});

describe('googleCell: everything else', () => {
  it('sends booleans and strings as displayed', () => {
    expect(googleCell({ effectiveValue: { boolValue: true }, formattedValue: 'TRUE' }).value).toBe('TRUE');
    expect(googleCell(str('Revenue')).value).toBe('Revenue');
  });

  it('sends an error as its literal', () => {
    expect(
      googleCell({ effectiveValue: { errorValue: { type: 'DIVIDE_BY_ZERO' } }, formattedValue: '#DIV/0!' }),
    ).toMatchObject({ kind: 'error', value: '#DIV/0!' });
    expect(googleCell({ effectiveValue: { errorValue: { type: 'REF' } } }).value).toBe('#REF!');
  });

  it('treats a cell with no effectiveValue as empty', () => {
    expect(googleCell(undefined).kind).toBe('empty');
    expect(googleCell({ formattedValue: '' }).kind).toBe('empty');
  });
});

describe('sheetsSerialToIso', () => {
  it('counts from 1899-12-30 without a timezone shift', () => {
    expect(sheetsSerialToIso(1)).toBe('1899-12-31');
    expect(sheetsSerialToIso(45838)).toBe('2025-06-30');
    expect(sheetsSerialToIso(45747.999999999)).toBe('2025-04-01'); // rounds to the whole second
  });
});

describe('parseSheetsLocation', () => {
  it('reads the live gid and a range= link from the hash', () => {
    expect(
      parseSheetsLocation('https://docs.google.com/spreadsheets/d/abc/edit?gid=0#gid=123&range=A3:G7'),
    ).toEqual({ gid: 123, range: 'A3:G7' });
  });

  it('falls back to the query gid and ignores a single-cell range', () => {
    expect(
      parseSheetsLocation('https://docs.google.com/spreadsheets/d/abc/edit?gid=7#range=B4'),
    ).toEqual({ gid: 7, range: null });
  });
});

describe('fetchSpreadsheetInfo', () => {
  it('lists sheet titles with their ids', async () => {
    const fetchMock = stubFetch({
      properties: { title: 'FA Spike Test' },
      sheets: [
        { properties: { sheetId: 0, title: 'P&L' } },
        { properties: { sheetId: 99, title: 'Notes' } },
      ],
    });
    await expect(fetchSpreadsheetInfo('tok', 'abc')).resolves.toEqual({
      title: 'FA Spike Test',
      sheets: [
        { sheetId: 0, title: 'P&L' },
        { sheetId: 99, title: 'Notes' },
      ],
    });
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(firstUrl(fetchMock).pathname).toBe('/v4/spreadsheets/abc');
    expect(init.headers).toEqual({ Authorization: 'Bearer tok' });
  });
});

describe('readSheetGrid', () => {
  // Shaped like the live risk-check response: dates carry DATE, plain numbers carry no format,
  // trailing empty cells are omitted, so rows come back ragged.
  const response = {
    sheets: [
      {
        merges: [{ startRowIndex: 0, endRowIndex: 1, startColumnIndex: 0, endColumnIndex: 7 }],
        data: [
          {
            startRow: 2,
            startColumn: 0,
            rowMetadata: [{}, {}, { hiddenByUser: true }],
            rowData: [
              { values: [str('Period'), str('Revenue'), str('Margin')] },
              {
                values: [
                  num(45747, '3/31/2025', 'DATE'),
                  num(1250000, '$1,250,000', 'CURRENCY'),
                  num(0.6145038167938931, '0.6145038168'),
                ],
              },
              { values: [num(45838, '6/30/2025', 'DATE'), num(-45000, '($45,000)')] },
              {},
            ],
          },
        ],
      },
    ],
  };

  it('requests the quoted sheet and range with the grid field mask', async () => {
    const fetchMock = stubFetch(response);
    await readSheetGrid('tok', 'abc', "Bob's P&L", parseA1Range('A3:C6'));
    expect(firstUrl(fetchMock).searchParams.get('ranges')).toBe("'Bob''s P&L'!A3:C6");
    expect(firstUrl(fetchMock).searchParams.get('fields')).toContain(
      'effectiveFormat/numberFormat/type',
    );
  });

  it('pads, trims, keeps the origin and notes hidden rows', async () => {
    stubFetch(response);
    const grid = (await readSheetGrid('tok', 'abc', 'P&L', parseA1Range('A3:C6')))!;
    expect(formatA1Range(grid.range)).toBe('A3:C5');
    expect(grid.rows.map((r) => r.map((c) => c.value))).toEqual([
      ['Period', 'Revenue', 'Margin'],
      ['2025-03-31', '1250000', '0.6145038167938931'],
      ['2025-06-30', '-45000', ''],
    ]);
    // Row 5 is hidden; the title merge sits above A3, so it isn't mentioned.
    expect(grid.notices.map((x) => x.kind)).toEqual(['hidden-rows']);
  });

  it('reads the whole used range from A1 when no range is given', async () => {
    const fetchMock = stubFetch({
      sheets: [{ data: [{ rowData: [{ values: [str('Period')] }, { values: [num(1)] }] }] }],
    });
    const grid = (await readSheetGrid('tok', 'abc', 'P&L', null))!;
    expect(firstUrl(fetchMock).searchParams.get('ranges')).toBe("'P&L'");
    expect(formatA1Range(grid.range)).toBe('A1:A2');
  });

  it('returns null for an empty sheet', async () => {
    stubFetch({ sheets: [{ data: [{}] }] });
    await expect(readSheetGrid('tok', 'abc', 'P&L', null)).resolves.toBeNull();
  });

  it('raises ProviderApiError with the status on failure', async () => {
    stubFetch({ error: {} }, 403);
    await expect(readSheetGrid('tok', 'abc', 'P&L', null)).rejects.toEqual(new ProviderApiError(403));
  });
});
