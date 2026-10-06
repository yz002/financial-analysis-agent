import { beforeEach, describe, expect, it } from 'vitest';
import { fakeBrowser } from 'wxt/testing/fake-browser';
import {
  clearChatState,
  clearPendingAsk,
  getConversation,
  getPendingAsk,
  setConversation,
  setPendingAsk,
  type PendingAsk,
  type StoredConversation,
} from './conversationStorage';

const CONVERSATION: StoredConversation = {
  csvContextId: 'ctx-1',
  statementLabel: 'Acme — Q3 P&L',
  conversationId: 'conv-1',
  messages: [{ role: 'question', text: 'What was revenue?' }],
};

const PENDING: PendingAsk = {
  requestId: 'req-1', question: 'What was revenue?', csvContextId: 'ctx-1', conversationId: null, startedAt: 123,
};

describe('conversationStorage', () => {
  beforeEach(() => {
    fakeBrowser.reset();
  });

  it('keeps the conversation and the pending question in storage.session only, never on disk', async () => {
    await setConversation(CONVERSATION);
    await setPendingAsk(PENDING);

    expect(await getConversation()).toEqual(CONVERSATION);
    expect(await getPendingAsk()).toEqual(PENDING);
    expect(await fakeBrowser.storage.session.get(null)).toEqual({
      fa_conversation: CONVERSATION,
      fa_pending_ask: PENDING,
    });
    expect(await fakeBrowser.storage.local.get(null)).toEqual({});
    expect(await fakeBrowser.storage.sync.get(null)).toEqual({});
  });

  it('clears the pending question on its own, and both together', async () => {
    await setConversation(CONVERSATION);
    await setPendingAsk(PENDING);

    await clearPendingAsk();
    expect(await getPendingAsk()).toBeNull();
    expect(await getConversation()).toEqual(CONVERSATION);

    await setPendingAsk(PENDING);
    await clearChatState();
    expect(await getPendingAsk()).toBeNull();
    expect(await getConversation()).toBeNull();
  });
});


describe('onChatStateChanged', () => {
  beforeEach(() => {
    fakeBrowser.reset();
  });

  it('fires for the conversation or the pending question in the session area only', async () => {
    const { onChatStateChanged } = await import('./conversationStorage');
    let calls = 0;
    onChatStateChanged(() => {
      calls += 1;
    });

    await setPendingAsk(PENDING);
    expect(calls).toBe(1);
    await setConversation(CONVERSATION);
    expect(calls).toBe(2);
    await fakeBrowser.storage.session.set({ unrelated: 1 });
    await fakeBrowser.storage.local.set({ fa_pending_ask: PENDING });
    expect(calls).toBe(2);
  });
});
