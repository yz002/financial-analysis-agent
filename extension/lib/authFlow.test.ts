import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AuthFlowError,
  buildAuthorizationUrl,
  exchangeCodeForToken,
  parseAuthorizationCode,
} from './authFlow';
import { DATA_GRANT_CONFIG } from './authConfig';

const REDIRECT_URI = 'https://abc123.chromiumapp.org/';

describe('buildAuthorizationUrl', () => {
  it('builds a Google sign-in URL with identity-only scopes', () => {
    const url = new URL(
      buildAuthorizationUrl('google', { codeChallenge: 'challenge123', redirectUri: REDIRECT_URI }),
    );
    expect(url.origin + url.pathname).toBe('https://accounts.google.com/o/oauth2/v2/auth');
    expect(url.searchParams.get('response_type')).toBe('code');
    expect(url.searchParams.get('redirect_uri')).toBe(REDIRECT_URI);
    expect(url.searchParams.get('code_challenge')).toBe('challenge123');
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    expect(url.searchParams.get('scope')).toBe('openid email');
    expect(url.searchParams.has('response_mode')).toBe(false);
  });

  it('builds a Microsoft sign-in URL with User.Read only, pinning response_mode=query', () => {
    const url = new URL(
      buildAuthorizationUrl('microsoft', { codeChallenge: 'challenge123', redirectUri: REDIRECT_URI }),
    );
    expect(url.origin + url.pathname).toBe(
      'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    );
    expect(url.searchParams.get('response_mode')).toBe('query');
    expect(url.searchParams.get('scope')).toBe('User.Read');
  });

  it('builds a Google data-grant URL: Sheets scope, include_granted_scopes, never access_type', () => {
    const { scopes, extraParams } = DATA_GRANT_CONFIG.google;
    const url = new URL(
      buildAuthorizationUrl('google', {
        codeChallenge: 'challenge123',
        redirectUri: REDIRECT_URI,
        scopes,
        extraParams: { ...extraParams, prompt: 'none', login_hint: 'data@example.com' },
      }),
    );
    expect(url.searchParams.get('scope')).toBe(
      'openid email https://www.googleapis.com/auth/spreadsheets.readonly',
    );
    expect(url.searchParams.get('include_granted_scopes')).toBe('true');
    expect(url.searchParams.get('prompt')).toBe('none');
    expect(url.searchParams.get('login_hint')).toBe('data@example.com');
    expect(url.searchParams.has('access_type')).toBe(false);
  });

  it('builds a Microsoft data-grant URL with Files.Read, not Files.ReadWrite', () => {
    const { scopes, extraParams } = DATA_GRANT_CONFIG.microsoft;
    const url = new URL(
      buildAuthorizationUrl('microsoft', {
        codeChallenge: 'challenge123',
        redirectUri: REDIRECT_URI,
        scopes,
        extraParams: { ...extraParams, prompt: 'select_account' },
      }),
    );
    expect(url.searchParams.get('scope')).toBe('User.Read Files.Read');
    expect(url.searchParams.get('prompt')).toBe('select_account');
    expect(url.searchParams.get('response_mode')).toBe('query');
  });
});

describe('parseAuthorizationCode', () => {
  it('extracts the code from a well-formed redirect URL', () => {
    expect(parseAuthorizationCode(`${REDIRECT_URI}?code=abc123`)).toBe('abc123');
  });

  it('throws AuthFlowError carrying the raw provider error when the provider reports one', () => {
    let caught: unknown;
    try {
      parseAuthorizationCode(`${REDIRECT_URI}?error=interaction_required`);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(AuthFlowError);
    expect((caught as AuthFlowError).providerError).toBe('interaction_required');
  });

  it('throws AuthFlowError with no providerError when neither code nor error is present', () => {
    let caught: unknown;
    try {
      parseAuthorizationCode(REDIRECT_URI);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(AuthFlowError);
    expect((caught as AuthFlowError).providerError).toBeUndefined();
  });
});

describe('exchangeCodeForToken (Microsoft only -- Google exchanges server-side, see AuthFlowResult)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('posts the expected fields and returns the token, expiry, and scope', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: 'token123', expires_in: 3600, scope: 'User.Read' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await exchangeCodeForToken('microsoft', {
      code: 'code123',
      codeVerifier: 'verifier123',
      redirectUri: REDIRECT_URI,
    });

    expect(result).toEqual({ accessToken: 'token123', expiresIn: 3600, scope: 'User.Read' });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('https://login.microsoftonline.com/common/oauth2/v2.0/token');
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/x-www-form-urlencoded');
    const body = new URLSearchParams(init.body as string);
    expect(body.get('grant_type')).toBe('authorization_code');
    expect(body.get('code')).toBe('code123');
    expect(body.get('code_verifier')).toBe('verifier123');
    expect(body.get('redirect_uri')).toBe(REDIRECT_URI);
  });

  it("never returns Microsoft's unrequested refresh_token", async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          access_token: 'token123',
          expires_in: 3600,
          scope: 'User.Read Files.Read',
          refresh_token: 'RT-secret',
        }),
      }),
    );

    const result = await exchangeCodeForToken('microsoft', {
      code: 'code123',
      codeVerifier: 'verifier123',
      redirectUri: REDIRECT_URI,
    });

    expect(result).not.toHaveProperty('refresh_token');
    expect(JSON.stringify(result)).not.toContain('RT-secret');
  });

  it('throws AuthFlowError on a non-2xx response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, json: async () => ({ error: 'invalid_grant' }) }),
    );

    await expect(
      exchangeCodeForToken('microsoft', {
        code: 'code123',
        codeVerifier: 'verifier123',
        redirectUri: REDIRECT_URI,
      }),
    ).rejects.toThrow(AuthFlowError);
  });
});
