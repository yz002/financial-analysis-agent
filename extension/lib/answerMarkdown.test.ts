import { describe, expect, it } from 'vitest';
import { keepsRangesWhole, parseAnswer, parseInline, type Block, type Inline, type Range } from './answerMarkdown';

/** An inline as readable strings: "text", or "bold+italic:text" for a styled run. */
function show(text: string, inline: Inline): string[] {
  return inline.map((run) => {
    const slice = text.slice(run.start, run.end);
    return run.styles.length ? `${run.styles.join('+')}:${slice}` : slice;
  });
}

function inline(text: string, ranges: Range[] = []): string[] {
  return show(text, parseInline(text, 0, text.length, ranges));
}

function kinds(blocks: Block[]): string[] {
  return blocks.map((b) => b.kind);
}

describe('parseInline', () => {
  it('bold, italic and code', () => {
    expect(inline('Revenue was **up** and *steady*, see `B4`.')).toEqual([
      'Revenue was ', 'bold:up', ' and ', 'italic:steady', ', see ', 'code:B4', '.',
    ]);
  });

  it('italic inside bold', () => {
    expect(inline('**very *much* up**')).toEqual(['bold:very ', 'bold+italic:much', 'bold: up']);
  });

  it('code content is not parsed further', () => {
    expect(inline('`**not bold**`')).toEqual(['code:**not bold**']);
  });

  it('an unclosed ** or ` stays literal', () => {
    expect(inline('Revenue **grew strongly')).toEqual(['Revenue **grew strongly']);
    expect(inline('see `B4 for details')).toEqual(['see `B4 for details']);
  });

  it('flanking: "revenue * margin" and "2 * 3" stay as they are', () => {
    expect(inline('revenue * margin')).toEqual(['revenue * margin']);
    expect(inline('2 * 3')).toEqual(['2 * 3']);
    expect(inline('2 * 3 * 4')).toEqual(['2 * 3 * 4']);
    expect(inline('a ** b ** c')).toEqual(['a ** b ** c']);
    expect(inline('**bold **')).toEqual(['**bold **']); // closer after a space
  });

  it('a single * touching a digit or letter outside stays literal: "2*3*4"', () => {
    expect(inline('2*3*4')).toEqual(['2*3*4']);
  });

  it('*** and stray stars stay literal', () => {
    expect(inline('***x***')).toEqual(['***x***']);
  });

  it('links and bare URLs are plain text', () => {
    expect(inline('[x](javascript:alert(1)) and https://example.com')).toEqual([
      '[x](javascript:alert(1)) and https://example.com',
    ]);
  });

  it('a delimiter inside a protected figure is not a delimiter', () => {
    const text = 'Revenue **1.25** million';
    // The figure "1.25** million" covers the closing **: the pair isn't applied.
    const figure: Range = [text.indexOf('1.25'), text.length];
    expect(inline(text, [figure])).toEqual(['Revenue **1.25** million']);
  });

  it('a figure fully inside bold stays inside the bold run', () => {
    const text = 'Revenue was **1.25 million**.';
    const figure: Range = [text.indexOf('1.25'), text.indexOf('1.25') + 12];
    const runs = parseInline(text, 0, text.length, [figure]);
    const bold = runs.find((r) => r.styles.includes('bold'))!;
    expect(text.slice(bold.start, bold.end)).toBe('1.25 million');
    expect(keepsRangesWhole([{ kind: 'paragraph', lines: [runs] }], [figure])).toBe(true);
  });
});

