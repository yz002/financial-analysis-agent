import { runAsk, type AskFailure } from '../../lib/askRunner';
import type { AskRequestBody, AskResponse, Citation } from '../../lib/backendApi';
import {
  ANSWER_PENDING_NOTE,
  describeCitation,
  DISCARD_LABEL,
  failureMessage,
  figureCheckBanner,
  ITERATION_CAP_NOTICE,
  QUESTION_MAX_LENGTH,
  segmentAnswer,
  type SourceLine,
} from '../../lib/chatModel';
import {
  clearChatState,
  clearPendingAsk,
  getConversation,
  getPendingAsk,
  setConversation,
  setPendingAsk,
  type ChatMessage,
  type PendingAsk,
  type StoredConversation,
} from '../../lib/conversationStorage';
import type { ActiveStatement, StoredSession } from '../../lib/sessionStorage';

/**
 * The chat panel (Phase D session 5): questions about the active statement, POST /v1/ask with
 * conversation continuity and request_id replay (lib/askRunner.ts), and answers rendered with
 * their figure check and citations.
 *
 * Every piece of text from the backend or the person's spreadsheet -- the answer, figures,
 * citation cells, read values, column names, statement labels -- reaches the DOM through
 * textContent or a text node, never innerHTML (renderAnswer below; tested with injected markup).
 */

export interface ChatElements {
  panel: HTMLElement;
  statementLine: HTMLElement;
  mismatch: HTMLElement;
  mismatchText: HTMLElement;
  newConversationButton: HTMLButtonElement;
  log: HTMLElement;
  notice: HTMLElement;
  waiting: HTMLElement;
  waitingText: HTMLElement;
  stopButton: HTMLButtonElement;
  checkButton: HTMLButtonElement;
  /** "Discard this question": shown while waiting and in the "check back later" state. */
  discardButton: HTMLButtonElement;
  /** The plain line saying why starting over is blocked while an answer is pending. */
  pendingNote: HTMLElement;
  form: HTMLFormElement;
  input: HTMLTextAreaElement;
  sendButton: HTMLButtonElement;
}

export interface ChatDeps {
  getSession(): Promise<StoredSession | null>;
  send(sessionToken: string, body: AskRequestBody, signal: AbortSignal): Promise<AskResponse>;
  /** A 401: end the session (which also clears the chat) and show the signed-out UI. */
  onUnauthorized(): Promise<void>;
  /** The statement can't be used any more: clear it so the person re-reads and re-confirms. */
  onStatementNeedsReconfirm(message: string): Promise<void>;
  /**
   * Whether an answer is pending (waiting, stopped, or "check back later"). The panel blocks
   * Confirm mapping while it is: confirming starts a new conversation, which would abandon a
   * question that still counts.
   */
  onPendingChange(pending: boolean): void;
  newRequestId(): string;
  now(): number;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function sourceList(lines: SourceLine[]): HTMLUListElement {
  const list = element('ul', 'chat-source-lines');
  for (const line of lines) {
    const item = element('li');
    if (line.ref) {
      item.append(element('span', 'ref', line.ref), document.createTextNode(' '));
    }
    item.append(document.createTextNode(line.text));
    if (line.children.length) item.append(sourceList(line.children));
    list.append(item);
  }
  return list;
}

function citationItem(citation: Citation): HTMLLIElement {
  const view = describeCitation(citation);
  const item = element('li', `chat-source chat-source--${view.status}`);
  item.append(element('span', 'chat-source-figure', view.figure));
  if (view.statusLabel) item.append(element('span', 'chat-source-status', view.statusLabel));
  if (view.lines.length) item.append(sourceList(view.lines));
  return item;
}

/**
 * One answer: the figure-check banner, the iteration-cap notice when it applies, the answer
 * text with each checked figure marked, and the sources behind every figure.
 */
export function renderAnswer(response: AskResponse): HTMLElement {
  const article = element('article', 'chat-answer');
  const citations = response.citations ?? [];

  const banner = figureCheckBanner(response.figure_check ?? {}, citations);
  article.append(element('p', `figure-check figure-check--${banner.tone}`, banner.text));
  if (response.hit_iteration_cap) {
    const notice = element('p', 'chat-cap-notice', ITERATION_CAP_NOTICE);
    notice.setAttribute('role', 'note');
    article.append(notice);
  }

  const text = element('p', 'chat-answer-text');
  for (const segment of segmentAnswer(response.final_answer, citations)) {
    if (segment.kind === 'text') {
      text.append(document.createTextNode(segment.text));
      continue;
    }
    const view = describeCitation(segment.citation);
    text.append(element('span', `figure figure--${view.status}`, segment.text));
    if (view.statusLabel) text.append(element('span', 'figure-marker', view.statusLabel));
  }
  article.append(text);

  const cited = citations.filter((c) => c.matches.length || c.status !== 'traced');
  if (cited.length) {
    const sources = element('section', 'chat-sources');
    sources.append(element('h3', undefined, 'Where the figures come from'));
    const list = element('ol');
    list.append(...cited.map(citationItem));
    sources.append(list);
    article.append(sources);
  }
  return article;
}

function renderMessage(message: ChatMessage): HTMLElement {
  if (message.role === 'question') return element('p', 'chat-question', message.text);
  if (message.role === 'notice') return element('p', 'chat-message-notice', message.text);
  return renderAnswer(message.response);
}

function freshConversation(statement: ActiveStatement): StoredConversation {
  return {
    csvContextId: statement.csvContextId,
    statementLabel: `${statement.entityName} — ${statement.label}`,
    conversationId: null,
    messages: [],
  };
}

const WAITING_TEXT = 'Answering… this can take a few minutes. You can close the panel; the answer is kept.';

export class ChatController {
  private statement: ActiveStatement | null = null;
  private conversation: StoredConversation | null = null;
  private running: AbortController | null = null;
  private pending: PendingAsk | null = null;

