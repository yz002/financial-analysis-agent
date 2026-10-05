// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeBrowser } from 'wxt/testing/fake-browser';
import { BackendApiError, type AskRequestBody, type AskResponse, type Citation } from '../../lib/backendApi';
import { getConversation, getPendingAsk, setPendingAsk } from '../../lib/conversationStorage';
import type { ActiveStatement } from '../../lib/sessionStorage';
import { ChatController, renderAnswer, type ChatDeps } from './chat';

const MARKUP = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';

const STATEMENT: ActiveStatement = {
  csvContextId: 'ctx-1',
  entityName: 'Acme',
  label: "Q3 P&L · 'P&L'!A3:C9",
  confirmedAt: '2026-10-05T00:00:00Z',
  cadence: 'quarterly',
  scale: 'thousands',
  currency: null,
};

function answer(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    conversation_id: 'conv-1',
    turn_id: 'turn-1',
    final_answer: 'Revenue was 1.25 million.',
    hit_iteration_cap: false,
    figure_check: { figures: [{ raw_text: '1.25 million', start: 12, end: 24, traced: true, weak_match: false }] },
    citations: [
      {
        figure_index: 0, raw_text: '1.25 million', start: 12, end: 24, status: 'traced',
        matches: [{
          kind: 'cell', value: 1250000, turn_id: null,
          source: { cell: "'P&L'!B4", concept: 'revenue', column: 'Revenue', period_end: '2025-03-31', read_value: '1250', sheet_scale: 'thousands' },
        }],
      },
    ],
    tool_calls_summary: [],
    ...overrides,
  };
}

const backend = (status: number, detail: unknown) => new BackendApiError(`failed (${status})`, status, detail);
const gateway = (status: number) => new BackendApiError(`failed (${status})`, status, null, false);

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

const PANEL = `
  <section id="chat-panel" hidden>
    <p id="chat-statement"></p>
    <div id="chat-mismatch" hidden><p id="chat-mismatch-text"></p><button id="chat-new" type="button">New conversation</button></div>
    <div id="chat-log"></div>
    <div id="chat-waiting" hidden><p id="chat-waiting-text"></p><button id="chat-stop" type="button">Stop waiting</button></div>
    <p id="chat-notice"></p>
    <button id="chat-check" type="button" hidden>Check again</button>
        <button id="chat-discard" type="button" hidden></button>
        <p id="chat-pending-note" hidden></p>
    <form id="chat-form"><textarea id="chat-input"></textarea><button id="chat-send" type="submit">Ask</button></form>
  </section>`;

const $ = <T extends HTMLElement>(selector: string) => document.querySelector<T>(selector)!;

interface Harness {
  chat: ChatController;
  send: ReturnType<typeof vi.fn>;
  deps: ChatDeps;
  sent: AskRequestBody[];
}

function makeChat(responses: Array<AskResponse | Error | 'hang'>): Harness {
  document.body.innerHTML = PANEL;
  const sent: AskRequestBody[] = [];
  let clock = 1_000_000;
  const send = vi.fn((_token: string, body: AskRequestBody, signal: AbortSignal) => {
    sent.push(body);
    const next = responses.shift();
    if (next === 'hang') {
      return new Promise<AskResponse>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      });
    }
    if (next === undefined) return Promise.reject(new Error('no scripted response'));
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
  });
  const deps: ChatDeps = {
    getSession: async () => ({ sessionToken: 'tok', accountId: 'a', expiresAt: 'e', provider: 'google' }),
    send,
    onUnauthorized: vi.fn(async () => {}),
    onStatementNeedsReconfirm: vi.fn(async () => {}),
    onPendingChange: vi.fn(),
    newRequestId: vi.fn(() => `req-${sent.length + 1}`),
    now: () => clock,
    sleep: async (ms: number) => {
      clock += ms;
    },
  };
  const chat = new ChatController(
    {
      panel: $('#chat-panel'),
      statementLine: $('#chat-statement'),
      mismatch: $('#chat-mismatch'),
      mismatchText: $('#chat-mismatch-text'),
      newConversationButton: $('#chat-new'),
      log: $('#chat-log'),
      notice: $('#chat-notice'),
      waiting: $('#chat-waiting'),
      waitingText: $('#chat-waiting-text'),
      stopButton: $('#chat-stop'),
      checkButton: $('#chat-check'),
      discardButton: $('#chat-discard'),
      pendingNote: $('#chat-pending-note'),
      form: $('#chat-form'),
      input: $('#chat-input'),
      sendButton: $('#chat-send'),
    },
    deps,
  );
  return { chat, send, deps, sent };
}

