import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AuthFlowError,
  buildAuthorizationUrl,
  exchangeCodeForToken,
  parseAuthorizationCode,
} from './authFlow';

const REDIRECT_URI = 'https://abc123.chromiumapp.org/';

describe('buildAuthorizationUrl', () => {
  it('builds a Google authorization URL with the expected params', () => {
    const url = new URL(
      buildAuthorizationUrl('google', { codeChallenge: 'challenge123', redirectUri: REDIRECT_URI }),
    );
    expect(url.origin + url.pathname).toBe('https://accounts.google.com/o/oauth2/v2/auth');
    expect(url.searchParams.get('response_type')).toBe('code');
    expect(url.searchParams.get('redirect_uri')).toBe(REDIRECT_URI);
    expect(url.searchParams.get('code_challenge')).toBe('challenge123');
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    expect(url.searchParams.get('scope')).toBe(
      'openid email https://www.googleapis.com/auth/spreadsheets.readonly',
    );
    expect(url.searchParams.has('response_mode')).toBe(false);
  });

  it('builds a Microsoft authorization URL that pins response_mode=query', () => {
    const url = new URL(
      buildAuthorizationUrl('microsoft', { codeChallenge: 'challenge123', redirectUri: REDIRECT_URI }),
    );
    expect(url.origin + url.pathname).toBe(
      'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    );
    expect(url.searchParams.get('response_mode')).toBe('query');
    expect(url.searchParams.get('scope')).toBe('User.Read Files.ReadWrite');
  });
});

describe('parseAuthorizationCode', () => {
  it('extracts the code from a well-formed redirect URL', () => {
    expect(parseAuthorizationCode(`${REDIRECT_URI}?code=abc123`)).toBe('abc123');
  });

  it('throws AuthFlowError when the provider reports an error', () => {
    expect(() => parseAuthorizationCode(`${REDIRECT_URI}?error=access_denied`)).toThrow(
      AuthFlowError,
    );
  });

  it('throws AuthFlowError when neither code nor error is present', () => {
    expect(() => parseAuthorizationCode(REDIRECT_URI)).toThrow(AuthFlowError);
  });
});

describe('exchangeCodeForToken (Microsoft only -- Google exchanges server-side, see AuthFlowResult)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('posts the expected fields and returns the access token on success', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: 'token123' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const token = await exchangeCodeForToken('microsoft', {
      code: 'code123',
      codeVerifier: 'verifier123',
      redirectUri: REDIRECT_URI,
    });

    expect(token).toBe('token123');
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
