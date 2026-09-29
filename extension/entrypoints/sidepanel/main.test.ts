// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BackendApiError } from '../../lib/backendApi';
import { AuthFlowError } from '../../lib/authFlow';
import type { StoredSession } from '../../lib/sessionStorage';

/**
 * These tests check the `hidden` attribute main.ts's rendering logic toggles, and which
 * mocked calls fire -- not the resulting visual/computed `display`. jsdom's
 * `getComputedStyle` does not reliably implement the full CSS cascade (in particular an
 * attribute selector like `[hidden]` combined with `!important` overriding an author
 * stylesheet rule), so asserting on computed style here would not genuinely verify
 * style.css's fix -- it would just re-assert whatever this file assumes the cascade
 * does. That fix needs manual/live verification in a real browser.
 */

const SIDEPANEL_BODY = `
  <div id="app">
    <h1>Financial Analysis Agent</h1>
    <div id="status-message" role="status" aria-live="polite"></div>
    <div id="signed-out-view">
      <p>Sign in to connect your spreadsheet.</p>
      <button id="signin-google" type="button">Sign in with Google</button>
      <button id="signin-microsoft" type="button">Sign in with Microsoft</button>
    </div>
    <div id="signed-in-view" hidden>
      <p id="account-info"></p>
      <button id="signout" type="button">Sign out</button>
      <button id="revoke-all" type="button">Sign out everywhere</button>
    </div>
  </div>
`;

const {
  getStoredSessionMock,
  setStoredSessionMock,
  clearStoredSessionMock,
  onStoredSessionChangedMock,
  launchAuthFlowMock,
  exchangeTokenMock,
  logoutMock,
  revokeAllSessionsMock,
} = vi.hoisted(() => ({
  getStoredSessionMock: vi.fn(),
  setStoredSessionMock: vi.fn(),
  clearStoredSessionMock: vi.fn(),
  onStoredSessionChangedMock: vi.fn(),
  launchAuthFlowMock: vi.fn(),
  exchangeTokenMock: vi.fn(),
  logoutMock: vi.fn(),
  revokeAllSessionsMock: vi.fn(),
}));

// Mocking the whole sessionStorage module (rather than a real/fake browser.storage)
// keeps these tests deterministic and focused on main.ts's own control flow.
vi.mock('../../lib/sessionStorage', () => ({
  getStoredSession: getStoredSessionMock,
  setStoredSession: setStoredSessionMock,
  clearStoredSession: clearStoredSessionMock,
  onStoredSessionChanged: onStoredSessionChangedMock,
}));

// Keep the real AuthFlowError class (main.ts does `instanceof AuthFlowError`) while
// mocking only launchAuthFlow itself.
vi.mock('../../lib/authFlow', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/authFlow')>();
  return { ...actual, launchAuthFlow: launchAuthFlowMock };
});

// Same pattern for BackendApiError (main.ts does `instanceof BackendApiError`).
vi.mock('../../lib/backendApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/backendApi')>();
  return {
    ...actual,
    exchangeToken: exchangeTokenMock,
    logout: logoutMock,
    revokeAllSessions: revokeAllSessionsMock,
  };
});

async function flushAsync(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

async function loadSidepanel(): Promise<void> {
  document.body.innerHTML = SIDEPANEL_BODY;
  vi.resetModules();
  await import('./main');
  await flushAsync();
}

function isHidden(selector: string): boolean {
  return document.querySelector(selector)?.hasAttribute('hidden') ?? true;
}

function statusText(): string | null {
  return document.querySelector('#status-message')?.textContent ?? null;
}

const existingSession: StoredSession = {
  sessionToken: 'old-token',
  accountId: 'account-old',
  expiresAt: '2026-01-01T00:00:00Z',
  provider: 'google',
};

beforeEach(() => {
  getStoredSessionMock.mockReset().mockResolvedValue(null);
  setStoredSessionMock.mockReset().mockResolvedValue(undefined);
  clearStoredSessionMock.mockReset().mockResolvedValue(undefined);
  onStoredSessionChangedMock.mockReset();
  launchAuthFlowMock.mockReset();
  exchangeTokenMock.mockReset();
  logoutMock.mockReset().mockResolvedValue({ revoked: true });
  revokeAllSessionsMock.mockReset().mockResolvedValue({ revoked: true });
});

afterEach(() => {
  document.body.innerHTML = '';
});

describe('sidepanel initial paint (hidden-attribute state only -- see file header)', () => {
  it('signed-out state: exactly #signed-out-view lacks the hidden attribute', async () => {
    getStoredSessionMock.mockResolvedValue(null);
    await loadSidepanel();

    expect(isHidden('#signed-out-view')).toBe(false);
    expect(isHidden('#signed-in-view')).toBe(true);
  });

  it('signed-in state: exactly #signed-in-view lacks the hidden attribute', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    await loadSidepanel();

    expect(isHidden('#signed-out-view')).toBe(true);
    expect(isHidden('#signed-in-view')).toBe(false);
  });
});