  constructor(
    private readonly el: ChatElements,
    private readonly deps: ChatDeps,
  ) {
    el.input.maxLength = QUESTION_MAX_LENGTH;
    el.form.addEventListener('submit', (event) => {
      event.preventDefault();
      void this.submit();
    });
    el.stopButton.addEventListener('click', () => this.running?.abort());
    el.checkButton.addEventListener('click', () => void this.checkAgain());
    el.discardButton.textContent = DISCARD_LABEL;
    el.discardButton.addEventListener('click', () => void this.discard());
    el.newConversationButton.addEventListener('click', () => {
      if (this.statement) void this.startNewConversation(this.statement);
    });
  }

  /** Show the chat for `statement`, or hide it when there's none: chat needs a confirmed statement. */
  async show(statement: ActiveStatement | null): Promise<void> {
    this.statement = statement;
    if (!statement) {
      this.el.panel.setAttribute('hidden', '');
      return;
    }
    this.el.statementLine.textContent = `Answers come from ${statement.entityName} — ${statement.label}.`;
    this.el.panel.removeAttribute('hidden');

    const stored = await getConversation();
    this.conversation = stored ?? freshConversation(statement);
    this.renderTranscript();
    this.renderMismatch();

    if (!this.running) {
      // A reopened panel with a question still pending: blocked state first, then resume.
      const pending = await getPendingAsk();
      this.setPending(pending);
      if (pending) void this.run(pending);
    }
  }

  /** True while an answer is pending: starting a new conversation is blocked. */
  get hasPendingAnswer(): boolean {
    return this.pending !== null;
  }

  /** A newly confirmed statement starts a new conversation (any unanswered question is dropped). */
  async startNewConversation(statement: ActiveStatement): Promise<void> {
    this.running?.abort();
    this.running = null;
    await clearChatState();
    this.setPending(null);
    this.statement = statement;
    this.conversation = freshConversation(statement);
    await setConversation(this.conversation);
    this.setNotice('');
    this.el.checkButton.hidden = true;
    this.setWaiting(false);
    this.renderTranscript();
    this.renderMismatch();
  }

  /** Signed out: stop waiting and clear the panel. Storage is cleared by clearStoredSession. */
  reset(): void {
    this.running?.abort();
    this.running = null;
    this.statement = null;
    this.conversation = null;
    this.setPending(null);
    this.el.log.replaceChildren();
    this.el.input.value = '';
    this.setNotice('');
    this.el.checkButton.hidden = true;
    this.setWaiting(false);
    this.el.mismatch.setAttribute('hidden', '');
    this.el.panel.setAttribute('hidden', '');
  }

  private renderTranscript(): void {
    this.el.log.replaceChildren(...(this.conversation?.messages ?? []).map(renderMessage));
  }

  /**
   * Offers a new conversation when the stored conversation belongs to a statement that's no
   * longer the active one, or (`force`) when the backend refused a question as
   * statement_mismatch. Never switches by itself.
   */
  private renderMismatch(force = false): void {
    const conversation = this.conversation;
    const statement = this.statement;
    if (statement && conversation && (force || conversation.csvContextId !== statement.csvContextId)) {
      this.el.mismatchText.textContent =
        `This conversation is about ${conversation.statementLabel}. Start a new conversation ` +
        `about ${statement.entityName} — ${statement.label}?`;
      this.el.mismatch.removeAttribute('hidden');
    } else {
      this.el.mismatch.setAttribute('hidden', '');
    }
  }

  private setPending(pending: PendingAsk | null): void {
    this.pending = pending;
    const blocked = pending !== null;
    this.el.newConversationButton.disabled = blocked;
    this.el.pendingNote.textContent = blocked ? ANSWER_PENDING_NOTE : '';
    this.el.pendingNote.hidden = !blocked;
    this.el.discardButton.hidden = !blocked;
    this.deps.onPendingChange(blocked);
  }

  private setNotice(text: string): void {
    this.el.notice.textContent = text;
  }

