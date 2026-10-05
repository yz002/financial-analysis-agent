import type { AskFailure } from './askRunner';
import type {
  Citation,
  CitationComputation,
  CitationInput,
  CitationMatch,
  CitationSource,
  CitationStatus,
  FigureCheck,
} from './backendApi';
import { roleLabel } from './mappingModel';

/**
 * What the chat panel shows for an answer (Phase D session 5), as plain data -- no DOM here,
 * so every wording and every split is unit-tested; entrypoints/sidepanel/chat.ts turns it into
 * elements with createElement + textContent only. Nothing here computes a financial figure:
 * values are shown exactly as the backend returned them, and a cell's `read_value` is quoted as
 * read, never rescaled.
 */

export const QUESTION_MAX_LENGTH = 4000;

export const NOT_TRACED_LABEL = 'Not traced to your data';
export const AMBIGUOUS_LABEL = 'Found in more than one place';

export const ITERATION_CAP_NOTICE =
  'This answer stopped early: it reached the limit on lookup steps before it finished. ' +
  'Try asking a narrower question.';

/**
 * While an answer is still pending, anything that would start a new conversation (confirming a
 * statement, New conversation) is blocked: it would abandon a question that still counts.
 */
export const ANSWER_PENDING_NOTE =
  'An answer is still being prepared. Wait for it, or discard it to start over.';
export const DISCARD_LABEL = 'Discard this question (it still counts toward your limit)';

export type AnswerSegment =
  | { kind: 'text'; text: string }
  | { kind: 'figure'; text: string; citation: Citation };

/**
 * The answer split into plain text and cited figures, at each citation's offsets. A citation
 * whose offsets don't match its raw_text in this answer, or overlap an earlier one, is left as
 * plain text rather than guessed at.
 */
export function segmentAnswer(answer: string, citations: Citation[]): AnswerSegment[] {
  const usable = citations
    .filter(
      (c) =>
        Number.isInteger(c.start) &&
        Number.isInteger(c.end) &&
        c.start >= 0 &&
        c.start < c.end &&
        c.end <= answer.length &&
        answer.slice(c.start, c.end) === c.raw_text,
    )
    .sort((a, b) => a.start - b.start);

  const segments: AnswerSegment[] = [];
  let position = 0;
  for (const citation of usable) {
    if (citation.start < position) continue;
    if (citation.start > position) {
      segments.push({ kind: 'text', text: answer.slice(position, citation.start) });
    }
    segments.push({ kind: 'figure', text: citation.raw_text, citation });
    position = citation.end;
  }
  if (position < answer.length) segments.push({ kind: 'text', text: answer.slice(position) });
  return segments;
}

/** The marker shown next to a figure; null for a figure traced to exactly one source. */
export function figureStatusLabel(status: CitationStatus): string | null {
  if (status === 'traced') return null;
  if (status === 'ambiguous') return AMBIGUOUS_LABEL;
  return NOT_TRACED_LABEL;
}

/** One line of a figure's sources. `ref` (a cell address) is shown in mono, apart from text. */
export interface SourceLine {
  ref: string | null;
  text: string;
  children: SourceLine[];
}

export interface CitationView {
  figure: string;
  status: CitationStatus;
  statusLabel: string | null;
  lines: SourceLine[];
}

const NAMES: Record<string, string> = {
  gross_margin: 'Gross margin',
  operating_margin: 'Operating margin',
  net_margin: 'Net margin',
  revenue_growth_qoq: 'Revenue growth, quarter over quarter',
  revenue_growth_yoy: 'Revenue growth, year over year',
  earnings_growth_qoq: 'Net income growth, quarter over quarter',
  earnings_growth_yoy: 'Net income growth, year over year',
  free_cash_flow: 'Free cash flow',
  debt_to_assets: 'Debt to assets',
  current_ratio: 'Current ratio',
  roa: 'Return on assets',
  roe: 'Return on equity',
  net_income_ttm: 'Net income, last four quarters',
};

function conceptLabel(concept: string | undefined): string {
  if (!concept) return 'Value';
  if (concept === 'net_income_ttm') return NAMES.net_income_ttm!;
  if (concept.endsWith('_prior')) return `${roleLabel(concept.slice(0, -'_prior'.length))}, prior period`;
  return roleLabel(concept);
}

function computationLabel(name: string): string {
  return NAMES[name] ?? name.replace(/_/g, ' ');
}

function period(periodEnd: string | null | undefined): string {
  return periodEnd ? `, period ending ${periodEnd}` : '';
}

function cellLine(source: CitationSource, label: string): SourceLine {
  const parts: string[] = [];
  if (!source.cell && source.column) parts.push(`Column “${source.column}”`);
  if (source.read_value !== null && source.read_value !== undefined) {
    const scale = source.sheet_scale && source.sheet_scale !== 'ones' ? `, in ${source.sheet_scale}` : '';
    parts.push(`= ${source.read_value} (as read${scale})`);
  }
  const reading = parts.join(' ');
  return {
    ref: source.cell ?? null,
    text: `${reading}${reading ? ' · ' : ''}${label}${period(source.period_end)}`,
    children: [],
  };
}

