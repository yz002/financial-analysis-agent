import { describe, expect, it } from 'vitest';
import type { ActiveStatement } from './sessionStorage';
import { describeStatement, statementSource } from './statementLabel';

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

  it('keeps a different business name in front of the file and range', () => {
    expect(statementSource({ ...BASE, entityName: 'Acme' })).toBe("Acme — FA Spike Test · 'P&L'!A3:G7");
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