describe('parseAnswer: blocks', () => {
  it('paragraphs split on blank lines; single newlines stay lines of one paragraph', () => {
    const text = 'Line one\nline two\n\nNext paragraph';
    const blocks = parseAnswer(text);
    expect(kinds(blocks)).toEqual(['paragraph', 'paragraph']);
    const first = blocks[0] as Extract<Block, { kind: 'paragraph' }>;
    expect(first.lines.map((l) => show(text, l))).toEqual([['Line one'], ['line two']]);
  });

  it('bullet and numbered lists', () => {
    const text = 'Highlights:\n- Revenue up\n- Costs **down**\n\n1. First\n2. Second';
    const blocks = parseAnswer(text);
    expect(kinds(blocks)).toEqual(['paragraph', 'bullets', 'numbered']);
    const bullets = blocks[1] as Extract<Block, { kind: 'bullets' }>;
    expect(bullets.items.map((i) => show(text, i))).toEqual([['Revenue up'], ['Costs ', 'bold:down']]);
    const numbered = blocks[2] as Extract<Block, { kind: 'numbered' }>;
    expect(numbered.start).toBe(1);
    expect(numbered.items.map((i) => show(text, i))).toEqual([['First'], ['Second']]);
  });

  it('"* " is a bullet, but "*italic*" at the start of a line is not', () => {
    expect(kinds(parseAnswer('* one\n* two'))).toEqual(['bullets']);
    expect(kinds(parseAnswer('*Note:* revenue fell'))).toEqual(['paragraph']);
  });

  it('a line starting with "-4.6%" or "-$120" is not a bullet', () => {
    const text = '-4.6% was the margin\n-$120 was the loss';
    const blocks = parseAnswer(text);
    expect(kinds(blocks)).toEqual(['paragraph']);
    const paragraph = blocks[0] as Extract<Block, { kind: 'paragraph' }>;
    expect(paragraph.lines.map((l) => show(text, l))).toEqual([['-4.6% was the margin'], ['-$120 was the loss']]);
  });

  it('a dash or number with nothing after it is not a list', () => {
    expect(kinds(parseAnswer('-\n1.'))).toEqual(['paragraph']);
  });

  it('a pipe table with a separator row', () => {
    const text = '| Quarter | Revenue |\n|---|---:|\n| Q1 | **1.25 million** |\n| Q2 | 1.31 million |';
    const blocks = parseAnswer(text);
    expect(kinds(blocks)).toEqual(['table']);
    const table = blocks[0] as Extract<Block, { kind: 'table' }>;
    expect(table.header.map((c) => show(text, c))).toEqual([['Quarter'], ['Revenue']]);
    expect(table.rows.map((r) => r.map((c) => show(text, c)))).toEqual([
      [['Q1'], ['bold:1.25 million']],
      [['Q2'], ['1.31 million']],
    ]);
  });

  it('a ragged table: a short row is padded, a long row keeps its extra cells as literal text', () => {
    const text = '| A | B | C |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |';
    const table = parseAnswer(text)[0] as Extract<Block, { kind: 'table' }>;
    expect(table.rows.map((r) => r.map((c) => show(text, c)))).toEqual([
      [['1'], [], []],
      [['1'], ['2'], ['3 | 4']],
    ]);
  });

  it('pipes without a separator row are not a table', () => {
    expect(kinds(parseAnswer('a | b\nc | d'))).toEqual(['paragraph']);
  });

  it('a separator with a different cell count is not a table', () => {
    expect(kinds(parseAnswer('| A | B |\n|---|\n| 1 | 2 |'))).toEqual(['paragraph']);
  });

  it('a row with a pipe inside a protected figure is not read as a row; it stays literal', () => {
    const text = '| A | B |\n|---|---|\n| 1 | 2 |';
    const pipeInFigure: Range = [text.lastIndexOf('|') - 1, text.length];
    const blocks = parseAnswer(text, [pipeInFigure]);
    expect(kinds(blocks)).toEqual(['table', 'paragraph']);
    expect((blocks[0] as Extract<Block, { kind: 'table' }>).rows).toEqual([]);
    expect(keepsRangesWhole(blocks, [pipeInFigure])).toBe(true);
  });

  it('headings and HTML are literal text', () => {
    const text = '# Title\n<b>x</b>';
    const blocks = parseAnswer(text);
    expect(kinds(blocks)).toEqual(['paragraph']);
    const paragraph = blocks[0] as Extract<Block, { kind: 'paragraph' }>;
    expect(paragraph.lines.map((l) => show(text, l))).toEqual([['# Title'], ['<b>x</b>']]);
  });

  it('every character shown comes from the answer itself', () => {
    const text = 'Plain **bold** and `code`\n- item *one*\n\n| H |\n|---|\n| v |';
    const blocks = parseAnswer(text);
    const shown: string[] = [];
    for (const block of blocks) {
      if (block.kind === 'paragraph') block.lines.forEach((l) => l.forEach((r) => shown.push(text.slice(r.start, r.end))));
      else if (block.kind === 'table') [block.header, ...block.rows].forEach((row) => row.forEach((c) => c.forEach((r) => shown.push(text.slice(r.start, r.end)))));
      else block.items.forEach((i) => i.forEach((r) => shown.push(text.slice(r.start, r.end))));
    }
    expect(shown.join('|')).toBe('Plain |bold| and |code|item |one|H|v');
  });
});

describe('keepsRangesWhole', () => {
  it('is false when a figure spans two lines, so the caller falls back to plain text', () => {
    const text = 'Revenue was 1.25\nmillion.';
    const figure: Range = [text.indexOf('1.25'), text.indexOf('million') + 7];
    expect(keepsRangesWhole(parseAnswer(text, [figure]), [figure])).toBe(false);
  });
});
