import { describe, expect, it } from 'vitest';
import type { Citation } from './backendApi';
import {
  AMBIGUOUS_LABEL,
  describeCitation,
  failureMessage,
  figureCheckBanner,
  NOT_TRACED_LABEL,
  segmentAnswer,
} from './chatModel';

const ANSWER = 'Revenue was 1.25 million and gross margin was 40.0%.';

function citation(overrides: Partial<Citation>): Citation {
  return { figure_index: 0, raw_text: '', start: 0, end: 0, status: 'traced', matches: [], ...overrides };
}

const REVENUE = citation({
  figure_index: 0,
  raw_text: '1.25 million',
  start: 12,
  end: 24,
  matches: [
    {
      kind: 'cell',
      value: 1250000,
      turn_id: null,
      source: {
        cell: "'P&L'!B4", concept: 'revenue', column: 'Total Revenue', period_end: '2025-03-31',
        read_value: '1250', sheet_scale: 'thousands',
      },
    },
  ],
});

const MARGIN = citation({
  figure_index: 1,
  raw_text: '40.0%',
  start: 46,
  end: 51,
  matches: [
    {
      kind: 'derived',
      value: 0.4,
      turn_id: null,
      computation: {
        name: 'gross_margin',
        formula: 'gross_profit / revenue',
        period_end: '2025-03-31',
        inputs: [
          { role: 'gross_profit', value: 500000, source: { cell: "'P&L'!C4", concept: 'gross_profit', column: 'GP', period_end: '2025-03-31', read_value: '500', sheet_scale: 'thousands' } },
          { role: 'revenue', value: 1250000, source: { cell: "'P&L'!B4", concept: 'revenue', column: 'Total Revenue', period_end: '2025-03-31', read_value: '1250', sheet_scale: 'thousands' } },
        ],
      },
    },
  ],
});

describe('segmentAnswer', () => {
  it('splits the answer into text and cited figures at the citation offsets', () => {
    expect(segmentAnswer(ANSWER, [MARGIN, REVENUE])).toEqual([
      { kind: 'text', text: 'Revenue was ' },
      { kind: 'figure', text: '1.25 million', citation: REVENUE },
      { kind: 'text', text: ' and gross margin was ' },
      { kind: 'figure', text: '40.0%', citation: MARGIN },
      { kind: 'text', text: '.' },
    ]);
  });

  it('leaves a citation whose offsets do not match its text as plain text', () => {
    const wrong = { ...REVENUE, start: 0, end: 12 };
    expect(segmentAnswer(ANSWER, [wrong])).toEqual([{ kind: 'text', text: ANSWER }]);
  });

  it('ignores out-of-range and overlapping citations', () => {
    const outOfRange = { ...REVENUE, start: 40, end: 400 };
    const overlapping = { ...REVENUE, figure_index: 2, raw_text: '25 million', start: 14, end: 24 };
    const segments = segmentAnswer(ANSWER, [REVENUE, overlapping, outOfRange]);
    expect(segments.filter((s) => s.kind === 'figure')).toHaveLength(1);
    expect(segments.map((s) => s.text).join('')).toBe(ANSWER);
  });
});

