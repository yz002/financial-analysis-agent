import type { AuthProvider } from './authConfig';

/**
 * Where data-access state lives (chrome-extension-design.md SS2 "Data-access tokens", SS6 3a):
 *
 * - Provider data TOKENS: chrome.storage.session only -- never .local, never .sync. Cleared
 *   by a browser restart; survives closing/reopening the side panel (spike-verified).
 * - The data account's EMAIL per provider: chrome.storage.local. It isn't a credential, and
 *   keeping it across restarts is what lets the first read after a restart run silently
 *   (prompt=none + login_hint) instead of showing an account chooser.
 *
 * Both are tied to the current session: clearStoredSession() (lib/sessionStorage.ts) calls
 * clearAllDataAccess(), and a successful sign-in calls it too, so data-account state never
 * outlives or crosses over to a different session.
 */

export interface CachedDataToken {
  accessToken: string;
  expiresAt: number; // epoch ms
  scope: string;
}

type DataTokens = Partial<Record<AuthProvider, CachedDataToken>>;
type DataAccounts = Partial<Record<AuthProvider, string>>;

const DATA_TOKENS_KEY = 'fa_data_tokens'; // storage.session
const DATA_ACCOUNTS_KEY = 'fa_data_accounts'; // storage.local

async function readTokens(): Promise<DataTokens> {
  const result = await browser.storage.session.get(DATA_TOKENS_KEY);
  return (result[DATA_TOKENS_KEY] as DataTokens | undefined) ?? {};
}

async function readAccounts(): Promise<DataAccounts> {
  const result = await browser.storage.local.get(DATA_ACCOUNTS_KEY);
  return (result[DATA_ACCOUNTS_KEY] as DataAccounts | undefined) ?? {};
}

export async function getCachedDataToken(provider: AuthProvider): Promise<CachedDataToken | null> {
  return (await readTokens())[provider] ?? null;
}

export async function getDataAccountEmail(provider: AuthProvider): Promise<string | null> {
  return (await readAccounts())[provider] ?? null;
}

/** Records a completed data grant: the token in .session, the account email in .local. */
export async function setDataGrant(
  provider: AuthProvider,
  token: CachedDataToken,
  email: string,
): Promise<void> {
  const tokens = await readTokens();
  await browser.storage.session.set({ [DATA_TOKENS_KEY]: { ...tokens, [provider]: token } });
  const accounts = await readAccounts();
  await browser.storage.local.set({ [DATA_ACCOUNTS_KEY]: { ...accounts, [provider]: email } });
}

/**
 * Drops one provider's token and email, so its next data grant is interactive with an
 * account chooser -- used when the token stopped working or the account can't open a file.
 */
export async function forgetDataGrant(provider: AuthProvider): Promise<void> {
  const { [provider]: _droppedToken, ...tokens } = await readTokens();
  await browser.storage.session.set({ [DATA_TOKENS_KEY]: tokens });
  const { [provider]: _droppedEmail, ...accounts } = await readAccounts();
  await browser.storage.local.set({ [DATA_ACCOUNTS_KEY]: accounts });
}

export async function clearAllDataAccess(): Promise<void> {
  await browser.storage.session.remove(DATA_TOKENS_KEY);
  await browser.storage.local.remove(DATA_ACCOUNTS_KEY);
}