describe('friendlyMessage 422 disambiguation (via the real sign-in flow)', () => {
  it('shows the Microsoft-specific message for the "no usable email" 422', async () => {
    launchAuthFlowMock.mockResolvedValue({ provider: 'microsoft', oauthToken: 'token' });
    exchangeTokenMock.mockRejectedValue(
      new BackendApiError(
        '/v1/auth/exchange failed (422).',
        422,
        'No usable email address is available for this account.',
      ),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-microsoft')!.click();
    await flushAsync();

    expect(statusText()).toBe(
      "This Microsoft account doesn't have a usable email address for sign-in.",
    );
  });

  it('shows a generic message for a "wrong fields for provider" 422, not the email message', async () => {
    launchAuthFlowMock.mockResolvedValue({
      provider: 'google',
      code: 'c',
      codeVerifier: 'v',
      redirectUri: 'r',
    });
    exchangeTokenMock.mockRejectedValue(
      new BackendApiError('/v1/auth/exchange failed (422).', 422, 'Microsoft sign-in requires oauth_token.'),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(statusText()).toBe('Sign-in failed — please try again.');
    expect(statusText()).not.toContain('usable email address');
  });
});

describe('bug 1: revoke-all never starts a new sign-in', () => {
  it('revoke-all never calls launchAuthFlow, and ends signed-out', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    revokeAllSessionsMock.mockResolvedValue({ revoked: true });
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#revoke-all')!.click();
    await flushAsync();

    expect(launchAuthFlowMock).not.toHaveBeenCalled();
    expect(clearStoredSessionMock).toHaveBeenCalled();
    expect(isHidden('#signed-out-view')).toBe(false);
    expect(isHidden('#signed-in-view')).toBe(true);
  });

  it('a 401 from revoke-all clears the session and shows signed-out', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    revokeAllSessionsMock.mockRejectedValue(
      new BackendApiError('failed', 401, 'Invalid or expired session token.'),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#revoke-all')!.click();
    await flushAsync();

    expect(launchAuthFlowMock).not.toHaveBeenCalled();
    expect(clearStoredSessionMock).toHaveBeenCalled();
    expect(isHidden('#signed-out-view')).toBe(false);
  });

  it('a non-401 failure from revoke-all keeps the session and shows an error, still signed in', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    revokeAllSessionsMock.mockRejectedValue(new Error('network error'));
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#revoke-all')!.click();
    await flushAsync();

    expect(launchAuthFlowMock).not.toHaveBeenCalled();
    expect(clearStoredSessionMock).not.toHaveBeenCalled();
    expect(isHidden('#signed-in-view')).toBe(false);
    expect(statusText()).toContain('other devices may still be signed in');
  });
});

describe('bug 3: old session revoked only after a successful new sign-in', () => {
  it('a successful sign-in with an existing session revokes the OLD token', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    launchAuthFlowMock.mockResolvedValue({
      provider: 'google',
      code: 'c',
      codeVerifier: 'v',
      redirectUri: 'r',
    });
    exchangeTokenMock.mockResolvedValue({
      session_token: 'new-token',
      account_id: 'account-new',
      expires_at: '2026-02-01T00:00:00Z',
    });
    logoutMock.mockResolvedValue({ revoked: true });
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(setStoredSessionMock).toHaveBeenCalledWith(
      expect.objectContaining({ sessionToken: 'new-token' }),
    );
    expect(logoutMock).toHaveBeenCalledWith('old-token');
    expect(isHidden('#signed-in-view')).toBe(false);
  });

  it('a cancelled sign-in never revokes the existing session', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    launchAuthFlowMock.mockRejectedValue(
      new AuthFlowError('Sign-in was cancelled or the authorization window could not be opened.'),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(logoutMock).not.toHaveBeenCalled();
    expect(setStoredSessionMock).not.toHaveBeenCalled();
  });

  it('a 401 from the best-effort old-token logout still leaves the NEW session stored and shown', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    launchAuthFlowMock.mockResolvedValue({
      provider: 'google',
      code: 'c',
      codeVerifier: 'v',
      redirectUri: 'r',
    });
    exchangeTokenMock.mockResolvedValue({
      session_token: 'new-token',
      account_id: 'account-new',
      expires_at: '2026-02-01T00:00:00Z',
    });
    logoutMock.mockRejectedValue(new BackendApiError('failed', 401, 'Invalid or expired session token.'));
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(setStoredSessionMock).toHaveBeenCalledWith(
      expect.objectContaining({ sessionToken: 'new-token' }),
    );
    expect(clearStoredSessionMock).not.toHaveBeenCalled();
    expect(isHidden('#signed-in-view')).toBe(false);
    expect(isHidden('#signed-out-view')).toBe(true);
  });
});
