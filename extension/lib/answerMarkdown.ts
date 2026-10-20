/**
 * A deliberately small markdown reader for answer text (Phase D session 7, decision B1(a)).
 * Pure and DOM-free: it only says which ranges of the answer string are paragraphs, list items,
 * table cells, bold, italic or code. entrypoints/sidepanel/chat.ts builds the elements with
 * createElement and text nodes only; nothing here or there produces HTML from a string.
 *
 * Every run is a [start, end) range into the original answer, so the text shown is always the
 * answer's own characters, and a figure's citation offsets (segmentAnswer) still line up.
 *
 * Supported: paragraphs (a single newline is a line break), **bold**, *italic*, `code`, "- " or
 * "* " bullets, "1. " numbered lists, and pipe tables with a separator row. Anything else stays
 * literal text: links (never an <a>), headings, HTML, nested lists. When in doubt, literal:
 * - a delimiter that falls inside a protected range (a cited figure) isn't one;
 * - emphasis needs a non-space after the opener and before the closer, so "2 * 3" stays as is;
 * - a single * also can't touch a letter or digit on its outer side, so "2*3*4" stays as is;
 * - a list marker needs a space after it, so "-4.6%" at the start of a line isn't a bullet;
 * - an unclosed ** or ` stays literal.
 */

export type InlineStyle = 'bold' | 'italic' | 'code';

/** A range of the answer, shown with these styles (outermost first). */
export interface Run {
  start: number;
  end: number;
  styles: InlineStyle[];
}

/** One line of a paragraph, one list item or one table cell. */
export type Inline = Run[];

export type Block =
  | { kind: 'paragraph'; lines: Inline[] }
  | { kind: 'bullets'; items: Inline[] }
  | { kind: 'numbered'; start: number; items: Inline[] }
  | { kind: 'table'; header: Inline[]; rows: Inline[][] };

export type Range = readonly [number, number];

interface Line {
  start: number;
  end: number; // exclusive, before the newline
}

const BULLET = /^ {0,3}([-*]) +(?=\S)/;
const NUMBERED = /^ {0,3}(\d{1,9})[.)] +(?=\S)/;
const SEPARATOR_CELL = /^\s*:?-{3,}:?\s*$/;

function splitLines(text: string): Line[] {
  const lines: Line[] = [];
  let start = 0;
  for (;;) {
    const newline = text.indexOf('\n', start);
    if (newline === -1) {
      lines.push({ start, end: text.length });
      return lines;
    }
    lines.push({ start, end: newline });
    start = newline + 1;
  }
}

function isProtected(position: number, protectedRanges: readonly Range[]): boolean {
  return protectedRanges.some(([start, end]) => position >= start && position < end);
}

function anyProtected(from: number, to: number, protectedRanges: readonly Range[]): boolean {
  return protectedRanges.some(([start, end]) => start < to && end > from);
}

const isSpace = (ch: string | undefined) => ch === undefined || /\s/.test(ch);
const isWordChar = (ch: string | undefined) => ch !== undefined && /[\p{L}\p{N}]/u.test(ch);

/** [start, end) without leading or trailing whitespace. */
function trimRange(text: string, start: number, end: number): [number, number] {
  while (start < end && /\s/.test(text[start]!)) start++;
  while (end > start && /\s/.test(text[end - 1]!)) end--;
  return [start, end];
}

/**
 * Emphasis pairs of one delimiter (`**` or `*`) in [start, end): leftmost opener, nearest closer.
 * Returns the delimiter positions; anything unpaired is left for literal text.
 */
function emphasisPairs(
  text: string,
  start: number,
  end: number,
  delimiter: '**' | '*',
  protectedRanges: readonly Range[],
): Array<{ open: number; close: number }> {
  const width = delimiter.length;
  const isDelimiterAt = (i: number) =>
    text.startsWith(delimiter, i) &&
    text[i - 1] !== '*' &&
    text[i + width] !== '*' &&
    !anyProtected(i, i + width, protectedRanges);
  const canOpen = (i: number) =>
    !isSpace(text[i + width]) && (width === 2 || !isWordChar(text[i - 1]));
  const canClose = (i: number) => !isSpace(text[i - 1]) && (width === 2 || !isWordChar(text[i + width]));

  const pairs: Array<{ open: number; close: number }> = [];
  let i = start;
  while (i + width <= end) {
    if (isDelimiterAt(i) && canOpen(i)) {
      let close = -1;
      for (let j = i + width + 1; j + width <= end; j++) {
        if (isDelimiterAt(j) && canClose(j)) {
          close = j;
          break;
        }
      }
      if (close !== -1) {
        pairs.push({ open: i, close });
        i = close + width;
        continue;
      }
    }
    i++;
  }
  return pairs;
}