function filingLine(source: CitationSource, label: string): SourceLine {
  const ticker = source.ticker ? `${source.ticker} ` : '';
  const derived = source.is_derived ? ' (derived, not directly filed)' : '';
  return {
    ref: null,
    text:
      `${ticker}${label}${period(source.period_end)} · SEC filing, tag ${source.tag ?? 'unknown'}` +
      `${source.filed ? `, filed ${source.filed}` : ''}${derived}`,
    children: [],
  };
}

function sourceLine(source: CitationSource | null | undefined, label: string): SourceLine {
  if (!source) return { ref: null, text: `${label}: source not available`, children: [] };
  if ('cell' in source || 'read_value' in source || 'column' in source) return cellLine(source, label);
  if ('tag' in source) return filingLine(source, label);
  return { ref: null, text: label, children: [] };
}

function inputLine(input: CitationInput): SourceLine {
  if (input.computation) return computationLine(input.computation);
  return sourceLine(input.source, conceptLabel(input.role));
}

function computationLine(computation: CitationComputation): SourceLine {
  const formula = computation.formula ? ` = ${computation.formula}` : '';
  return {
    ref: null,
    text: `${computationLabel(computation.name)}${period(computation.period_end)}${formula}`,
    children: computation.inputs.map(inputLine),
  };
}

function matchLine(match: CitationMatch): SourceLine {
  let line: SourceLine;
  if (match.kind === 'derived' && match.computation) {
    line = computationLine(match.computation);
  } else if (match.kind === 'tool') {
    const tool = match.source?.tool_name ?? 'a tool';
    line = {
      ref: null,
      text:
        tool === 'forecast_metric'
          ? 'A projection from forecast_metric, not a filed or entered figure'
          : `From ${tool}`,
      children: [],
    };
  } else {
    line = sourceLine(match.source, conceptLabel(match.source?.concept));
  }
  if (match.turn_id) line = { ...line, text: `${line.text} (from an earlier answer)` };
  return line;
}

export function describeCitation(citation: Citation): CitationView {
  return {
    figure: citation.raw_text,
    status: citation.status,
    statusLabel: figureStatusLabel(citation.status),
    lines: citation.matches.map(matchLine),
  };
}

export interface FigureCheckBanner {
  tone: 'ok' | 'warn' | 'none';
  text: string;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The summary above an answer, in plain words. */
export function figureCheckBanner(figureCheck: FigureCheck, citations: Citation[]): FigureCheckBanner {
  const total = figureCheck.figures?.length ?? citations.length;
  const notTraced = citations.filter((c) => c.status === 'weak' || c.status === 'untraced').length;
  const ambiguous = citations.filter((c) => c.status === 'ambiguous').length;
  const skipped = figureCheck.figures_skipped?.length ?? 0;

  if (total === 0 && skipped === 0) {
    return { tone: 'none', text: 'This answer has no figures to check.' };
  }
  const sentences: string[] = [];
  let tone: FigureCheckBanner['tone'];
  if (notTraced === 0) {
    tone = skipped === 0 ? 'ok' : 'warn';
    if (total > 0) sentences.push('Every figure in this answer was found in your data or the sources used.');
  } else {
    tone = 'warn';
    sentences.push(
      `${notTraced} of ${plural(total, 'figure', 'figures')} couldn't be found in your data or the ` +
        `sources used. They're marked “${NOT_TRACED_LABEL}”. Check them before relying on them.`,
    );
  }
  if (ambiguous > 0) {
    sentences.push(
      `${plural(ambiguous, 'figure matches', 'figures match')} more than one place; every place is listed.`,
    );
  }
  if (skipped > 0) {
    sentences.push(
      `${plural(skipped, 'figure', 'figures')} couldn't be checked because of how ` +
        `${skipped === 1 ? 'it was' : 'they were'} written.`,
    );
  }
  return { tone, text: sentences.join(' ') };
}

/** The panel's message for a question that ended without an answer. */
export function failureMessage(failure: AskFailure): string {
  switch (failure.kind) {
    case 'answer_failed':
    case 'answer_lost':
      return "That question didn't finish. Ask it again.";
    case 'time_budget':
      return 'That question took too long and was stopped. Try asking a narrower question.';
    case 'cap': {
      const when = failure.resetsAt
        ? ` More are available from ${new Date(failure.resetsAt).toLocaleString()}.`
        : '';
      return failure.error === 'monthly_cap_reached'
        ? `You've used all the questions for this billing period.${when}`
        : `You've used today's questions.${when}`;
    }
    case 'invalid_question':
      return `That question couldn't be sent. Questions can be up to ${QUESTION_MAX_LENGTH.toLocaleString('en-US')} characters.`;
    case 'request_id_reused':
      return 'That question was sent in a way the server rejected. This is a bug; please report it.';
    case 'ai_service':
      return "The AI service couldn't answer right now. Try again in a moment.";
    case 'still_running':
      return 'Still not finished — check back later.';
    case 'stopped':
      return 'Stopped waiting. The answer may still arrive: select Check again.';
    case 'conversation_not_found':
      return 'This conversation is no longer available. Ask your question again to start a new one.';
    case 'statement_needs_reconfirm':
      return 'This statement needs to be confirmed again. Read the range and confirm it, then ask again.';
    case 'statement_mismatch':
      return 'This conversation is about a different statement.';
    case 'unauthorized':
      return 'Your session ended — please sign in again.';
    case 'server':
    default:
      return 'Something went wrong answering that question. Try again.';
  }
}
