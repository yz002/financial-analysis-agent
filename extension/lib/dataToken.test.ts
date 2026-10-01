import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { fakeBrowser } from 'wxt/testing/fake-browser';
import { AuthFlowError } from './authFlow';
import { BackendApiError } from './backendApi';
import { setDataGrant } from './dataAccessStorage';
import {
  getDataToken,
  InteractiveAfterSilentFailedError,
  ScopeNotGrantedError,
  UserClickRequiredError,
} from './dataToken';

const { exchangeGoogleDataTokenMock } = vi.hoisted(() => ({
  exchangeGoogleDataTokenMock: vi.fn(),
}));

// Mock only the backend call; keep the real BackendApiError class.
vi.mock('./backendApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./backendApi')>();
  return { ...actual, exchangeGoogleDataToken: exchangeGoogleDataTokenMock };
});

const REDIRECT_URI = 'https://abc123.chromiumapp.org/';
const SHEETS = 'https://www.googleapis.com/auth/spreadsheets.readonly';
const SESSION = 'session-token-123';
// jsdom/script-dispatched events are never trusted, so a trusted click is faked as a plain
// object -- getDataToken only reads isTrusted.
const trustedClick = { isTrusted: true } as Event;

let launchWebAuthFlow: Mock;

function codeRedirect(code = 'auth-code') {
  return `${REDIRECT_URI}?code=${code}`;
}

function errorRedirect(error: string) {
  return `${REDIRECT_URI}?error=${error}`;
}

/** The authorization URL and options of the Nth launchWebAuthFlow call. */
function launchCall(n: number): { url: URL; interactive: boolean } {
  const details = launchWebAuthFlow.mock.calls[n]![0] as { url: string; interactive: boolean };
  return { url: new URL(details.url), interactive: details.interactive };
}

function googleGrant(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    access_token: 'google-data-token',
    expires_in: 3599,
    scope: `openid email ${SHEETS}`,
    email: 'data@example.com',
    ...overrides,
  };
}

async function storedState(): Promise<{ session: unknown; local: unknown }> {
  return {
    session: (await fakeBrowser.storage.session.get('fa_data_tokens')).fa_data_tokens,
    local: (await fakeBrowser.storage.local.get('fa_data_accounts')).fa_data_accounts,
  };
}

