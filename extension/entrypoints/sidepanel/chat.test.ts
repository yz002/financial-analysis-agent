// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeBrowser } from 'wxt/testing/fake-browser';
import { BackendApiError, type AskRequestBody, type AskResponse, type Citation } from '../../lib/backendApi';
import {
  clearChatState,
  clearPendingAsk,
  getConversation,
  getPendingAsk,
  onChatStateChanged,
  setConversation,
  setPendingAsk,
} from '../../lib/conversationStorage';
import { onActiveStatementChanged, setActiveStatement, type ActiveStatement } from '../../lib/sessionStorage';
import { ChatController, NEW_CONVERSATION_ELSEWHERE_NOTE, renderAnswer, type ChatDeps } from './chat';

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

/** A chat panel built into `root` (the whole document by default; a second window gets its own root). */
function makeChat(responses: Array<AskResponse | Error | 'hang'>, root: HTMLElement = document.body): Harness {
  root.innerHTML = PANEL;
  const $ = <T extends HTMLElement>(selector: string) => root.querySelector<T>(selector)!;
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

  it('a figure repeated in the answer is listed once in the sources, with its count', () => {
    const final_answer = 'Revenue was 1.25 million; yes, 1.25 million.';
    const second = final_answer.lastIndexOf('1.25 million');
    const first = answer().citations[0]!;
    const article = renderAnswer(answer({
      final_answer,
      citations: [first, { ...first, figure_index: 1, start: second, end: second + 12 }],
    }));
    expect(article.querySelectorAll('.chat-answer-text .figure')).toHaveLength(2); // both still marked
    expect(article.querySelectorAll('.chat-sources li.chat-source')).toHaveLength(1);
    expect(article.querySelector('.chat-source-count')!.textContent).toBe('×2 mentions');
    expect(renderAnswer(answer()).querySelector('.chat-source-count')).toBeNull();
  });

  it('lists where each figure comes from, with the cell in its own element', () => {
    const article = renderAnswer(answer());
    expect(article.querySelector('.chat-sources .ref')!.textContent).toBe("'P&L'!B4");
    expect(article.querySelector('.chat-sources')!.textContent).toContain('= 1250 (as read, in thousands)');
  });
});