/** Italic runs inside [start, end), with `styles` already applied from outside. */
function italicRuns(
  text: string,
  start: number,
  end: number,
  styles: InlineStyle[],
  protectedRanges: readonly Range[],
): Run[] {
  const runs: Run[] = [];
  let position = start;
  for (const { open, close } of emphasisPairs(text, start, end, '*', protectedRanges)) {
    if (open > position) runs.push({ start: position, end: open, styles });
    runs.push({ start: open + 1, end: close, styles: [...styles, 'italic'] });
    position = close + 1;
  }
  if (position < end) runs.push({ start: position, end, styles });
  return runs;
}

/** Bold, then italic inside and outside it, for a stretch with no code span. */
function emphasisRuns(text: string, start: number, end: number, protectedRanges: readonly Range[]): Run[] {
  const runs: Run[] = [];
  let position = start;
  for (const { open, close } of emphasisPairs(text, start, end, '**', protectedRanges)) {
    if (open > position) runs.push(...italicRuns(text, position, open, [], protectedRanges));
    runs.push(...italicRuns(text, open + 2, close, ['bold'], protectedRanges));
    position = close + 2;
  }
  if (position < end) runs.push(...italicRuns(text, position, end, [], protectedRanges));
  return runs;
}

/** Inline runs for [start, end): code spans first (their content isn't parsed), then emphasis. */
export function parseInline(text: string, start: number, end: number, protectedRanges: readonly Range[] = []): Inline {
  const runs: Run[] = [];
  let position = start;
  let i = start;
  while (i < end) {
    if (text[i] === '`' && !isProtected(i, protectedRanges)) {
      let close = -1;
      for (let j = i + 2; j < end; j++) {
        if (text[j] === '`' && !isProtected(j, protectedRanges)) {
          close = j;
          break;
        }
      }
      if (close !== -1) {
        if (i > position) runs.push(...emphasisRuns(text, position, i, protectedRanges));
        runs.push({ start: i + 1, end: close, styles: ['code'] });
        position = close + 1;
        i = close + 1;
        continue;
      }
    }
    i++;
  }
  if (position < end) runs.push(...emphasisRuns(text, position, end, protectedRanges));
  return runs.filter((run) => run.end > run.start);
}

/** The cells of a table line, as trimmed ranges; null if it has no pipe outside a figure. */
function tableCells(text: string, line: Line, protectedRanges: readonly Range[]): Array<[number, number]> | null {
  const pipes: number[] = [];
  for (let i = line.start; i < line.end; i++) {
    if (text[i] === '|') {
      if (isProtected(i, protectedRanges)) return null; // a pipe inside a figure: not a table
      pipes.push(i);
    }
  }
  if (pipes.length === 0) return null;
  const [lineStart, lineEnd] = trimRange(text, line.start, line.end);
  const bounds = [lineStart - 1, ...pipes, lineEnd];
  const cells: Array<[number, number]> = [];
  for (let k = 0; k + 1 < bounds.length; k++) cells.push(trimRange(text, bounds[k]! + 1, bounds[k + 1]!));
  // A leading or trailing pipe makes an empty first or last cell, which isn't a column.
  if (text[lineStart] === '|') cells.shift();
  if (text[lineEnd - 1] === '|' && lineEnd - 1 > lineStart) cells.pop();
  return cells;
}

function isSeparatorLine(text: string, line: Line, protectedRanges: readonly Range[]): boolean {
  const cells = tableCells(text, line, protectedRanges);
  return cells !== null && cells.length > 0 && cells.every(([s, e]) => SEPARATOR_CELL.test(text.slice(s, e)));
}

function isBlank(text: string, line: Line): boolean {
  return text.slice(line.start, line.end).trim() === '';
}

/** A list marker's length at the start of a line, or 0; never one that touches a figure. */
function markerLength(text: string, line: Line, pattern: RegExp, protectedRanges: readonly Range[]): number {
  const match = pattern.exec(text.slice(line.start, line.end));
  if (!match) return 0;
  return anyProtected(line.start, line.start + match[0].length, protectedRanges) ? 0 : match[0].length;
}

