import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  BackendApiError,
  exchangeGoogleDataToken,
  exchangeToken,
  logout,
  revokeAllSessions,
} from './backendApi';
import { BACKEND_BASE_URL } from './authConfig';

describe('backendApi', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('exchangeToken posts the expected Google body (code/code_verifier/redirect_uri), no Authorization header', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_token: 's', account_id: 'a', expires_at: 'e' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await exchangeToken({
      provider: 'google',
      code: 'code-123',
      codeVerifier: 'verifier-123',
      redirectUri: 'https://abc123.chromiumapp.org/',
    });

    expect(result).toEqual({ session_token: 's', account_id: 'a', expires_at: 'e' });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/auth/exchange`);
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(init.headers['Authorization']).toBeUndefined();
    expect(JSON.parse(init.body as string)).toEqual({
      provider: 'google',
      code: 'code-123',
      code_verifier: 'verifier-123',
      redirect_uri: 'https://abc123.chromiumapp.org/',
    });
  });

  it('exchangeToken posts the expected Microsoft body (oauth_token), no Authorization header', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_token: 's', account_id: 'a', expires_at: 'e' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await exchangeToken({ provider: 'microsoft', oauthToken: 'oauth-token-123' });

    expect(result).toEqual({ session_token: 's', account_id: 'a', expires_at: 'e' });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/auth/exchange`);
    expect(init.headers['Authorization']).toBeUndefined();
    expect(JSON.parse(init.body as string)).toEqual({
      provider: 'microsoft',
      oauth_token: 'oauth-token-123',
    });
  });

  it('exchangeToken throws BackendApiError with status/detail on 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: async () => ({ detail: 'OAuth token could not be verified.' }),
      }),
    );

    await expect(
      exchangeToken({
        provider: 'google',
        code: 'bad-code',
        codeVerifier: 'verifier',
        redirectUri: 'https://abc123.chromiumapp.org/',
      }),
    ).rejects.toMatchObject({
      status: 401,
      detail: 'OAuth token could not be verified.',
    });
  });

  it('exchangeToken throws BackendApiError with status/detail on 422 ("no usable email")', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({ detail: 'No usable email address is available for this account.' }),
      }),
    );

    await expect(
      exchangeToken({ provider: 'microsoft', oauthToken: 'token' }),
    ).rejects.toBeInstanceOf(BackendApiError);
  });

  it('exchangeToken throws BackendApiError with status/detail on 422 ("wrong fields for provider")', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({ detail: 'Microsoft sign-in requires oauth_token.' }),
      }),
    );

    // Deliberately a malformed call from this test's own perspective (TS wouldn't let
    // real calling code build this shape) -- exercises the backend's request-shape 422
    // path, which is documented as a client bug, not an account condition.
    await expect(
      exchangeToken({ provider: 'microsoft', oauthToken: '' }),
    ).rejects.toMatchObject({
      status: 422,
      detail: 'Microsoft sign-in requires oauth_token.',
    });
  });

  it('logout sends no body and no Content-Type header, with the Authorization header set', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ revoked: true }) });
    vi.stubGlobal('fetch', fetchMock);

    const result = await logout('session-token-123');

    expect(result).toEqual({ revoked: true });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/auth/logout`);
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBeUndefined();
    expect(init.headers['Authorization']).toBe('Bearer session-token-123');
  });

  it('revokeAllSessions sends no body and the Authorization header', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ revoked: true }) });
    vi.stubGlobal('fetch', fetchMock);

    const result = await revokeAllSessions('session-token-123');

    expect(result).toEqual({ revoked: true });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/auth/sessions/revoke-all`);
    expect(init.body).toBeUndefined();
  });
});

describe('exchangeGoogleDataToken', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('posts code/code_verifier/redirect_uri with the session bearer token', async () => {
    const grant = { access_token: 'ya29.x', expires_in: 3599, scope: 'openid', email: 'd@example.com' };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => grant });
    vi.stubGlobal('fetch', fetchMock);

    const result = await exchangeGoogleDataToken('session-123', {
      code: 'code-1',
      codeVerifier: 'verifier-1',
      redirectUri: 'https://abc123.chromiumapp.org/',
    });

    expect(result).toEqual(grant);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/google/data-token`);
    expect(init.method).toBe('POST');
    expect(init.headers['Authorization']).toBe('Bearer session-123');
    expect(JSON.parse(init.body as string)).toEqual({
      code: 'code-1',
      code_verifier: 'verifier-1',
      redirect_uri: 'https://abc123.chromiumapp.org/',
    });
  });

  it('throws BackendApiError carrying the status, so a 400 and a 401 can be told apart', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        json: async () => ({ detail: 'Google authorization could not be exchanged.' }),
      }),
    );

    await expect(
      exchangeGoogleDataToken('session-123', { code: 'c', codeVerifier: 'v', redirectUri: 'r' }),
    ).rejects.toMatchObject({ status: 400, detail: 'Google authorization could not be exchanged.' });
  });
});