describe('renderAnswer: light markdown, built from elements and text nodes only', () => {
  /** A citation for the first `raw` at or after `from` in `text`. */
  function cite(text: string, raw: string, status: Citation['status'] = 'untraced', from = 0): Citation {
    const start = text.indexOf(raw, from);
    if (start === -1) throw new Error(`"${raw}" not in answer`);
    return { figure_index: 0, raw_text: raw, start, end: start + raw.length, status, matches: [] };
  }

  function render(final_answer: string, citations: Citation[] = []): HTMLElement {
    const article = renderAnswer(answer({ final_answer, citations, figure_check: { figures: [] } }));
    document.body.append(article);
    return article;
  }

  const text = (article: HTMLElement) => article.querySelector('.chat-answer-text')!;

  it('script tags, onerror images and javascript: links stay literal text, with no element created', () => {
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    const hostile = [
      '<script>alert(1)</script>',
      '<img src=x onerror=alert(1)>',
      '[x](javascript:alert(1))',
    ];
    const final_answer =
      `${hostile[0]}\n\n**${hostile[1]}**\n\n- ${hostile[2]}\n\n| A | B |\n|---|---|\n| ${hostile[0]} | ${hostile[2]} |`;
    const article = render(final_answer);

    expect(article.querySelectorAll('script, img, a, iframe, object, embed')).toHaveLength(0);
    for (const literal of hostile) expect(article.textContent).toContain(literal);
    expect(article.querySelector('strong')!.textContent).toBe(hostile[1]);
    expect(article.querySelector('li')!.textContent).toBe(hostile[2]);
    expect(alertSpy).not.toHaveBeenCalled();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('bold, italic, inline code, lists and line breaks become elements', () => {
    const article = render('Revenue was **up** and *steady*, see `B4`.\nSecond line\n\n- one\n- two\n\n1. first\n2. second');
    const answerText = text(article);
    expect(answerText.querySelector('strong')!.textContent).toBe('up');
    expect(answerText.querySelector('em')!.textContent).toBe('steady');
    expect(answerText.querySelector('code')!.textContent).toBe('B4');
    const p = answerText.querySelector('p')!;
    expect(p.querySelectorAll('br')).toHaveLength(1);
    expect(p.textContent).toBe('Revenue was up and steady, see B4.Second line');
    expect([...answerText.querySelectorAll('ul > li')].map((li) => li.textContent)).toEqual(['one', 'two']);
    expect([...answerText.querySelectorAll('ol > li')].map((li) => li.textContent)).toEqual(['first', 'second']);
    expect(answerText.textContent).not.toContain('\n');
  });

  it('an unclosed ** or ` stays literal', () => {
    const article = render('Revenue **grew and see `B4');
    expect(text(article).querySelectorAll('strong, em, code')).toHaveLength(0);
    expect(text(article).textContent).toBe('Revenue **grew and see `B4');
  });

  it('"revenue * margin", "2 * 3" and a "-4.6%" line stay as written', () => {
    const final_answer = 'revenue * margin and 2 * 3\n-4.6% was the margin\n-$120 was the loss';
    const article = render(final_answer, [cite(final_answer, '-4.6%')]);
    expect(text(article).querySelectorAll('em, strong, ul, ol')).toHaveLength(0);
    expect(text(article).textContent).toBe('revenue * margin and 2 * 3-4.6%Not traced to your data was the margin-$120 was the loss');
    expect(text(article).querySelector('.figure')!.textContent).toBe('-4.6%');
  });

  it('a figure inside bold keeps its span, status class and marker', () => {
    const final_answer = 'Revenue was **1.25 million** this quarter.';
    const article = render(final_answer, [cite(final_answer, '1.25 million')]);
    const figure = text(article).querySelector('strong > .figure')!;
    expect(figure.className).toBe('figure figure--untraced');
    expect(figure.textContent).toBe('1.25 million');
    expect(figure.nextElementSibling!.className).toBe('figure-marker');
    expect(figure.nextElementSibling!.textContent).toBe('Not traced to your data');
  });

  it('a figure inside a table cell and a list item keeps its mark; the sources list is unchanged', () => {
    const final_answer = '| Quarter | Revenue |\n|---|---|\n| Q1 | 1.25 million |\n\n- Margin was 40.0%';
    const citations = [
      { ...cite(final_answer, '1.25 million', 'traced'), matches: answer().citations[0]!.matches },
      cite(final_answer, '40.0%'),
    ];
    const article = render(final_answer, citations);

    const cellFigure = text(article).querySelector('table td .figure')!;
    expect(cellFigure.textContent).toBe('1.25 million');
    expect(cellFigure.className).toBe('figure figure--traced');
    expect(text(article).querySelector('li .figure--untraced')!.textContent).toBe('40.0%');
    // The sources still come from the citations, as before.
    const sources = [...article.querySelectorAll('.chat-sources li.chat-source .chat-source-figure')].map((s) => s.textContent);
    expect(sources).toEqual(['1.25 million', '40.0%']);
    expect(article.querySelector('.chat-sources .ref')!.textContent).toBe("'P&L'!B4");
  });

  it('a figure that straddles a ** keeps its text; those stars stay literal', () => {
    const final_answer = 'Revenue **1.25** million.';
    const article = render(final_answer, [cite(final_answer, '1.25** million')]);
    expect(text(article).querySelector('strong')).toBeNull();
    expect(text(article).querySelector('.figure')!.textContent).toBe('1.25** million');
    expect(text(article).textContent).toContain('Revenue **');
  });

  it('a ragged table pads short rows and keeps a long row\'s extra cells as literal text', () => {
    const article = render('| A | B | C |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |');
    const rows = [...text(article).querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent));
    expect(rows).toEqual([
      ['1', '', ''],
      ['1', '2', '3 | 4'],
    ]);
    expect([...text(article).querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['A', 'B', 'C']);
  });

  it('a figure across a line break falls back to the plain answer, marked as before', () => {
    const final_answer = 'Revenue was **1.25\nmillion**.';
    const article = render(final_answer, [cite(final_answer, '1.25\nmillion')]);
    const answerText = text(article);
    expect(answerText.tagName).toBe('P');
    expect(answerText.classList.contains('chat-answer-text--formatted')).toBe(false);
    expect(answerText.textContent).toBe('Revenue was **1.25\nmillionNot traced to your data**.');
    expect(answerText.querySelector('.figure')!.textContent).toBe('1.25\nmillion');
  });

  it('the banner and the stored answer are untouched by the formatting', () => {
    const final_answer = '**Revenue** was 1.25 million.';
    const response = answer({
      final_answer,
      citations: [{ ...answer().citations[0]!, start: final_answer.indexOf('1.25'), end: final_answer.indexOf('1.25') + 12 }],
    });
    const before = JSON.stringify(response);
    const article = renderAnswer(response);
    expect(JSON.stringify(response)).toBe(before); // final_answer and citations not modified
    expect(article.querySelector('.figure-check')!.textContent).toBe(
      renderAnswer(answer()).querySelector('.figure-check')!.textContent,
    );
  });
});

describe('ChatController', () => {
  it('needs a confirmed statement: hidden without one, shown with one', async () => {
    const { chat } = makeChat([]);
    await chat.show(null);
    expect($('#chat-panel').hidden).toBe(true);
    await chat.show(STATEMENT);
    expect($('#chat-panel').hidden).toBe(false);
    const when = new Date(STATEMENT.confirmedAt).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    expect($('#chat-statement').textContent).toBe(
      `Asking about Acme — Q3 P&L · 'P&L'!A3:C9 · numbers in thousands · confirmed ${when}. ` +
        'Answers use this statement, whichever tab is open.',
    );
    expect($('#chat-statement .chat-about-label').textContent).toBe('Asking about');
    expect($('#chat-statement .chat-about-source').textContent).toContain("Q3 P&L · 'P&L'!A3:C9");
  });

  it('the about line shows a statement name that looks like markup as text', async () => {
    const { chat } = makeChat([]);
    await chat.show({ ...STATEMENT, entityName: MARKUP, label: `${MARKUP} · 'P&L'!A1:B2` });
    expect($('#chat-statement').querySelectorAll('img, script')).toHaveLength(0);
    expect($('#chat-statement').textContent).toContain(MARKUP);
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
    expect($('#chat-discard').textContent).toBe('Discard this question (it may still count toward your limit)');
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


describe('ChatController.syncFromStorage (another window)', () => {
  const OTHER_PENDING = {
    requestId: 'req-other-window', question: 'From elsewhere?', csvContextId: 'ctx-1', conversationId: null, startedAt: 1_000_000,
  };

  it("a question pending in another window blocks here too, and isn't resent from here", async () => {
    const { chat, send, deps } = makeChat([]);
    await chat.show(STATEMENT);
    await setPendingAsk(OTHER_PENDING);

    await chat.syncFromStorage();

    expect(send).not.toHaveBeenCalled();
    expect(deps.onPendingChange).toHaveBeenLastCalledWith(true);
    expect($('#chat-notice').textContent).toContain('being prepared in another window');
    expect($('#chat-check').hidden).toBe(false);
    expect($('#chat-discard').hidden).toBe(false);
    expect($<HTMLButtonElement>('#chat-new').disabled).toBe(true);
  });

  it('when the other window clears it, the block lifts and its answer appears', async () => {
    const { chat, deps } = makeChat([]);
    await chat.show(STATEMENT);
    await setPendingAsk(OTHER_PENDING);
    await chat.syncFromStorage();

    await setConversation({
      csvContextId: 'ctx-1', statementLabel: 'Acme', conversationId: 'conv-1',
      messages: [{ role: 'question', text: 'From elsewhere?' }, { role: 'answer', response: answer() }],
    });
    await clearPendingAsk();
    await chat.syncFromStorage();

    expect(deps.onPendingChange).toHaveBeenLastCalledWith(false);
    expect($('#chat-notice').textContent).toBe('');
    expect($('#chat-check').hidden).toBe(true);
    expect($('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1);
  });

  it("this panel's own pending question doesn't look like another window's", async () => {
    const { chat, send } = makeChat(['hang']);
    await chat.show(STATEMENT);
    await ask('Mine?');
    await chat.syncFromStorage();

    expect(send).toHaveBeenCalledTimes(1);
    expect($('#chat-notice').textContent).not.toContain('another window');
  });

  it('a new conversation started in another window replaces the transcript with a notice', async () => {
    const { chat } = makeChat([answer()]);
    await chat.show(STATEMENT);
    await ask('Q?');
    expect($('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1);

    // Another window confirmed a statement: its own fresh conversation, with its own id.
    await setConversation({
      localId: 'other-window', csvContextId: 'ctx-1', statementLabel: 'Acme', conversationId: null, messages: [],
    });
    await chat.syncFromStorage();

    expect($('#chat-log').children).toHaveLength(0);
    expect($('#chat-notice').textContent).toBe(NEW_CONVERSATION_ELSEWHERE_NOTE);
  });

  it("no notice for this panel's own new conversation, more messages in the same one, or an empty transcript", async () => {
    const { chat } = makeChat([answer(), answer()]);
    await chat.show(STATEMENT);
    await chat.syncFromStorage(); // nothing shown yet
    expect($('#chat-notice').textContent).toBe('');

    await ask('Q?');
    const sameConversation = (await getConversation())!;
    await setConversation({
      ...sameConversation,
      messages: [...sameConversation.messages, { role: 'question', text: 'Elsewhere?' }],
    });
    await chat.syncFromStorage(); // another window added to the same conversation
    expect($('#chat-notice').textContent).toBe('');

    await chat.startNewConversation(STATEMENT);
    await chat.syncFromStorage(); // this panel's own new conversation
    expect($('#chat-notice').textContent).toBe('');
  });

  it('the notice clears on the next question', async () => {
    const { chat } = makeChat([answer(), answer()]);
    await chat.show(STATEMENT);
    await ask('Q?');
    await setConversation({
      localId: 'other-window', csvContextId: 'ctx-1', statementLabel: 'Acme', conversationId: null, messages: [],
    });
    await chat.syncFromStorage();
    expect($('#chat-notice').textContent).toBe(NEW_CONVERSATION_ELSEWHERE_NOTE);

    await ask('Next?');
    expect($('#chat-notice').textContent).toBe('');
  });

  it('a conversation replaced by another window for another statement shows the New conversation offer', async () => {
    const { chat } = makeChat([]);
    await chat.show(STATEMENT);
    await setConversation({
      csvContextId: 'ctx-elsewhere', statementLabel: 'Other Co · Sheet1!A1:B9 · numbers in millions',
      conversationId: 'conv-x', messages: [],
    });

    await chat.syncFromStorage();

    expect($('#chat-mismatch').hidden).toBe(false);
    expect($('#chat-mismatch-text').textContent).toContain('Other Co');
  });

  it('the New conversation offer tells apart two statements from the same range by scale', async () => {
    const { chat } = makeChat([answer()]);
    await chat.show({ ...STATEMENT, csvContextId: 'ctx-thousands', scale: 'thousands' });
    await ask('Q?');
    await chat.show({ ...STATEMENT, csvContextId: 'ctx-millions', scale: 'millions' });

    const text = $('#chat-mismatch-text').textContent!;
    expect(text).toContain('numbers in thousands');
    expect(text).toContain('numbers in millions');
  });

  it("the chat line doesn't repeat a business name that is also the file name", async () => {
    const { chat } = makeChat([]);
    await chat.show({ ...STATEMENT, entityName: 'FA Spike Test', label: "FA Spike Test · 'P&L'!A3:G7" });
    expect($('#chat-statement').textContent).not.toContain('FA Spike Test — FA Spike Test');
  });
});

describe('a new conversation started in another window (F1)', () => {
  const NEXT: ActiveStatement = { ...STATEMENT, csvContextId: 'ctx-2', confirmedAt: '2026-10-06T00:00:00Z' };

  interface PanelWindow extends Harness {
    root: HTMLElement;
    /** main.ts's renderStatementCard: the card follows the statement, and the chat with it. */
    showStatement(statement: ActiveStatement | null): void;
    in<T extends HTMLElement>(selector: string): T;
  }

  /**
   * One side panel, wired to storage the way main.ts wires it: the real onActiveStatementChanged
   * (skipping the statement the card already shows) calling chat.show, and the real
   * onChatStateChanged calling syncFromStorage.
   */
  function openWindow(responses: Array<AskResponse | Error | 'hang'>): PanelWindow {
    const root = document.createElement('div');
    document.body.append(root);
    const harness = makeChat(responses, root);
    let displayedStatementId: string | null = null;
    const showStatement = (statement: ActiveStatement | null) => {
      displayedStatementId = statement?.csvContextId ?? null;
      void harness.chat.show(statement);
    };
    onActiveStatementChanged((statement) => {
      if ((statement?.csvContextId ?? null) === displayedStatementId) return; // this panel's own write
      showStatement(statement);
    });
    onChatStateChanged(() => {
      void harness.chat.syncFromStorage();
    });
    return {
      ...harness,
      root,
      showStatement,
      in: <T extends HTMLElement>(selector: string) => root.querySelector<T>(selector)!,
    };
  }

  async function askIn(panel: PanelWindow, question: string): Promise<void> {
    panel.in<HTMLTextAreaElement>('#chat-input').value = question;
    panel.in<HTMLFormElement>('#chat-form').requestSubmit();
    await settle();
  }

  /** main.ts's statementConfirmed, in order: the statement, a new conversation, the card. */
  async function confirmIn(panel: PanelWindow, statement: ActiveStatement): Promise<void> {
    await setActiveStatement(statement);
    await panel.chat.startNewConversation(statement);
    panel.showStatement(statement);
    await settle();
  }

  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('two windows, real storage events: a confirm in window 1 clears window 2 with the notice', async () => {
    const window1 = openWindow([]);
    const window2 = openWindow([answer()]);
    await setActiveStatement(STATEMENT);
    await settle();

    await askIn(window2, 'What was revenue?');
    expect(window2.in('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1);
    expect(window1.in('#chat-log').querySelectorAll('.chat-answer')).toHaveLength(1); // synced

    await confirmIn(window1, NEXT);

    expect(window2.in('#chat-log').children).toHaveLength(0);
    expect(window2.in('#chat-notice').textContent).toBe(NEW_CONVERSATION_ELSEWHERE_NOTE);
    expect(window1.in('#chat-log').children).toHaveLength(0);
    expect(window1.in('#chat-notice').textContent).toBe('');
    expect(await getConversation()).toEqual(expect.objectContaining({ csvContextId: 'ctx-2', messages: [] }));

    await askIn(window2, 'And now?'); // the notice clears on window 2's next question
    expect(window2.in('#chat-notice').textContent).toBe('');
  });

  it("this panel's own confirm shows no notice, its own empty step included (real storage events)", async () => {
    const panel = openWindow([answer()]);
    await setActiveStatement(STATEMENT);
    await settle();
    await askIn(panel, 'What was revenue?');

    await confirmIn(panel, NEXT);

    expect(panel.in('#chat-log').children).toHaveLength(0);
    expect(panel.in('#chat-notice').textContent).toBe('');
  });

  // The same confirm, with this panel's two reactions (show for the statement change, sync for
  // each chat-state change) driven by hand, in each order relative to the other window's writes.
  const otherWindowsConversation = {
    localId: 'other-window', csvContextId: 'ctx-2', statementLabel: 'Acme', conversationId: null, messages: [],
  };
  it.each<[string, (chat: ChatController) => Promise<void>]>([
    ['show before the clear', async (chat) => {
      await chat.show(NEXT);
      await clearChatState();
      await chat.syncFromStorage();
      await setConversation(otherWindowsConversation);
      await chat.syncFromStorage();
    }],
    ['show between the clear and the new conversation', async (chat) => {
      await clearChatState();
      await chat.syncFromStorage();
      await chat.show(NEXT);
      await setConversation(otherWindowsConversation);
      await chat.syncFromStorage();
    }],
    ['show after the new conversation', async (chat) => {
      await clearChatState();
      await chat.syncFromStorage();
      await setConversation(otherWindowsConversation);
      await chat.syncFromStorage();
      await chat.show(NEXT);
    }],
  ])('hand-driven order, %s: the transcript clears with the notice', async (_order, steps) => {
    const { chat } = makeChat([answer()]);
    await chat.show(STATEMENT);
    await ask('What was revenue?');

    await steps(chat);

    expect($('#chat-log').children).toHaveLength(0);
    expect($('#chat-notice').textContent).toBe(NEW_CONVERSATION_ELSEWHERE_NOTE);
  });

  it('no notice after a sign-out reset', async () => {
    const { chat } = makeChat([answer()]);
    await chat.show(STATEMENT);
    await ask('What was revenue?');

    chat.reset();
    await setConversation(otherWindowsConversation);
    await chat.show(NEXT);
    await chat.syncFromStorage();

    expect($('#chat-notice').textContent).toBe('');
  });

  it('no notice for a conversation stored without a localId', async () => {
    const { chat } = makeChat([answer()]);
    await chat.show(STATEMENT);
    await ask('What was revenue?');

    await setConversation({ csvContextId: 'ctx-2', statementLabel: 'Acme', conversationId: null, messages: [] });
    await chat.syncFromStorage();

    expect($('#chat-log').children).toHaveLength(0);
    expect($('#chat-notice').textContent).toBe('');
  });
});