/**
 * The answer as blocks. `protectedRanges` are the cited figures' [start, end) offsets: no
 * markdown delimiter is ever taken from inside one, so a figure is never split or reworded.
 */
export function parseAnswer(text: string, protectedRanges: readonly Range[] = []): Block[] {
  const lines = splitLines(text);
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    if (isBlank(text, line)) {
      i++;
      continue;
    }

    // A table: a header line with pipes, then a separator row with the same number of cells.
    const header = tableCells(text, line, protectedRanges);
    const next = lines[i + 1];
    if (header && header.length > 0 && next && isSeparatorLine(text, next, protectedRanges)) {
      const width = header.length;
      const separator = tableCells(text, next, protectedRanges)!;
      if (separator.length === width) {
        const rows: Inline[][] = [];
        let r = i + 2;
        while (r < lines.length && !isBlank(text, lines[r]!)) {
          const cells = tableCells(text, lines[r]!, protectedRanges);
          if (!cells) break;
          rows.push(tableRow(text, cells, width, protectedRanges));
          r++;
        }
        blocks.push({
          kind: 'table',
          header: header.map(([s, e]) => parseInline(text, s, e, protectedRanges)),
          rows,
        });
        i = r;
        continue;
      }
    }

    const bullet = markerLength(text, line, BULLET, protectedRanges);
    if (bullet) {
      const items: Inline[] = [];
      while (i < lines.length) {
        const length = markerLength(text, lines[i]!, BULLET, protectedRanges);
        if (!length) break;
        items.push(parseInline(text, lines[i]!.start + length, lines[i]!.end, protectedRanges));
        i++;
      }
      blocks.push({ kind: 'bullets', items });
      continue;
    }

    const numbered = markerLength(text, line, NUMBERED, protectedRanges);
    if (numbered) {
      const first = Number(NUMBERED.exec(text.slice(line.start, line.end))![1]);
      const items: Inline[] = [];
      while (i < lines.length) {
        const length = markerLength(text, lines[i]!, NUMBERED, protectedRanges);
        if (!length) break;
        items.push(parseInline(text, lines[i]!.start + length, lines[i]!.end, protectedRanges));
        i++;
      }
      blocks.push({ kind: 'numbered', start: first, items });
      continue;
    }

    // A paragraph: consecutive lines that start nothing else.
    const paragraph: Inline[] = [];
    while (i < lines.length && !isBlank(text, lines[i]!)) {
      const current = lines[i]!;
      if (paragraph.length > 0 && startsBlock(text, current, lines[i + 1], protectedRanges)) break;
      paragraph.push(parseInline(text, current.start, current.end, protectedRanges));
      i++;
    }
    blocks.push({ kind: 'paragraph', lines: paragraph });
  }
  return blocks;
}

function startsBlock(text: string, line: Line, next: Line | undefined, protectedRanges: readonly Range[]): boolean {
  if (markerLength(text, line, BULLET, protectedRanges) || markerLength(text, line, NUMBERED, protectedRanges)) return true;
  const cells = tableCells(text, line, protectedRanges);
  return Boolean(cells && cells.length > 0 && next && isSeparatorLine(text, next, protectedRanges));
}

/**
 * A body row with exactly `width` cells: a short row is padded with empty cells, and a long
 * row's extra cells stay in its last cell as the literal text they were (pipes included).
 */
function tableRow(
  text: string,
  cells: Array<[number, number]>,
  width: number,
  protectedRanges: readonly Range[],
): Inline[] {
  const row = cells.slice(0, width).map(([s, e]) => parseInline(text, s, e, protectedRanges));
  if (cells.length > width) {
    const [lastStart] = cells[width - 1]!;
    const [, lastEnd] = cells[cells.length - 1]!;
    row[width - 1] = [{ start: lastStart, end: lastEnd, styles: [] }];
  }
  while (row.length < width) row.push([]);
  return row;
}

/** True when every protected range sits inside a single run: the renderer can keep it whole. */
export function keepsRangesWhole(blocks: Block[], protectedRanges: readonly Range[]): boolean {
  const runs: Run[] = [];
  for (const block of blocks) {
    if (block.kind === 'paragraph') block.lines.forEach((line) => runs.push(...line));
    else if (block.kind === 'table') [block.header, ...block.rows].forEach((row) => row.forEach((cell) => runs.push(...cell)));
    else block.items.forEach((item) => runs.push(...item));
  }
  return protectedRanges.every(([start, end]) => runs.some((run) => run.start <= start && end <= run.end));
}
