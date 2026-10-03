import { describe, expect, it } from 'vitest';
import {
  bodySizeError,
  buildParseRequest,
  cellAddress,
  columnLetter,
  EMPTY_CELL,
  formatA1Range,
  isoDate,
  looksLikeDateText,
  MAX_COLUMNS,
  MAX_DATA_ROWS,
  MAX_TOTAL_CELLS,
  normalizeGrid,
  numberCell,
  parseA1Range,
  precheckGrid,
  quoteSheetName,
  textCell,
  type Cell,
  type NormalizedGrid,
  type RawGrid,
} from './cellGrid';

const t = textCell;
const n = (v: number) => numberCell(v, String(v));
const date = (iso: string): Cell => ({ kind: 'date', value: iso, display: iso });

function raw(cells: Cell[][], extra: Partial<RawGrid> = {}): RawGrid {
  return { originRow: 3, originCol: 0, cells, hiddenRows: [], merges: [], ...extra };
}

function grid(cells: Cell[][], extra: Partial<RawGrid> = {}): NormalizedGrid {
  const result = normalizeGrid('P&L', raw(cells, extra));
  if (!result) throw new Error('expected a grid');
  return result;
}

const PNL = [
  [t('Period'), t('Revenue'), t('Margin')],
  [date('2025-03-31'), n(1250000), n(0.6145038167938931)],
  [date('2025-06-30'), n(-45000), n(0.5)],
];

describe('A1 helpers', () => {
  it('converts column indexes to letters and back through ranges', () => {
    expect([0, 25, 26, 51, 701, 702].map(columnLetter)).toEqual(['A', 'Z', 'AA', 'AZ', 'ZZ', 'AAA']);
    expect(parseA1Range(' a3:g7 ')).toEqual({ startRow: 3, startCol: 0, endRow: 7, endCol: 6 });
    expect(formatA1Range(parseA1Range('AA10:AB12')!)).toBe('AA10:AB12');
    expect(cellAddress(4, 1)).toBe('B4');
  });

  it.each(['A3', 'G7:A3', 'A0:B2', "'P&L'!A3:G7", 'A3:G'])('rejects %s', (text) => {
    expect(parseA1Range(text)).toBeNull();
  });

  it('quotes sheet names, doubling embedded quotes', () => {
    expect(quoteSheetName('P&L')).toBe("'P&L'");
    expect(quoteSheetName("Bob's Q1")).toBe("'Bob''s Q1'");
  });

  it('formats ISO dates, adding the time only when it is not midnight', () => {
    expect(isoDate(2025, 3, 31)).toBe('2025-03-31');
    expect(isoDate(2025, 3, 31, 14, 5, 9)).toBe('2025-03-31T14:05:09');
  });
});

describe('numberCell', () => {
  // The backend's numeric cleaner is tested against these exact strings
  // (tests/test_csv_statement.py::test_exponent_form_numbers_parse_exactly).
  it.each([
    [1250000, '1250000'],
    [-45000, '-45000'],
    [0.6145038167938931, '0.6145038167938931'],
    [1e21, '1e+21'],
    [1e-7, '1e-7'],
    [-1e-7, '-1e-7'],
    [1.5e300, '1.5e+300'],
  ])('sends %s as String(value) = %s', (value, sent) => {
    expect(numberCell(value, 'shown').value).toBe(sent);
    expect(numberCell(value, 'shown').value).toBe(String(value));
  });
});

describe('looksLikeDateText', () => {
  it.each(['3/31/2025', '31/03/2025', '31.03.2025', '28-Apr-25', 'Mar 2025', 'March 31, 2025', '31 Mar 2025'])(
    'flags %s',
    (text) => expect(looksLikeDateText(text)).toBe(true),
  );
  it.each(['2025-03-31', 'Revenue', '1250000', 'Q1 2025', 'Margin %'])('does not flag %s', (text) =>
    expect(looksLikeDateText(text)).toBe(false),
  );
});