  private setWaiting(waiting: boolean): void {
    this.el.waiting.hidden = !waiting;
    this.el.waitingText.textContent = waiting ? WAITING_TEXT : '';
    this.el.sendButton.disabled = waiting;
    this.el.input.disabled = waiting;
  }

  private async save(): Promise<void> {
    if (this.conversation) await setConversation(this.conversation);
  }

  private async submit(): Promise<void> {
    const question = this.el.input.value.trim();
    const statement = this.statement;
    const conversation = this.conversation;
    if (!statement || !conversation || this.running || this.pending) return;
    if (!question) {
      this.setNotice('Type a question first.');
      return;
    }
    if (question.length > QUESTION_MAX_LENGTH) {
      this.setNotice(failureMessage({ kind: 'invalid_question' }));
      return;
    }

    const pending: PendingAsk = {
      requestId: this.deps.newRequestId(),
      question,
      // The active statement. If the conversation is bound to a different one, the backend
      // answers statement_mismatch rather than silently switching.
      csvContextId: statement.csvContextId,
      conversationId: conversation.conversationId,
      startedAt: this.deps.now(),
    };
    conversation.messages.push({ role: 'question', text: question });
    await this.save();
    await setPendingAsk(pending);
    this.setPending(pending);
    this.el.input.value = '';
    this.renderTranscript();
    await this.run(pending);
  }

  private async checkAgain(): Promise<void> {
    const pending = await getPendingAsk();
    if (pending && !this.running) await this.run(pending);
  }

  private async run(pending: PendingAsk): Promise<void> {
    const session = await this.deps.getSession();
    if (!session) return;
    const stop = new AbortController();
    this.running = stop;
    this.setNotice('');
    this.el.checkButton.hidden = true;
    this.setWaiting(true);

    const body: AskRequestBody = {
      question: pending.question,
      csv_context_id: pending.csvContextId,
      conversation_id: pending.conversationId,
      request_id: pending.requestId,
    };
    const outcome = await runAsk(
      body,
      pending.startedAt,
      {
        send: (b, signal) => this.deps.send(session.sessionToken, b, signal),
        sleep: this.deps.sleep,
        now: this.deps.now,
      },
      stop.signal,
    );
    if (this.running !== stop) return; // reset, or a new conversation, meanwhile
    this.running = null;
    this.setWaiting(false);

    if (outcome.kind === 'answered') {
      await clearPendingAsk();
      this.setPending(null);
      if (this.conversation) {
        this.conversation.conversationId = outcome.response.conversation_id;
        this.conversation.messages.push({ role: 'answer', response: outcome.response });
        await this.save();
      }
      this.renderTranscript();
      return;
    }
    await this.failed(outcome.failure, pending);
  }

  /** The question went unanswered: what to tell the person, and what to keep. */
  private async failed(failure: AskFailure, pending: PendingAsk): Promise<void> {
    switch (failure.kind) {
      case 'unauthorized':
        await this.deps.onUnauthorized();
        return;
      case 'still_running':
      case 'stopped':
        // The answer may still arrive: keep the pending entry and offer to check again.
        this.setNotice(failureMessage(failure));
        this.el.checkButton.hidden = false;
        return;
      case 'statement_needs_reconfirm':
        await clearChatState();
        this.setPending(null);
        this.conversation = null;
        this.el.log.replaceChildren();
        await this.deps.onStatementNeedsReconfirm(failureMessage(failure));
        return;
      default:
        break;
    }

    // Final: the question won't be answered. Put it back in the box so it's easy to ask again.
    await clearPendingAsk();
    this.setPending(null);
    this.dropUnansweredQuestion(pending.question);
    if (!this.el.input.value) this.el.input.value = pending.question;

    if (failure.kind === 'statement_mismatch') {
      this.renderMismatch(true);
      this.setNotice(failureMessage(failure));
    } else if (failure.kind === 'conversation_not_found') {
      if (this.statement) this.conversation = freshConversation(this.statement);
      this.setNotice(failureMessage(failure));
    } else {
      this.conversation?.messages.push({ role: 'notice', text: failureMessage(failure) });
    }
    await this.save();
    this.renderTranscript();
  }

  /**
   * Gives up on the pending question: stops waiting, clears the pending entry, and puts the
   * question back in the box. The backend may still finish it, and it still counts -- the
   * button's label says so.
   */
  private async discard(): Promise<void> {
    const pending = this.pending ?? (await getPendingAsk());
    this.running?.abort();
    this.running = null;
    this.setWaiting(false);
    await clearPendingAsk();
    this.setPending(null);
    this.el.checkButton.hidden = true;
    this.setNotice('');
    if (pending) {
      this.dropUnansweredQuestion(pending.question);
      if (!this.el.input.value) this.el.input.value = pending.question;
      await this.save();
      this.renderTranscript();
    }
  }

  /** Takes the last question back out of the transcript (it's put back in the input instead). */
  private dropUnansweredQuestion(question: string): void {
    const messages = this.conversation?.messages;
    const last = messages?.[messages.length - 1];
    if (last && last.role === 'question' && last.text === question) messages!.pop();
  }
}
