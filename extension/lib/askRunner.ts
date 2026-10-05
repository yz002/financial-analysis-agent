import { BackendApiError, type AskRequestBody, type AskResponse } from './backendApi';

/**
 * Sending one question to POST /v1/ask and waiting for its answer (EXTENSION_INTEGRATION.md SS6
 * /v1/ask, amended session 5, "Long answers and recovery").
 *
 * - One request waits up to CLIENT_TIMEOUT_MS.
 * - After that, or after a network error, a bare gateway 502/503/504, or answer_in_progress,
 *   the same body is resent -- same request_id, so the backend never runs or charges the
 *   question twice -- after a backoff that starts at FIRST_RETRY_DELAY_MS and grows to
 *   MAX_RETRY_DELAY_MS.
 * - Resending stops once RETRY_BUDGET_MS has passed since the question was first sent: the
 *   result is "still_running", the caller keeps the pending entry, and the person can check
 *   again later. It never loops forever during an outage.
 * - Every other response is final and mapped to a typed outcome; nothing else is retried.
 */

export const CLIENT_TIMEOUT_MS = 5 * 60_000;
export const RETRY_BUDGET_MS = 30 * 60_000;
export const FIRST_RETRY_DELAY_MS = 20_000;
export const MAX_RETRY_DELAY_MS = 2 * 60_000;
const BACKOFF_FACTOR = 1.5;

export type AskFailure =
  | { kind: 'unauthorized' }
  | { kind: 'statement_mismatch' }
  | { kind: 'statement_needs_reconfirm' }
  | { kind: 'conversation_not_found' }
  | { kind: 'answer_failed' }
  | { kind: 'answer_lost' }
  | { kind: 'request_id_reused' }
  | { kind: 'time_budget' }
  | { kind: 'cap'; error: string; resetsAt: string | null }
  | { kind: 'invalid_question' }
  | { kind: 'ai_service' }
  | { kind: 'server' }
  | { kind: 'still_running' }
  | { kind: 'stopped' };

export type AskOutcome =
  | { kind: 'answered'; response: AskResponse }
  | { kind: 'failed'; failure: AskFailure };

export interface AskRunnerDeps {
  send(body: AskRequestBody, signal: AbortSignal): Promise<AskResponse>;
  /** Resolves after `ms`, or as soon as `signal` aborts. */
  sleep(ms: number, signal: AbortSignal): Promise<void>;
  now(): number;
  clientTimeoutMs?: number;
}

const RETRY = 'retry' as const;

function detailError(detail: unknown): string | null {
  if (detail && typeof detail === 'object' && 'error' in detail) {
    const error = (detail as { error: unknown }).error;
    return typeof error === 'string' ? error : null;
  }
  return null;
}

/** One failed attempt: worth resending with the same request_id, or a final outcome. */
export function classifyAskError(err: unknown): typeof RETRY | AskFailure {
  if (err instanceof DOMException && err.name === 'AbortError') return RETRY; // our timeout
  if (err instanceof TypeError) return RETRY; // network failure: fetch rejects with TypeError
  if (!(err instanceof BackendApiError)) return { kind: 'server' };

  if (!err.fromBackend) {
    // No JSON `detail`: a proxy or gateway answered, not this backend. The run may still be
    // going (or finished), so a gateway error is resent; anything else is a server problem.
    return [502, 503, 504].includes(err.status) ? RETRY : { kind: 'server' };
  }

  const code = detailError(err.detail);
  switch (err.status) {
    case 401:
      return { kind: 'unauthorized' };
    case 404:
      // A conversation that's gone. ("csv context not found" can't happen for a statement the
      // panel confirmed unless it was deleted -- the same remedy as re-confirming.)
      return err.detail === 'conversation not found'
        ? { kind: 'conversation_not_found' }
        : { kind: 'statement_needs_reconfirm' };
    case 409:
      if (code === 'answer_in_progress') return RETRY;
      if (
        code === 'statement_mismatch' ||
        code === 'statement_needs_reconfirm' ||
        code === 'answer_failed' ||
        code === 'answer_lost'
      ) {
        return { kind: code };
      }
      return { kind: 'server' };
    case 422:
      return code === 'request_id_reused' ? { kind: 'request_id_reused' } : { kind: 'invalid_question' };
    case 429: {
      const detail = err.detail as { error?: unknown; resets_at?: unknown } | null;
      return {
        kind: 'cap',
        error: typeof detail?.error === 'string' ? detail.error : 'cap_reached',
        resetsAt: typeof detail?.resets_at === 'string' ? detail.resets_at : null,
      };
    }
    case 502:
      return { kind: 'ai_service' };
    case 504:
      return code === 'answer_time_budget_exceeded' ? { kind: 'time_budget' } : { kind: 'server' };
    default:
      return { kind: 'server' };
  }
}

/**
 * Sends `body` until it's answered, fails for good, `stop` aborts (the person chose to stop
 * waiting), or the retry budget -- measured from `startedAt`, the time the question was first
 * sent, so a reopened panel doesn't get a fresh 30 minutes -- runs out. At least one attempt is
 * always made, so a reopened panel past its budget still checks once.
 */
export async function runAsk(
  body: AskRequestBody,
  startedAt: number,
  deps: AskRunnerDeps,
  stop: AbortSignal,
): Promise<AskOutcome> {
  const timeoutMs = deps.clientTimeoutMs ?? CLIENT_TIMEOUT_MS;
  let delay = FIRST_RETRY_DELAY_MS;
  for (;;) {
    if (stop.aborted) return { kind: 'failed', failure: { kind: 'stopped' } };

    const attempt = new AbortController();
    const abortAttempt = () => attempt.abort();
    stop.addEventListener('abort', abortAttempt);
    const timer = setTimeout(abortAttempt, timeoutMs);
    try {
      return { kind: 'answered', response: await deps.send(body, attempt.signal) };
    } catch (err) {
      if (stop.aborted) return { kind: 'failed', failure: { kind: 'stopped' } };
      const result = classifyAskError(err);
      if (result !== RETRY) return { kind: 'failed', failure: result };
    } finally {
      clearTimeout(timer);
      stop.removeEventListener('abort', abortAttempt);
    }

    if (deps.now() - startedAt + delay > RETRY_BUDGET_MS) {
      return { kind: 'failed', failure: { kind: 'still_running' } };
    }
    await deps.sleep(delay, stop);
    delay = Math.min(delay * BACKOFF_FACTOR, MAX_RETRY_DELAY_MS);
  }
}

/** A sleep that ends early when `signal` aborts -- the default for AskRunnerDeps.sleep. */
export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    }
    signal.addEventListener('abort', done);
  });
}
