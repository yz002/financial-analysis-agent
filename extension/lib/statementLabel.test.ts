import { describe, expect, it } from 'vitest';
import type { ActiveStatement } from './sessionStorage';
import { describeStatement, periodsText, statementSource } from './statementLabel';

const BASE: ActiveStatement = {
  csvContextId: 'ctx-1',
  entityName: 'FA Spike Test',
  label: "FA Spike Test · 'P&L'!A3:G7",
  confirmedAt: '2026-10-05T14:03:00Z',
  cadence: 'quarterly',
  scale: 'thousands',
  currency: null,
};

describe('statementSource', () => {
  it("doesn't repeat a business name that's also the file name", () => {
    expect(statementSource(BASE)).toBe("FA Spike Test · 'P&L'!A3:G7");
  });

  it("doesn't repeat a name pre-filled without the file's extension", () => {
    const statement = { ...BASE, label: "FA Spike Test.xlsx · 'P&L'!A3:G7" };
    expect(statementSource(statement)).toBe("FA Spike Test.xlsx · 'P&L'!A3:G7");
  });

  it('keeps a different business name in front of the file and range', () => {
    expect(statementSource({ ...BASE, entityName: 'Acme' })).toBe("Acme — FA Spike Test · 'P&L'!A3:G7");
  });
});

describe('periodsText', () => {
  it("uses the backend's count and cadence verbatim", () => {
    expect(periodsText('quarterly', 4)).toBe('4 quarterly periods');
    expect(periodsText('annual', 5)).toBe('5 annual periods');
  });

  it('no cadence: just the count, and "1 period" for one', () => {
    expect(periodsText(null, 1)).toBe('1 period');
    expect(periodsText(null, 3)).toBe('3 periods');
  });

  it('a statement stored before period_count keeps the old wording', () => {
    expect(periodsText('quarterly', undefined)).toBe('quarterly periods');
    expect(periodsText(null, undefined)).toBe('a single period');
    expect(periodsText('annual', null)).toBe('annual periods');
  });
});

describe('describeStatement', () => {
  it('adds the scale and when it was confirmed', () => {
    const when = new Date(BASE.confirmedAt).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    expect(describeStatement(BASE)).toBe(`FA Spike Test · 'P&L'!A3:G7 · numbers in thousands · confirmed ${when}`);
  });

  it.each([
    ['ones', 'numbers in ones (as shown)'],
    ['thousands', 'numbers in thousands'],
    ['millions', 'numbers in millions'],
    ['billions', 'numbers in billions'],
  ])('names the %s scale', (scale, text) => {
    expect(describeStatement({ ...BASE, scale })).toContain(text);
  });

  it('tells apart two statements from the same range that differ only by scale', () => {
    expect(describeStatement({ ...BASE, scale: 'thousands' })).not.toBe(describeStatement({ ...BASE, scale: 'millions' }));
  });

  it('omits the confirmed time when it is missing or invalid, never "Invalid Date"', () => {
    for (const confirmedAt of ['', 'not a date', undefined as unknown as string]) {
      const text = describeStatement({ ...BASE, confirmedAt });
      expect(text).toBe("FA Spike Test · 'P&L'!A3:G7 · numbers in thousands");
      expect(text).not.toContain('Invalid');
    }
  });
});
