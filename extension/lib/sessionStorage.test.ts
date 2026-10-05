import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeBrowser } from 'wxt/testing/fake-browser';
import {
  clearActiveStatement,
  clearStoredSession,
  getActiveStatement,
  getStoredSession,
  onStoredSessionChanged,
  setActiveStatement,
  setStoredSession,
} from './sessionStorage';
import type { StoredSession } from './sessionStorage';
import { setDataGrant } from './dataAccessStorage';
import { getConversation, getPendingAsk, setConversation, setPendingAsk } from './conversationStorage';

function createFakeLocalStorage() {
  let store: Record<string, unknown> = {};
  return {
    get: vi.fn(async (key: string) => ({ [key]: store[key] })),
    set: vi.fn(async (items: Record<string, unknown>) => {
      store = { ...store, ...items };
    }),
    remove: vi.fn(async (key: string) => {
      delete store[key];
    }),
  };
}

describe('sessionStorage', () => {
  beforeEach(() => {
    vi.stubGlobal('browser', { storage: { local: createFakeLocalStorage() } });
  });

  it('returns null when nothing is stored', async () => {
    await expect(getStoredSession()).resolves.toBeNull();
  });

  it('round-trips a stored session', async () => {
    const session: StoredSession = {
      sessionToken: 'session-token-123',
      accountId: 'account-123',
      expiresAt: '2026-01-01T00:00:00Z',
      provider: 'google',
    };
    await setStoredSession(session);
    await expect(getStoredSession()).resolves.toEqual(session);
  });

  it('clears a stored session', async () => {
    const session: StoredSession = {
      sessionToken: 'session-token-123',
      accountId: 'account-123',
      expiresAt: '2026-01-01T00:00:00Z',
      provider: 'microsoft',
    };
    await setStoredSession(session);
    await clearStoredSession();
    await expect(getStoredSession()).resolves.toBeNull();
  });
});

/**
 * Uses WXT's real fake-browser (@webext-core/fake-browser), not the hand-rolled stub
 * above -- confirmed by reading its source that storage.local's set/remove/clear
 * genuinely fire storage.onChanged with (changes, areaName), matching the real API. The
 * hand-rolled stub above only implements get/set/remove, not onChanged, so it could not
 * honestly exercise this path.
 */
describe('onStoredSessionChanged', () => {
  const fakeSession: StoredSession = {
    sessionToken: 'fake-session-token',
    accountId: 'account-123',
    expiresAt: '2026-01-01T00:00:00Z',
    provider: 'google',
  };

  beforeEach(() => {
    fakeBrowser.reset();
    vi.stubGlobal('browser', fakeBrowser);
  });

  it('calls back with the new session on a local fa_session change', async () => {
    const callback = vi.fn();
    onStoredSessionChanged(callback);

    await setStoredSession(fakeSession);

    expect(callback).toHaveBeenCalledWith(fakeSession);
  });

  it('calls back with null when fa_session is removed', async () => {
    await setStoredSession(fakeSession);
    const callback = vi.fn();
    onStoredSessionChanged(callback);

    await clearStoredSession();

    expect(callback).toHaveBeenCalledWith(null);
  });

  it('ignores changes to unrelated keys in the local area', async () => {
    const callback = vi.fn();
    onStoredSessionChanged(callback);

    await fakeBrowser.storage.local.set({ some_other_key: 'value' });

    expect(callback).not.toHaveBeenCalled();
  });

  it('ignores fa_session changes in a non-local storage area', async () => {
    const callback = vi.fn();
    onStoredSessionChanged(callback);

    await fakeBrowser.storage.sync.set({ fa_session: fakeSession });

    expect(callback).not.toHaveBeenCalled();
  });
});

describe('clearStoredSession clears data access too (the single chokepoint)', () => {
  beforeEach(() => {
    fakeBrowser.reset();
    vi.stubGlobal('browser', fakeBrowser);
  });

  it('removes fa_data_tokens from storage.session and fa_data_accounts from storage.local', async () => {
    await setStoredSession({
      sessionToken: 'session-token-123',
      accountId: 'account-123',
      expiresAt: '2026-01-01T00:00:00Z',
      provider: 'google',
    });
    await setDataGrant(
      'google',
      { accessToken: 'data-token', expiresAt: Date.now() + 3_600_000, scope: 'openid' },
      'data@example.com',
    );

    await setActiveStatement({
      csvContextId: 'ctx-1',
      entityName: 'Spike Co',
      label: "FA Spike Test · 'P&L'!A3:C9",
      confirmedAt: '2026-10-03T12:00:00Z',
      cadence: 'quarterly',
      scale: 'thousands',
      currency: null,
    });

    // Session 5: the chat -- transcript and pending question -- goes with the session too.
    await setConversation({
      csvContextId: 'ctx-1', statementLabel: 'Spike Co', conversationId: 'conv-1',
      messages: [{ role: 'question', text: 'What was revenue?' }],
    });
    await setPendingAsk({
      requestId: 'req-1', question: 'What was revenue?', csvContextId: 'ctx-1', conversationId: 'conv-1', startedAt: 1,
    });
    expect(await getPendingAsk()).not.toBeNull();

    await clearStoredSession();

    expect(await getConversation()).toBeNull();
    expect(await getPendingAsk()).toBeNull();
    expect(await getStoredSession()).toBeNull();
    expect(await getActiveStatement()).toBeNull();
    expect(await fakeBrowser.storage.session.get(null)).toEqual({});
    expect(await fakeBrowser.storage.local.get(null)).toEqual({});
  });
});

describe('active statement (session 4)', () => {
  beforeEach(() => {
    fakeBrowser.reset();
    vi.stubGlobal('browser', fakeBrowser);
  });

  it('round-trips in storage.local and clears on its own', async () => {
    const statement = {
      csvContextId: 'ctx-1',
      entityName: 'Spike Co',
      label: 'FA Spike Test',
      confirmedAt: '2026-10-03T12:00:00Z',
      cadence: null,
      scale: 'ones',
      currency: 'EUR',
    };
    expect(await getActiveStatement()).toBeNull();
    await setActiveStatement(statement);
    expect(await getActiveStatement()).toEqual(statement);
    expect(await fakeBrowser.storage.local.get('fa_active_statement')).toEqual({ fa_active_statement: statement });
    await clearActiveStatement();
    expect(await getActiveStatement()).toBeNull();
  });
});