describe('normalizeGrid', () => {
  it('pads ragged rows and trims trailing empty rows and columns, keeping the origin', () => {
    const g = grid([
      [t('Period'), t('Revenue')],
      [date('2025-03-31')],
      [EMPTY_CELL, EMPTY_CELL, EMPTY_CELL],
      [],
    ]);
    expect(g.rows).toEqual([
      [t('Period'), t('Revenue')],
      [date('2025-03-31'), EMPTY_CELL],
    ]);
    expect(formatA1Range(g.range)).toBe('A3:B4');
  });

  it('keeps interior blank rows so every cell keeps its address', () => {
    const g = grid([[t('Period'), t('Revenue')], [EMPTY_CELL, EMPTY_CELL], [date('2025-06-30'), n(1)]]);
    expect(g.rows).toHaveLength(3);
    expect(formatA1Range(g.range)).toBe('A3:B5');
  });

  it('never shrinks a requested range silently', () => {
    const cells = [
      [t('Period'), t('Revenue'), EMPTY_CELL],
      [date('2025-03-31'), n(1), EMPTY_CELL],
      [EMPTY_CELL, EMPTY_CELL, EMPTY_CELL],
    ];
    const notice = (requested: string) =>
      grid(cells, { requested: parseA1Range(requested)! }).notices.find((x) => x.kind === 'range-trimmed')
        ?.message;

    expect(notice('A3:C4')).toBe('Column C was empty and was left out.');
    expect(notice('A3:E4')).toBe('Columns C–E were empty and were left out.');
    expect(notice('A3:B5')).toBe('Row 5 was empty and was left out.');
    expect(notice('A3:C7')).toBe(
      'Column C was empty and was left out. Rows 5–7 were empty and were left out.',
    );
    expect(notice('A3:B4')).toBeUndefined(); // nothing trimmed
    expect(grid(cells).notices.map((x) => x.kind)).not.toContain('range-trimmed'); // no range asked for
  });

  it('returns null when nothing non-empty is left', () => {
    expect(normalizeGrid('P&L', raw([[EMPTY_CELL], []]))).toBeNull();
    expect(normalizeGrid('P&L', raw([]))).toBeNull();
  });

  it('notes hidden rows, merges, formula errors and date-like text, by address', () => {
    const g = grid(
      [
        [t('Period'), t('Revenue'), t('Margin')],
        [t('3/31/2025'), n(1), { kind: 'error', value: '#DIV/0!', display: '#DIV/0!' }],
        [date('2025-06-30'), n(2), n(3)],
      ],
      {
        hiddenRows: [2, 5],
        merges: [
          { startRow: 1, startCol: 0, endRow: 1, endCol: 6 }, // a title merge above the range
          { startRow: 4, startCol: 1, endRow: 5, endCol: 1 },
        ],
      },
    );
    const byKind = Object.fromEntries(g.notices.map((x) => [x.kind, x.message]));
    expect(byKind['hidden-rows']).toBe('A hidden row is included (row 5).');
    expect(byKind['merged-cells']).toContain('B4:B5');
    expect(byKind['merged-cells']).not.toContain('A1:G1');
    expect(byKind['formula-errors']).toContain('C4');
    expect(byKind['text-dates']).toContain('day/month order may be misread: A4');
    expect(byKind['periods-across-columns']).toBeUndefined();
  });

  it('warns when periods run across the header row', () => {
    const g = grid([
      [t('Line item'), date('2025-03-31'), t('6/30/2025')],
      [t('Revenue'), n(1), n(2)],
    ]);
    expect(g.notices.map((x) => x.kind)).toContain('periods-across-columns');
  });
});

describe('precheckGrid', () => {
  it('passes a well-formed grid', () => {
    expect(precheckGrid(grid(PNL))).toBeNull();
  });

  it('names the empty header cell', () => {
    const g = grid([
      [t('Period'), EMPTY_CELL, t('Margin')],
      [date('2025-03-31'), n(1), n(2)],
    ]);
    expect(precheckGrid(g)).toMatch(/^Header cell B3 is empty/);
  });

  it('recognizes a title row picked up as the header', () => {
    const g = grid([
      [t('Acme Co — Quarterly P&L'), EMPTY_CELL, EMPTY_CELL],
      [t('Period'), t('Revenue'), t('Margin')],
    ]);
    expect(precheckGrid(g)).toMatch(/^Row 3 looks like a title/);
  });

  it('names duplicate header columns', () => {
    const g = grid([
      [t('Period'), t('Revenue'), t('Revenue')],
      [date('2025-03-31'), n(1), n(2)],
    ]);
    expect(precheckGrid(g)).toBe(
      'Columns B and C both have the header "Revenue". Each column needs a distinct header.',
    );
  });

  it('refuses a header with no data rows', () => {
    expect(precheckGrid(grid([[t('Period'), t('Revenue')]]))).toMatch(/no data rows/);
  });

  it('enforces the column, row, total-cell and cell-length caps', () => {
    const wide = [Array.from({ length: MAX_COLUMNS + 1 }, (_, i) => t(`c${i}`)), [n(1)]];
    expect(precheckGrid(grid(wide))).toMatch(`limit is ${MAX_COLUMNS}`);

    const tall = [[t('Period')], ...Array.from({ length: MAX_DATA_ROWS + 1 }, () => [n(1)])];
    expect(precheckGrid(grid(tall))).toMatch(`limit is ${MAX_DATA_ROWS}`);

    const columns = 100;
    const header = Array.from({ length: columns }, (_, i) => t(`c${i}`));
    const body = () => header.map(() => n(1));
    const atCap = [header, ...Array.from({ length: MAX_TOTAL_CELLS / columns - 1 }, body)];
    expect(precheckGrid(grid(atCap))).toBeNull();
    expect(precheckGrid(grid([...atCap, body()]))).toMatch(`limit is ${MAX_TOTAL_CELLS}`);

    const long = [[t('Note')], [t('x'.repeat(1001))]];
    expect(precheckGrid(grid(long))).toBe('Cell A4 is longer than 1000 characters.');
  });
});

describe('buildParseRequest', () => {
  it('sends values (never display text) with the sheet source', () => {
    const request = buildParseRequest(grid(PNL), {
      name: 'FA Spike Test.xlsx',
      platform: 'excel',
      modifiedAt: '2026-09-30T12:17:57Z',
    });
    expect(request).toEqual({
      rows: [
        ['Period', 'Revenue', 'Margin'],
        ['2025-03-31', '1250000', '0.6145038167938931'],
        ['2025-06-30', '-45000', '0.5'],
      ],
      filename: 'FA Spike Test.xlsx — P&L',
      source: {
        platform: 'excel',
        sheet_name: 'P&L',
        range: 'A3:C5',
        file_name: 'FA Spike Test.xlsx',
        modified_at: '2026-09-30T12:17:57Z',
      },
    });
    expect(bodySizeError(request)).toBeNull();
  });

  it('measures the body in UTF-8 bytes against the 2 MB cap', () => {
    const request = buildParseRequest(grid(PNL), { name: 'f', platform: 'google_sheets', modifiedAt: null });
    request.rows.push(['é'.repeat(1_100_000)]); // 2 bytes each in UTF-8, 1 UTF-16 unit each
    expect(bodySizeError(request)).toMatch(/limit is 2 MB/);
  });
});