describe('describeCitation', () => {
  it('a direct value names its cell and quotes the value as read, with the scale', () => {
    const view = describeCitation(REVENUE);
    expect(view.statusLabel).toBeNull();
    expect(view.lines).toEqual([
      {
        ref: "'P&L'!B4",
        text: '= 1250 (as read, in thousands) · Revenue, period ending 2025-03-31',
        children: [],
      },
    ]);
  });

  it('a value in ones has no scale note', () => {
    const ones = structuredClone(REVENUE);
    ones.matches[0]!.source!.sheet_scale = 'ones';
    ones.matches[0]!.source!.read_value = '1250000';
    expect(describeCitation(ones).lines[0]!.text).toBe('= 1250000 (as read) · Revenue, period ending 2025-03-31');
  });

  it('a derived value shows its formula and every input cell, never a single cell', () => {
    const [line] = describeCitation(MARGIN).lines;
    expect(line!.ref).toBeNull();
    expect(line!.text).toBe('Gross margin, period ending 2025-03-31 = gross_profit / revenue');
    expect(line!.children.map((c) => [c.ref, c.text])).toEqual([
      ["'P&L'!C4", '= 500 (as read, in thousands) · Gross profit, period ending 2025-03-31'],
      ["'P&L'!B4", '= 1250 (as read, in thousands) · Revenue, period ending 2025-03-31'],
    ]);
  });

  it('a growth rate names the prior period as its own input', () => {
    const growth = citation({
      raw_text: '4.0%',
      matches: [{
        kind: 'derived', value: 0.04, turn_id: null,
        computation: {
          name: 'revenue_growth_qoq', formula: '(revenue - revenue_prior) / revenue_prior', period_end: '2025-06-30',
          inputs: [
            { role: 'revenue', value: 1300000, source: { cell: "'P&L'!B5", concept: 'revenue', period_end: '2025-06-30', read_value: '1300', sheet_scale: 'ones' } },
            { role: 'revenue_prior', value: 1250000, source: { cell: "'P&L'!B4", concept: 'revenue', period_end: '2025-03-31', read_value: '1250', sheet_scale: 'ones' } },
          ],
        },
      }],
    });
    const children = describeCitation(growth).lines[0]!.children;
    expect(children[1]).toEqual({
      ref: "'P&L'!B4",
      text: '= 1250 (as read) · Revenue, prior period, period ending 2025-03-31',
      children: [],
    });
  });

  it('a trailing-twelve-month input nests its four quarters', () => {
    const ttm = citation({
      raw_text: '410,000',
      matches: [{
        kind: 'derived', value: 410000, turn_id: null,
        computation: {
          name: 'net_income_ttm', formula: 'sum of net_income for this quarter and the 3 quarters before it',
          period_end: '2024-12-31',
          inputs: ['D4', 'D5', 'D6', 'D7'].map((c) => ({
            role: 'net_income', value: null,
            source: { cell: `'P&L'!${c}`, concept: 'net_income', period_end: null, read_value: '1', sheet_scale: 'ones' },
          })),
        },
      }],
    });
    const [line] = describeCitation(ttm).lines;
    expect(line!.text).toContain('Net income, last four quarters');
    expect(line!.children.map((c) => c.ref)).toEqual(["'P&L'!D4", "'P&L'!D5", "'P&L'!D6", "'P&L'!D7"]);
  });

  it('an ambiguous figure lists every place it matches and says so', () => {
    const two = citation({
      raw_text: '1.3 million',
      status: 'ambiguous',
      matches: ['B5', 'B6'].map((c) => ({
        kind: 'cell' as const, value: 1300000, turn_id: null,
        source: { cell: `'P&L'!${c}`, concept: 'revenue', period_end: null, read_value: '1300', sheet_scale: 'thousands' },
      })),
    });
    const view = describeCitation(two);
    expect(view.statusLabel).toBe(AMBIGUOUS_LABEL);
    expect(view.lines.map((l) => l.ref)).toEqual(["'P&L'!B5", "'P&L'!B6"]);
  });

  it('an untraced or weak figure is marked "Not traced to your data"', () => {
    expect(describeCitation(citation({ status: 'untraced' })).statusLabel).toBe(NOT_TRACED_LABEL);
    expect(describeCitation(citation({ status: 'weak' })).statusLabel).toBe(NOT_TRACED_LABEL);
    expect(NOT_TRACED_LABEL).toBe('Not traced to your data');
  });

  it('a filing fact, a projection, and an earlier turn are each named plainly', () => {
    const filing = describeCitation(citation({
      matches: [{
        kind: 'filing', value: 1, turn_id: 'turn-1',
        source: { ticker: 'MSFT', concept: 'revenue', tag: 'Revenues', filed: '2025-07-30', period_end: '2025-06-30', is_derived: false },
      }],
    }));
    expect(filing.lines[0]!.text).toBe(
      'MSFT Revenue, period ending 2025-06-30 · SEC filing, tag Revenues, filed 2025-07-30 (from an earlier answer)',
    );
    const projection = describeCitation(citation({
      matches: [{ kind: 'tool', value: 1, turn_id: null, source: { tool_name: 'forecast_metric', json_path: 'x' } }],
    }));
    expect(projection.lines[0]!.text).toBe('A projection from forecast_metric, not a filed or entered figure');
  });
});

describe('figureCheckBanner', () => {
  const traced = { ...REVENUE };
  const untraced = citation({ raw_text: '9.99 million', status: 'untraced' });

  it('says plainly when every figure was found', () => {
    expect(figureCheckBanner({ figures: [{} as never] }, [traced])).toEqual({
      tone: 'ok',
      text: 'Every figure in this answer was found in your data or the sources used.',
    });
  });

  it('says how many figures were not found, and how they are marked', () => {
    const banner = figureCheckBanner({ figures: [{} as never, {} as never] }, [traced, untraced]);
    expect(banner.tone).toBe('warn');
    expect(banner.text).toBe(
      "1 of 2 figures couldn't be found in your data or the sources used. They're marked " +
        '“Not traced to your data”. Check them before relying on them.',
    );
  });

  it('mentions figures that match several places, and ones that could not be checked', () => {
    const ambiguous = citation({ status: 'ambiguous' });
    const banner = figureCheckBanner(
      { figures: [{} as never, {} as never], figures_skipped: [{ raw_text: '1.25M', start: 0, end: 5, reason: 'bare_scale_suffix' }] },
      [traced, ambiguous],
    );
    expect(banner.text).toContain('1 figure matches more than one place; every place is listed.');
    expect(banner.text).toContain("1 figure couldn't be checked because of how it was written.");
    expect(banner.tone).toBe('warn');
  });

  it('an answer with no figures says so', () => {
    expect(figureCheckBanner({ figures: [] }, [])).toEqual({ tone: 'none', text: 'This answer has no figures to check.' });
  });
});

describe('failureMessage', () => {
  it('words every outcome plainly', () => {
    expect(failureMessage({ kind: 'still_running' })).toBe('Still not finished — check back later.');
    expect(failureMessage({ kind: 'time_budget' })).toContain('took too long');
    expect(failureMessage({ kind: 'answer_lost' })).toBe("That question didn't finish. Ask it again.");
    expect(failureMessage({ kind: 'cap', error: 'daily_cap_reached', resetsAt: null })).toBe("You've used today's questions.");
    expect(failureMessage({ kind: 'cap', error: 'monthly_cap_reached', resetsAt: null })).toBe(
      "You've used all the questions for this billing period.",
    );
    expect(failureMessage({ kind: 'invalid_question' })).toContain('4,000 characters');
  });
});
