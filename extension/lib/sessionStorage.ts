import type { AuthProvider } from './authConfig';
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
 * Also clears every provider data token and data-account email (lib/dataAccessStorage.ts).
 * This is the single chokepoint, so sign-out, revoke-all, a 401, and any future path that
 * ends the session can't leave data-access state behind
 * (chrome-extension-design.md SS6 3a).
 */
export async function clearStoredSession(): Promise<void> {
  await browser.storage.local.remove(SESSION_STORAGE_KEY);
  await clearAllDataAccess();
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
