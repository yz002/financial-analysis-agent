import { describe, expect, it, vi } from 'vitest';
import {
  classifyAskError,
  FIRST_RETRY_DELAY_MS,
  MAX_RETRY_DELAY_MS,
  RETRY_BUDGET_MS,
  runAsk,
  type AskRunnerDeps,
} from './askRunner';
import { BackendApiError, type AskRequestBody, type AskResponse } from './backendApi';

const BODY: AskRequestBody = {
  question: 'What was revenue?',
  csv_context_id: 'ctx-1',
  conversation_id: null,
  request_id: 'req-1',
};

const ANSWER: AskResponse = {
  conversation_id: 'conv-1',
  turn_id: 'turn-1',
  final_answer: 'Revenue was 1.25 million.',
  hit_iteration_cap: false,
  figure_check: { figures: [] },
  citations: [],
  tool_calls_summary: [],
};

const backend = (status: number, detail: unknown) =>
  new BackendApiError(`/v1/ask failed (${status}).`, status, detail);
const gateway = (status: number) => new BackendApiError(`/v1/ask failed (${status}).`, status, null, false);
const abortError = () => new DOMException('The operation was aborted.', 'AbortError');

/** Deps with a virtual clock: sleep advances it instantly and records each delay. */
function fakeDeps(attempts: Array<AskResponse | Error>) {
  let clock = 1_000_000;
  const sleeps: number[] = [];
  const sent: AskRequestBody[] = [];
  const deps: AskRunnerDeps = {
    send: vi.fn(async (body: AskRequestBody) => {
      sent.push(body);
      const next = attempts.shift();
      if (next === undefined) throw new Error('no more scripted attempts');
      if (next instanceof Error) throw next;
      return next;
    }),
    sleep: vi.fn(async (ms: number) => {
      sleeps.push(ms);
      clock += ms;
    }),
    now: () => clock,
  };
  return { deps, sleeps, sent, startedAt: clock };
}

describe('runAsk', () => {
  it('returns the answer from the first attempt', async () => {
    const { deps, sleeps, startedAt } = fakeDeps([ANSWER]);
    const outcome = await runAsk(BODY, startedAt, deps, new AbortController().signal);
    expect(outcome).toEqual({ kind: 'answered', response: ANSWER });
    expect(sleeps).toEqual([]);
  });

  it('after a client timeout, resends the same body -- the same request_id -- and gets the answer', async () => {
    const { deps, sent, startedAt } = fakeDeps([abortError(), ANSWER]);
    const outcome = await runAsk(BODY, startedAt, deps, new AbortController().signal);
    expect(outcome.kind).toBe('answered');
    expect(sent).toHaveLength(2);
    expect(sent[1]).toEqual(sent[0]);
    expect(sent[1]!.request_id).toBe('req-1');
  });

  it.each([
    ['a network error', new TypeError('Failed to fetch')],
    ['a bare gateway 502', gateway(502)],
    ['a bare gateway 504', gateway(504)],
    ['answer_in_progress', backend(409, { error: 'answer_in_progress' })],
  ])('resends after %s', async (_name, error) => {
    const { deps, startedAt } = fakeDeps([error, ANSWER]);
    const outcome = await runAsk(BODY, startedAt, deps, new AbortController().signal);
    expect(outcome.kind).toBe('answered');
  });

  it('backs off between resends, up to the maximum delay', async () => {
    const inProgress = () => backend(409, { error: 'answer_in_progress' });
    const { deps, sleeps, startedAt } = fakeDeps([inProgress(), inProgress(), inProgress(), inProgress(), inProgress(), ANSWER]);
    await runAsk(BODY, startedAt, deps, new AbortController().signal);
    expect(sleeps[0]).toBe(FIRST_RETRY_DELAY_MS);
    for (let i = 1; i < sleeps.length; i++) expect(sleeps[i]!).toBeGreaterThanOrEqual(sleeps[i - 1]!);
    expect(Math.max(...sleeps)).toBeLessThanOrEqual(MAX_RETRY_DELAY_MS);
  });

  it('gives up as still_running once the retry budget is spent -- never an endless loop', async () => {
    const outage = Array.from({ length: 200 }, () => gateway(502));
    const { deps, sleeps, startedAt } = fakeDeps(outage);
    const outcome = await runAsk(BODY, startedAt, deps, new AbortController().signal);
    expect(outcome).toEqual({ kind: 'failed', failure: { kind: 'still_running' } });
    const waited = sleeps.reduce((a, b) => a + b, 0);
    expect(waited).toBeLessThanOrEqual(RETRY_BUDGET_MS);
    expect(deps.send).toHaveBeenCalledTimes(sleeps.length + 1);
    expect(sleeps.length).toBeLessThan(40);
  });

  it('measures the budget from when the question was first sent, but always checks once', async () => {
    const { deps, startedAt } = fakeDeps([backend(409, { error: 'answer_in_progress' })]);
    const longAgo = startedAt - RETRY_BUDGET_MS - 1;
    const outcome = await runAsk(BODY, longAgo, deps, new AbortController().signal);
    expect(outcome).toEqual({ kind: 'failed', failure: { kind: 'still_running' } });
    expect(deps.send).toHaveBeenCalledTimes(1);
  });

  it('a reopened panel past its budget still gets the finished answer on its one check', async () => {
    const { deps, startedAt } = fakeDeps([ANSWER]);
    const outcome = await runAsk(BODY, startedAt - 2 * RETRY_BUDGET_MS, deps, new AbortController().signal);
    expect(outcome.kind).toBe('answered');
  });

  it('stops when the person stops waiting, keeping the request for later', async () => {
    const stop = new AbortController();
    const { deps, startedAt } = fakeDeps([]);
    deps.send = vi.fn(async (_body, signal: AbortSignal) => {
      stop.abort();
      expect(signal.aborted).toBe(true); // the in-flight request is aborted too
      throw abortError();
    });
    const outcome = await runAsk(BODY, startedAt, deps, stop.signal);
    expect(outcome).toEqual({ kind: 'failed', failure: { kind: 'stopped' } });
  });

  it.each([
    [backend(409, { error: 'statement_mismatch' }), { kind: 'statement_mismatch' }],
    [backend(409, { error: 'statement_needs_reconfirm' }), { kind: 'statement_needs_reconfirm' }],
    [backend(409, { error: 'answer_failed' }), { kind: 'answer_failed' }],
    [backend(409, { error: 'answer_lost' }), { kind: 'answer_lost' }],
    [backend(422, { error: 'request_id_reused' }), { kind: 'request_id_reused' }],
    [backend(504, { error: 'answer_time_budget_exceeded' }), { kind: 'time_budget' }],
    [backend(401, 'Invalid or expired session token.'), { kind: 'unauthorized' }],
    [backend(502, 'Anthropic API error.'), { kind: 'ai_service' }],
    [backend(500, 'run_agent failed unexpectedly.'), { kind: 'server' }],
  ])('does not resend a final response (%#)', async (error, failure) => {
    const { deps, startedAt } = fakeDeps([error]);
    const outcome = await runAsk(BODY, startedAt, deps, new AbortController().signal);
    expect(outcome).toEqual({ kind: 'failed', failure });
    expect(deps.send).toHaveBeenCalledTimes(1);
  });
});