async function ask(question: string): Promise<void> {
  $<HTMLTextAreaElement>('#chat-input').value = question;
  $<HTMLFormElement>('#chat-form').requestSubmit();
  await settle();
}

beforeEach(() => {
  fakeBrowser.reset();
  delete (window as { __pwned?: number }).__pwned;
});

describe('renderAnswer: text from the backend or the sheet never becomes markup', () => {
  it('answer text, figures, cells, read values and column names are all plain text', () => {
    const final_answer = `Note ${MARKUP} revenue was 1.25 million.`;
    const start = final_answer.indexOf('1.25 million');
    const citations: Citation[] = [
      {
        figure_index: 0, raw_text: '1.25 million', start, end: start + 12, status: 'untraced', matches: [],
      },
      {
        figure_index: 1, raw_text: '1.25 million', start, end: start + 12, status: 'ambiguous',
        matches: [{
          kind: 'cell', value: 1, turn_id: null,
          source: { cell: `'${MARKUP}'!B4`, concept: 'revenue', column: MARKUP, period_end: null, read_value: MARKUP, sheet_scale: 'ones' },
        }, {
          kind: 'cell', value: 1, turn_id: null,
          source: { cell: null, concept: 'revenue', column: MARKUP, period_end: null, read_value: MARKUP, sheet_scale: 'ones' },
        }],
      },
    ];
    const article = renderAnswer(answer({ final_answer, citations, figure_check: { figures: [] } }));
    document.body.append(article);

    expect(article.querySelectorAll('img, script')).toHaveLength(0);
    expect(article.textContent).toContain(MARKUP);
    expect(article.innerHTML).toContain('&lt;img');
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('marks a figure not traced to the data, in plain words', () => {
    const article = renderAnswer(answer({
      citations: [{ ...answer().citations[0]!, status: 'untraced', matches: [] }],
    }));
    expect(article.querySelector('.figure--untraced')!.textContent).toBe('1.25 million');
    expect(article.querySelector('.figure-marker')!.textContent).toBe('Not traced to your data');
    expect(article.querySelector('.figure-check')!.textContent).toContain("couldn't be found in your data");
  });

  it('shows the iteration-cap notice when the answer stopped early', () => {
    const article = renderAnswer(answer({ hit_iteration_cap: true }));
    expect(article.querySelector('.chat-cap-notice')!.textContent).toContain('stopped early');
    expect(renderAnswer(answer()).querySelector('.chat-cap-notice')).toBeNull();
  });

  it('lists where each figure comes from, with the cell in its own element', () => {
    const article = renderAnswer(answer());
    expect(article.querySelector('.chat-sources .ref')!.textContent).toBe("'P&L'!B4");
    expect(article.querySelector('.chat-sources')!.textContent).toContain('= 1250 (as read, in thousands)');
  });
});

describe('ChatController', () => {
  it('needs a confirmed statement: hidden without one, shown with one', async () => {
    const { chat } = makeChat([]);
    await chat.show(null);
    expect($('#chat-panel').hidden).toBe(true);
    await chat.show(STATEMENT);
    expect($('#chat-panel').hidden).toBe(false);
    expect($('#chat-statement').textContent).toBe("Answers come from Acme — Q3 P&L · 'P&L'!A3:C9.");
  });

  it('asks with the statement and a request id, then continues the same conversation', async () => {
    const { chat, sent } = makeChat([answer(), answer({ turn_id: 'turn-2' })]);
    await chat.show(STATEMENT);

    await ask('What was revenue?');
    expect(sent[0]).toEqual({
      question: 'What was revenue?', csv_context_id: 'ctx-1', conversation_id: null, request_id: 'req-1',
    });
    expect(await getPendingAsk()).toBeNull();
    expect((await getConversation())!.conversationId).toBe('conv-1');
    expect($('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1);

    await ask('And margin?');
    expect(sent[1]!.conversation_id).toBe('conv-1');
    expect(sent[1]!.request_id).toBe('req-2');
  });

  it('keeps the pending question in session storage while it waits', async () => {
    const { chat } = makeChat(['hang']);
    await chat.show(STATEMENT);
    await ask('Slow one?');
    const pending = await getPendingAsk();
    expect(pending).toEqual(expect.objectContaining({ requestId: 'req-1', question: 'Slow one?', csvContextId: 'ctx-1' }));
    expect($('#chat-waiting').hidden).toBe(false);
    expect($<HTMLButtonElement>('#chat-send').disabled).toBe(true);
  });

  it('resends the same request id after a bare gateway error and shows the answer', async () => {
    const { chat, sent } = makeChat([gateway(504), gateway(502), answer()]);
    await chat.show(STATEMENT);
    await ask('What was revenue?');
    expect(sent.map((b) => b.request_id)).toEqual(['req-1', 'req-1', 'req-1']);
    expect($('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1);
  });

  it('during an outage, stops resending and says "Still not finished — check back later", keeping the pending entry', async () => {
    const outage = Array.from({ length: 100 }, () => gateway(502));
    const { chat, send } = makeChat(outage);
    await chat.show(STATEMENT);
    await ask('What was revenue?');

    expect($('#chat-notice').textContent).toBe('Still not finished — check back later.');
    expect($('#chat-check').hidden).toBe(false);
    expect(await getPendingAsk()).not.toBeNull();
    expect(send.mock.calls.length).toBeLessThan(40);
  });

  it('a reopened panel resumes a pending question with its original request id', async () => {
    const { chat, sent } = makeChat([answer()]);
    await setPendingAsk({
      requestId: 'req-from-before', question: 'Earlier?', csvContextId: 'ctx-1', conversationId: null, startedAt: 1_000_000,
    });
    await chat.show(STATEMENT);
    await settle();
    expect(sent[0]!.request_id).toBe('req-from-before');
    expect(await getPendingAsk()).toBeNull();
    expect($('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1);
  });

  it('Stop waiting keeps the pending entry and offers Check again, which resends the same id', async () => {
    const { chat, sent } = makeChat(['hang', answer()]);
    await chat.show(STATEMENT);
    await ask('Slow one?');
    $<HTMLButtonElement>('#chat-stop').click();
    await settle();
    expect($('#chat-notice').textContent).toContain('Stopped waiting');
    expect(await getPendingAsk()).not.toBeNull();

    $<HTMLButtonElement>('#chat-check').click();
    await settle();
    expect(sent.map((b) => b.request_id)).toEqual(['req-1', 'req-1']);
    expect($('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1);
  });

  it('statement_mismatch offers a new conversation, never switching, and keeps the question', async () => {
    const { chat } = makeChat([backend(409, { error: 'statement_mismatch' })]);
    await chat.show(STATEMENT);
    await ask('Switch?');

    expect($('#chat-mismatch').hidden).toBe(false);
    expect($('#chat-mismatch-text').textContent).toContain('Start a new conversation');
    expect($<HTMLTextAreaElement>('#chat-input').value).toBe('Switch?');
    expect($('#chat-log').querySelector('.chat-question')).toBeNull();
    expect(await getPendingAsk()).toBeNull();

    $<HTMLButtonElement>('#chat-new').click();
    await settle();
    expect($('#chat-mismatch').hidden).toBe(true);
    expect((await getConversation())!.conversationId).toBeNull();
  });

  it('a stored conversation for another statement is offered as a new conversation', async () => {
    const { chat } = makeChat([answer()]);
    await chat.show({ ...STATEMENT, csvContextId: 'ctx-old', entityName: 'Old Co' });
    await ask('Q?');
    await chat.show(STATEMENT);
    expect($('#chat-mismatch').hidden).toBe(false);
    expect($('#chat-mismatch-text').textContent).toContain('Old Co');
  });

  it('statement_needs_reconfirm clears the chat and hands back to the panel', async () => {
    const { chat, deps } = makeChat([backend(409, { error: 'statement_needs_reconfirm' })]);
    await chat.show(STATEMENT);
    await ask('Q?');
    expect(deps.onStatementNeedsReconfirm).toHaveBeenCalledWith(expect.stringContaining('confirmed again'));
    expect(await getConversation()).toBeNull();
    expect(await getPendingAsk()).toBeNull();
  });

  it('a 401 ends the session through the panel', async () => {
    const { chat, deps } = makeChat([backend(401, 'Invalid or expired session token.')]);
    await chat.show(STATEMENT);
    await ask('Q?');
    expect(deps.onUnauthorized).toHaveBeenCalled();
  });

  it.each([
    [backend(429, { error: 'daily_cap_reached', resets_at: null }), "You've used today's questions."],
    [backend(409, { error: 'answer_failed' }), "That question didn't finish. Ask it again."],
    [backend(504, { error: 'answer_time_budget_exceeded' }), 'took too long'],
    [backend(502, 'Anthropic API error.'), "The AI service couldn't answer right now."],
    [backend(500, 'run_agent failed unexpectedly.'), 'Something went wrong'],
  ])('a final error is explained and the question is put back (%#)', async (error, message) => {
    const { chat, send } = makeChat([error]);
    await chat.show(STATEMENT);
    await ask('Q?');
    expect(send).toHaveBeenCalledTimes(1);
    expect($('#chat-log').textContent).toContain(message);
    expect($<HTMLTextAreaElement>('#chat-input').value).toBe('Q?');
    expect(await getPendingAsk()).toBeNull();
  });

  it('a new conversation drops the transcript and any pending question', async () => {
    const { chat } = makeChat(['hang']);
    await chat.show(STATEMENT);
    await ask('Slow one?');
    await chat.startNewConversation({ ...STATEMENT, csvContextId: 'ctx-2' });
    expect(await getPendingAsk()).toBeNull();
    expect((await getConversation())).toEqual(expect.objectContaining({ csvContextId: 'ctx-2', messages: [] }));
    expect($('#chat-log').children).toHaveLength(0);
    expect($('#chat-waiting').hidden).toBe(true);
  });

  it('while an answer is pending, New conversation is blocked with a plain reason and a Discard button', async () => {
    const { chat, deps } = makeChat(['hang']);
    await chat.show({ ...STATEMENT, csvContextId: 'ctx-old' });
    await ask('Slow one?');
    await chat.show(STATEMENT); // now offering a new conversation for the active statement

    expect(chat.hasPendingAnswer).toBe(true);
    expect(deps.onPendingChange).toHaveBeenLastCalledWith(true);
    expect($<HTMLButtonElement>('#chat-new').disabled).toBe(true);
    expect($('#chat-pending-note').hidden).toBe(false);
    expect($('#chat-pending-note').textContent).toBe(
      'An answer is still being prepared. Wait for it, or discard it to start over.',
    );
    expect($('#chat-discard').hidden).toBe(false);
    expect($('#chat-discard').textContent).toBe('Discard this question (it still counts toward your limit)');
  });

  it('Discard clears the pending entry, puts the question back, and unblocks starting over', async () => {
    const { chat, deps } = makeChat(['hang']);
    await chat.show(STATEMENT);
    await ask('Slow one?');

    $<HTMLButtonElement>('#chat-discard').click();
    await settle();

    expect(await getPendingAsk()).toBeNull();
    expect(chat.hasPendingAnswer).toBe(false);
    expect(deps.onPendingChange).toHaveBeenLastCalledWith(false);
    expect($<HTMLTextAreaElement>('#chat-input').value).toBe('Slow one?');
    expect($('#chat-log').querySelector('.chat-question')).toBeNull();
    expect($<HTMLButtonElement>('#chat-new').disabled).toBe(false);
    expect($('#chat-discard').hidden).toBe(true);
    expect($('#chat-pending-note').hidden).toBe(true);
    expect($('#chat-waiting').hidden).toBe(true);
    expect($<HTMLButtonElement>('#chat-send').disabled).toBe(false);
  });

  it('Discard is offered in the "check back later" state too', async () => {
    const outage = Array.from({ length: 100 }, () => gateway(502));
    const { chat } = makeChat(outage);
    await chat.show(STATEMENT);
    await ask('What was revenue?');
    expect($('#chat-notice').textContent).toBe('Still not finished — check back later.');
    expect($('#chat-discard').hidden).toBe(false);

    $<HTMLButtonElement>('#chat-discard').click();
    await settle();
    expect(await getPendingAsk()).toBeNull();
    expect($('#chat-check').hidden).toBe(true);
  });

  it('a reopened panel with a pending entry shows the same blocked state', async () => {
    const { chat, deps } = makeChat(['hang']);
    await setPendingAsk({
      requestId: 'req-from-before', question: 'Earlier?', csvContextId: 'ctx-1', conversationId: null, startedAt: 1_000_000,
    });
    await chat.show(STATEMENT);
    await settle();

    expect(deps.onPendingChange).toHaveBeenLastCalledWith(true);
    expect($<HTMLButtonElement>('#chat-new').disabled).toBe(true);
    expect($('#chat-pending-note').hidden).toBe(false);
    expect($('#chat-discard').hidden).toBe(false);
  });

  it('an answer arriving lifts the block', async () => {
    const { chat, deps } = makeChat([answer()]);
    await chat.show(STATEMENT);
    await ask('Q?');
    expect(deps.onPendingChange).toHaveBeenLastCalledWith(false);
    expect($('#chat-discard').hidden).toBe(true);
  });

  it('an empty question is not sent', async () => {
    const { chat, send } = makeChat([]);
    await chat.show(STATEMENT);
    await ask('   ');
    expect(send).not.toHaveBeenCalled();
    expect($('#chat-notice').textContent).toBe('Type a question first.');
  });
});