beforeEach(() => {
  fakeBrowser.reset();
  vi.stubGlobal('browser', fakeBrowser);
  launchWebAuthFlow = vi.fn();
  Object.assign(fakeBrowser.identity, {
    launchWebAuthFlow,
    getRedirectURL: () => REDIRECT_URI,
  });
  exchangeGoogleDataTokenMock.mockReset();
  vi.spyOn(console, 'info').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('click guard: no launchWebAuthFlow outside a user click', () => {
  it.each([
    ['an untrusted (script-dispatched) event', { isTrusted: false } as Event],
    ['no event at all', undefined],
  ])('refuses %s with nothing cached, before touching browser.identity', async (_label, click) => {
    await expect(getDataToken('google', { sessionToken: SESSION, click })).rejects.toBeInstanceOf(
      UserClickRequiredError,
    );
    expect(launchWebAuthFlow).not.toHaveBeenCalled();
    expect(exchangeGoogleDataTokenMock).not.toHaveBeenCalled();
  });

  it('refuses an untrusted event even when a grant could run silently', async () => {
    await fakeBrowser.storage.local.set({ fa_data_accounts: { microsoft: 'data@example.com' } });

    await expect(
      getDataToken('microsoft', { sessionToken: SESSION, click: { isTrusted: false } as Event }),
    ).rejects.toBeInstanceOf(UserClickRequiredError);
    expect(launchWebAuthFlow).not.toHaveBeenCalled();
  });
});

describe('cache', () => {
  it('returns a cached, unexpired token with no launchWebAuthFlow call', async () => {
    await setDataGrant(
      'google',
      { accessToken: 'cached-token', expiresAt: Date.now() + 60 * 60 * 1000, scope: SHEETS },
      'data@example.com',
    );

    const token = await getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    expect(token).toEqual({ accessToken: 'cached-token', email: 'data@example.com', scope: SHEETS });
    expect(launchWebAuthFlow).not.toHaveBeenCalled();
    expect(exchangeGoogleDataTokenMock).not.toHaveBeenCalled();
  });

  it('does not use a token inside the 5-minute expiry margin', async () => {
    await setDataGrant(
      'google',
      { accessToken: 'nearly-expired', expiresAt: Date.now() + 2 * 60 * 1000, scope: SHEETS },
      'data@example.com',
    );
    launchWebAuthFlow.mockResolvedValue(codeRedirect());
    exchangeGoogleDataTokenMock.mockResolvedValue(googleGrant());

    const token = await getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    expect(token.accessToken).toBe('google-data-token');
    expect(launchWebAuthFlow).toHaveBeenCalledTimes(1);
  });
});

describe('silent, then interactive', () => {
  it('with a stored data email, tries silent first: prompt=none + login_hint, interactive:false', async () => {
    await fakeBrowser.storage.local.set({ fa_data_accounts: { google: 'data@example.com' } });
    launchWebAuthFlow.mockResolvedValue(codeRedirect('silent-code'));
    exchangeGoogleDataTokenMock.mockResolvedValue(googleGrant());

    const token = await getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    expect(launchWebAuthFlow).toHaveBeenCalledTimes(1);
    const { url, interactive } = launchCall(0);
    expect(interactive).toBe(false);
    expect(launchWebAuthFlow.mock.calls[0]![0]).toMatchObject({
      abortOnLoadForNonInteractive: false,
      timeoutMsForNonInteractive: 10000,
    });
    expect(url.searchParams.get('prompt')).toBe('none');
    expect(url.searchParams.get('login_hint')).toBe('data@example.com');
    expect(url.searchParams.get('include_granted_scopes')).toBe('true');
    expect(exchangeGoogleDataTokenMock).toHaveBeenCalledWith(
      SESSION,
      expect.objectContaining({ code: 'silent-code', redirectUri: REDIRECT_URI }),
    );
    expect(token).toEqual({
      accessToken: 'google-data-token',
      email: 'data@example.com',
      scope: `openid email ${SHEETS}`,
    });
  });

  it('with no stored data email, goes straight to interactive with prompt=select_account', async () => {
    launchWebAuthFlow.mockResolvedValue(codeRedirect());
    exchangeGoogleDataTokenMock.mockResolvedValue(googleGrant());

    await getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    expect(launchWebAuthFlow).toHaveBeenCalledTimes(1);
    const { url, interactive } = launchCall(0);
    expect(interactive).toBe(true);
    expect(url.searchParams.get('prompt')).toBe('select_account');
    expect(url.searchParams.has('login_hint')).toBe(false);
  });

  it('falls back to one interactive attempt in the same call when silent fails', async () => {
    await fakeBrowser.storage.local.set({ fa_data_accounts: { google: 'old@example.com' } });
    launchWebAuthFlow
      .mockResolvedValueOnce(errorRedirect('interaction_required'))
      .mockResolvedValueOnce(codeRedirect('interactive-code'));
    exchangeGoogleDataTokenMock.mockResolvedValue(googleGrant({ email: 'picked@example.com' }));

    const token = await getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    expect(launchWebAuthFlow).toHaveBeenCalledTimes(2);
    expect(launchCall(0).interactive).toBe(false);
    expect(launchCall(1).interactive).toBe(true);
    expect(launchCall(1).url.searchParams.get('prompt')).toBe('select_account');
    expect(token.email).toBe('picked@example.com');
    expect(await storedState()).toEqual({
      session: { google: expect.objectContaining({ accessToken: 'google-data-token' }) },
      local: { google: 'picked@example.com' },
    });
  });

  it('a silent timeout (launch rejects) also falls back to interactive', async () => {
    await fakeBrowser.storage.local.set({ fa_data_accounts: { google: 'data@example.com' } });
    launchWebAuthFlow
      .mockRejectedValueOnce(new Error('timed out'))
      .mockResolvedValueOnce(codeRedirect());
    exchangeGoogleDataTokenMock.mockResolvedValue(googleGrant());

    await getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    expect(launchWebAuthFlow).toHaveBeenCalledTimes(2);
    expect(launchCall(1).interactive).toBe(true);
  });

  it('silent fails, then the interactive window fails to open: a "click again" error, nothing stored', async () => {
    await fakeBrowser.storage.local.set({ fa_data_accounts: { google: 'data@example.com' } });
    launchWebAuthFlow
      .mockRejectedValueOnce(new Error('timed out'))
      .mockRejectedValueOnce(new Error('User interaction required.'));

    const attempt = getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    await expect(attempt).rejects.toBeInstanceOf(InteractiveAfterSilentFailedError);
    await expect(attempt).rejects.toThrow('click Connect data again');
    expect(exchangeGoogleDataTokenMock).not.toHaveBeenCalled();
    expect((await storedState()).session).toBeUndefined();
  });

  it('a cancelled interactive-only attempt keeps the ordinary AuthFlowError', async () => {
    launchWebAuthFlow.mockRejectedValueOnce(new Error('The user did not approve access.'));

    const attempt = getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    await expect(attempt).rejects.toBeInstanceOf(AuthFlowError);
    await expect(attempt).rejects.not.toBeInstanceOf(InteractiveAfterSilentFailedError);
  });

  it('a backend 401 during the exchange propagates: no interactive retry, nothing stored', async () => {
    await fakeBrowser.storage.local.set({ fa_data_accounts: { google: 'data@example.com' } });
    launchWebAuthFlow.mockResolvedValue(codeRedirect());
    exchangeGoogleDataTokenMock.mockRejectedValue(
      new BackendApiError('failed', 401, 'Invalid or expired session token.'),
    );

    await expect(
      getDataToken('google', { sessionToken: SESSION, click: trustedClick }),
    ).rejects.toMatchObject({ status: 401 });
    expect(launchWebAuthFlow).toHaveBeenCalledTimes(1);
    expect((await storedState()).session).toBeUndefined();
  });
});

describe('Google granular consent', () => {
  it('interactive grant without the Sheets scope: ScopeNotGrantedError, neither token nor email stored', async () => {
    launchWebAuthFlow.mockResolvedValue(codeRedirect());
    exchangeGoogleDataTokenMock.mockResolvedValue(googleGrant({ scope: 'openid email' }));

    await expect(
      getDataToken('google', { sessionToken: SESSION, click: trustedClick }),
    ).rejects.toBeInstanceOf(ScopeNotGrantedError);
    expect(await storedState()).toEqual({ session: undefined, local: undefined });
  });

  it('a silent grant without the Sheets scope falls through to interactive', async () => {
    await fakeBrowser.storage.local.set({ fa_data_accounts: { google: 'data@example.com' } });
    launchWebAuthFlow.mockResolvedValue(codeRedirect());
    exchangeGoogleDataTokenMock
      .mockResolvedValueOnce(googleGrant({ scope: 'openid email' }))
      .mockResolvedValueOnce(googleGrant());

    const token = await getDataToken('google', { sessionToken: SESSION, click: trustedClick });

    expect(launchWebAuthFlow).toHaveBeenCalledTimes(2);
    expect(launchCall(1).interactive).toBe(true);
    expect(token.scope).toContain(SHEETS);
  });
});

describe('Microsoft', () => {
  function stubMicrosoftFetch(me: { mail?: string | null; userPrincipalName?: string | null }) {
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.startsWith('https://login.microsoftonline.com/')) {
        return {
          ok: true,
          json: async () => ({
            access_token: 'ms-data-token',
            expires_in: 3600,
            scope: 'User.Read Files.Read',
            refresh_token: 'RT-secret',
          }),
        };
      }
      if (url.startsWith('https://graph.microsoft.com/v1.0/me')) {
        return { ok: true, json: async () => me };
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('never stores the refresh token anywhere, and falls back to userPrincipalName for the email', async () => {
    const fetchMock = stubMicrosoftFetch({ mail: null, userPrincipalName: 'upn@contoso.example' });
    launchWebAuthFlow.mockResolvedValue(codeRedirect());

    const token = await getDataToken('microsoft', { sessionToken: SESSION, click: trustedClick });

    expect(token).toEqual({
      accessToken: 'ms-data-token',
      email: 'upn@contoso.example',
      scope: 'User.Read Files.Read',
    });
    expect(JSON.stringify(token)).not.toContain('RT-secret');
    const everything = JSON.stringify({
      session: await fakeBrowser.storage.session.get(null),
      local: await fakeBrowser.storage.local.get(null),
      sync: await fakeBrowser.storage.sync.get(null),
    });
    expect(everything).not.toContain('RT-secret');
    // /me is called with the new data token.
    const meCall = fetchMock.mock.calls.find(([url]) => url.startsWith('https://graph.microsoft.com'))!;
    expect((meCall[1] as RequestInit).headers).toEqual({ Authorization: 'Bearer ms-data-token' });
    expect(exchangeGoogleDataTokenMock).not.toHaveBeenCalled();
  });

  it('prefers mail over userPrincipalName', async () => {
    stubMicrosoftFetch({ mail: 'mail@contoso.example', userPrincipalName: 'upn@contoso.example' });
    launchWebAuthFlow.mockResolvedValue(codeRedirect());

    const token = await getDataToken('microsoft', { sessionToken: SESSION, click: trustedClick });

    expect(token.email).toBe('mail@contoso.example');
  });

  it('keeps the token only in storage.session and the email only in storage.local', async () => {
    stubMicrosoftFetch({ mail: 'mail@contoso.example' });
    launchWebAuthFlow.mockResolvedValue(codeRedirect());

    await getDataToken('microsoft', { sessionToken: SESSION, click: trustedClick });

    const session = JSON.stringify(await fakeBrowser.storage.session.get(null));
    const local = JSON.stringify(await fakeBrowser.storage.local.get(null));
    const sync = JSON.stringify(await fakeBrowser.storage.sync.get(null));
    expect(session).toContain('ms-data-token');
    expect(session).not.toContain('mail@contoso.example');
    expect(local).toContain('mail@contoso.example');
    expect(local).not.toContain('ms-data-token');
    expect(sync).toBe('{}');
  });
});