describe('classifyAskError', () => {
  it('a 504 with this backend\'s budget body is final; a bare 504 is a gateway problem to resend', () => {
    expect(classifyAskError(backend(504, { error: 'answer_time_budget_exceeded' }))).toEqual({ kind: 'time_budget' });
    expect(classifyAskError(gateway(504))).toBe('retry');
  });

  it('a 502 with a JSON detail is the backend\'s own failure; without one it is a gateway', () => {
    expect(classifyAskError(backend(502, 'Anthropic API error.'))).toEqual({ kind: 'ai_service' });
    expect(classifyAskError(gateway(502))).toBe('retry');
  });

  it('maps the 429 caps with their reset time', () => {
    expect(
      classifyAskError(
        backend(429, {
          error: 'daily_cap_reached', prompt_byo_key: false, prompt_upgrade: true, resets_at: '2026-10-06T00:00:00Z',
        }),
      ),
    ).toEqual({ kind: 'cap', error: 'daily_cap_reached', resetsAt: '2026-10-06T00:00:00Z' });
    expect(classifyAskError(backend(429, { error: 'monthly_cap_reached', resets_at: null }))).toEqual({
      kind: 'cap', error: 'monthly_cap_reached', resetsAt: null,
    });
  });

  it('maps the 404s: a gone conversation, or a statement that needs re-confirming', () => {
    expect(classifyAskError(backend(404, 'conversation not found'))).toEqual({ kind: 'conversation_not_found' });
    expect(classifyAskError(backend(404, 'csv context not found'))).toEqual({ kind: 'statement_needs_reconfirm' });
  });

  it('a validation 422 is an invalid question, not a reused id', () => {
    expect(classifyAskError(backend(422, [{ loc: ['body', 'question'], msg: 'too long' }]))).toEqual({
      kind: 'invalid_question',
    });
  });

  it('anything unrecognized is a server problem, never a silent retry', () => {
    expect(classifyAskError(new Error('boom'))).toEqual({ kind: 'server' });
    expect(classifyAskError(backend(409, { error: 'something_new' }))).toEqual({ kind: 'server' });
    expect(classifyAskError(gateway(500))).toEqual({ kind: 'server' });
  });
});
