import type { AskResponse } from './backendApi';

/**
 * Chat state (Phase D session 5), in chrome.storage.session only -- never .local or .sync:
 * answer text never goes to disk. It survives closing and reopening the side panel, and a
 * Chrome restart starts a fresh conversation. clearStoredSession() (lib/sessionStorage.ts)
 * clears it with the session, so nothing carries over to whoever signs in next.
 *
 * - The conversation: which statement it's bound to, the backend's conversation_id once the
 *   first answer arrives, and the transcript shown in the panel.
 * - The pending question: the request_id of a question still waiting for its answer, so a
 *   panel reopened mid-answer resends the same request_id (never a second charge).
 */

export type ChatMessage =
  | { role: 'question'; text: string }
  | { role: 'answer'; response: AskResponse }
  | { role: 'notice'; text: string };

export interface StoredConversation {
  csvContextId: string;
  statementLabel: string;
  conversationId: string | null;
  messages: ChatMessage[];
}

export interface PendingAsk {
  requestId: string;
  question: string;
  csvContextId: string;
  conversationId: string | null;
  startedAt: number; // epoch ms, when the question was first sent
}

const CONVERSATION_KEY = 'fa_conversation';
const PENDING_ASK_KEY = 'fa_pending_ask';

export async function getConversation(): Promise<StoredConversation | null> {
  const result = await browser.storage.session.get(CONVERSATION_KEY);
  return (result[CONVERSATION_KEY] as StoredConversation | undefined) ?? null;
}

export async function setConversation(conversation: StoredConversation): Promise<void> {
  await browser.storage.session.set({ [CONVERSATION_KEY]: conversation });
}

export async function getPendingAsk(): Promise<PendingAsk | null> {
  const result = await browser.storage.session.get(PENDING_ASK_KEY);
  return (result[PENDING_ASK_KEY] as PendingAsk | undefined) ?? null;
}

export async function setPendingAsk(pending: PendingAsk): Promise<void> {
  await browser.storage.session.set({ [PENDING_ASK_KEY]: pending });
}

export async function clearPendingAsk(): Promise<void> {
  await browser.storage.session.remove(PENDING_ASK_KEY);
}

/**
 * Subscribes to changes of the conversation or the pending question in chrome.storage.session,
 * so every open panel shows the same chat and the same pending-answer block (Phase D session
 * 5). The callback re-reads storage itself; this panel's own writes trigger it too.
 */
export function onChatStateChanged(callback: () => void): void {
  browser.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== 'session') return;
    if (CONVERSATION_KEY in changes || PENDING_ASK_KEY in changes) callback();
  });
}

/** Both the conversation and any pending question. */
export async function clearChatState(): Promise<void> {
  await browser.storage.session.remove([CONVERSATION_KEY, PENDING_ASK_KEY]);
}
