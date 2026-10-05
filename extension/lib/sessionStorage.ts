import type { AuthProvider } from './authConfig';
import { clearChatState } from './conversationStorage';
import { clearAllDataAccess } from './dataAccessStorage';

export interface StoredSession {
  sessionToken: string;
  accountId: string;
  expiresAt: string; // ISO-8601, as returned by POST /v1/auth/exchange
  provider: AuthProvider; // local-only -- not part of the backend contract. Lets the
                           // signed-in UI say which provider, and lets "sign out
                           // everywhere" know which provider to re-launch.
}

const SESSION_STORAGE_KEY = 'fa_session';

export async function getStoredSession(): Promise<StoredSession | null> {
  const result = await browser.storage.local.get(SESSION_STORAGE_KEY);
  const session = result[SESSION_STORAGE_KEY] as StoredSession | undefined;
  return session ?? null;
}

export async function setStoredSession(session: StoredSession): Promise<void> {
  await browser.storage.local.set({ [SESSION_STORAGE_KEY]: session });
}

/**
 * Also clears every provider data token and data-account email (lib/dataAccessStorage.ts), the
 * active statement, and the chat -- the conversation and any pending question
 * (lib/conversationStorage.ts). This is the single chokepoint, so sign-out, revoke-all, a 401,
 * and any future path that ends the session can't leave data-access or chat state behind
 * (chrome-extension-design.md SS6 3a).
 */
export async function clearStoredSession(): Promise<void> {
  await browser.storage.local.remove(SESSION_STORAGE_KEY);
  await clearAllDataAccess();
  await clearActiveStatement();
  await clearChatState();
}

/**
 * The statement confirmed last (Phase D session 4): what questions will be asked about from
 * session 5 on. An identifier, not a credential -- the backend checks ownership on every use --
 * so it lives in storage.local and survives a browser restart. It belongs to the signed-in
 * account, so it's cleared with the session and on every new sign-in.
 */
export interface ActiveStatement {
  csvContextId: string;
  entityName: string;
  label: string; // e.g. "FA Spike Test · 'P&L'!A3:C9"
  confirmedAt: string; // ISO-8601
  cadence: string | null;
  scale: string;
  currency: string | null;
}

const ACTIVE_STATEMENT_KEY = 'fa_active_statement';

export async function getActiveStatement(): Promise<ActiveStatement | null> {
  const result = await browser.storage.local.get(ACTIVE_STATEMENT_KEY);
  return (result[ACTIVE_STATEMENT_KEY] as ActiveStatement | undefined) ?? null;
}

export async function setActiveStatement(statement: ActiveStatement): Promise<void> {
  await browser.storage.local.set({ [ACTIVE_STATEMENT_KEY]: statement });
}

export async function clearActiveStatement(): Promise<void> {
  await browser.storage.local.remove(ACTIVE_STATEMENT_KEY);
}

/**
 * Subscribes to fa_session changes in chrome.storage.local -- the only way one side
 * panel document ever learns about another panel's sign-in/sign-out/revoke-all, since
 * each open panel is a separate document with no other shared state. Filters out
 * changes to unrelated keys and changes in a non-local storage area (e.g. `.sync`,
 * which this project never writes to, per the design doc's storage-area rule above).
 * Passes null when fa_session is removed (sign-out/revoke-all), the new StoredSession
 * otherwise.
 */
export function onStoredSessionChanged(callback: (session: StoredSession | null) => void): void {
  browser.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== 'local' || !(SESSION_STORAGE_KEY in changes)) return;
    const newValue = changes[SESSION_STORAGE_KEY]?.newValue as StoredSession | undefined;
    callback(newValue ?? null);
  });
}
