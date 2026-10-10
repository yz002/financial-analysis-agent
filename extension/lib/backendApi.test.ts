import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ask,
  BackendApiError,
  confirmMapping,
  createCheckoutSession,
  createPortalSession,
  getUsage,
  registerByoKey,
  removeByoKey,
  exchangeGoogleDataToken,
  exchangeToken,
  logout,
  proposeMapping,
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

describe('mapping calls (session 4)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('proposeMapping posts no body to the context, with the bearer token', async () => {
    const proposal = { proposal: [{ csv_column: 'Q', proposed_role: 'period_end', rationale: 'r' }], note: null };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => proposal });
    vi.stubGlobal('fetch', fetchMock);

    expect(await proposeMapping('session-123', 'ctx-1')).toEqual(proposal);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/csv/ctx-1/propose-mapping`);
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBeUndefined();
    expect(init.headers['Authorization']).toBe('Bearer session-123');
  });

  it('proposeMapping surfaces a 429 with its detail object', async () => {
    const detail = { error: 'mapping_cap_reached', resets_at: '2026-10-04T10:00:00+00:00' };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 429, json: async () => ({ detail }) }));
    await expect(proposeMapping('s', 'ctx-1')).rejects.toMatchObject({ status: 429, detail });
  });

  it('confirmMapping posts the body as given', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ confirmed: true }) });
    vi.stubGlobal('fetch', fetchMock);
    const body = {
      mapping: { Q: 'period_end', R: 'revenue' },
      entity_name: 'Co',
      scale: 'thousands' as const,
      currency: null,
      accept_unparsed_cells: false,
      ack_fingerprint: null,
    };

    await confirmMapping('session-123', 'ctx-1', body);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/csv/ctx-1/confirm`);
    expect(init.headers['Authorization']).toBe('Bearer session-123');
    expect(JSON.parse(init.body as string)).toEqual(body);
  });

  it('confirmMapping throws BackendApiError on a 409', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail: 'csv context already confirmed' }) }),
    );
    await expect(
      confirmMapping('s', 'ctx-1', {
        mapping: {}, entity_name: 'Co', scale: 'ones', currency: null,
        accept_unparsed_cells: false, ack_fingerprint: null,
      }),
    ).rejects.toBeInstanceOf(BackendApiError);
  });
});

describe('ask (session 5)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  const BODY = {
    question: 'What was revenue?',
    csv_context_id: 'ctx-1',
    conversation_id: null,
    request_id: '6f1c2c1e-7a55-4b5e-9f66-2b0c5f6e9d10',
  };

  it('posts the question with its request id, bearer token and abort signal', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ conversation_id: 'c' }) });
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    await ask('session-token', BODY, controller.signal);

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/ask`);
    expect(init.method).toBe('POST');
    expect(init.headers['Authorization']).toBe('Bearer session-token');
    expect(JSON.parse(init.body as string)).toEqual(BODY);
    expect(init.signal).toBe(controller.signal);
  });

  it("a backend error keeps its structured detail and is marked as the backend's own", async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 504, json: async () => ({ detail: { error: 'answer_time_budget_exceeded' } }),
    }));
    const error = await ask('t', BODY).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(BackendApiError);
    expect((error as BackendApiError).status).toBe(504);
    expect((error as BackendApiError).detail).toEqual({ error: 'answer_time_budget_exceeded' });
    expect((error as BackendApiError).fromBackend).toBe(true);
  });

  it('a bodyless or non-JSON gateway 502/504 is marked as not from the backend', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 502, json: async () => { throw new SyntaxError('Unexpected token <'); },
    }));
    const error = (await ask('t', BODY).catch((e: unknown) => e)) as BackendApiError;
    expect(error.status).toBe(502);
    expect(error.fromBackend).toBe(false);

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 504, json: async () => ({ message: 'upstream timed out' }),
    }));
    const other = (await ask('t', BODY).catch((e: unknown) => e)) as BackendApiError;
    expect(other.fromBackend).toBe(false);
  });
});

describe('usage, billing and BYO key (Phase D session 6)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(json: unknown) {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => json });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('getUsage is a GET with the session token and no body', async () => {
    const fetchMock = stubFetch({ tier: 'free' });
    await getUsage('tok');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/usage`);
    expect(init.method).toBe('GET');
    expect(init.headers['Authorization']).toBe('Bearer tok');
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBeUndefined();
  });

  it('createCheckoutSession and createPortalSession are bodyless authenticated POSTs', async () => {
    const fetchMock = stubFetch({ checkout_url: 'https://checkout.stripe.com/x' });
    expect(await createCheckoutSession('tok')).toEqual({ checkout_url: 'https://checkout.stripe.com/x' });
    stubFetch({});
    const portalFetch = stubFetch({ portal_url: 'https://billing.stripe.com/p' });
    expect(await createPortalSession('tok')).toEqual({ portal_url: 'https://billing.stripe.com/p' });

    for (const [mock, path] of [
      [fetchMock, '/v1/billing/checkout-session'],
      [portalFetch, '/v1/billing/portal-session'],
    ] as const) {
      const [url, init] = mock.mock.calls[0]!;
      expect(url).toBe(`${BACKEND_BASE_URL}${path}`);
      expect(init.method).toBe('POST');
      expect(init.headers['Authorization']).toBe('Bearer tok');
      expect(init.body).toBeUndefined();
    }
  });

  it('registerByoKey sends the key once, in the body', async () => {
    const fetchMock = stubFetch({ registered: true });
    await registerByoKey('tok', 'sk-ant-api03-abc');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/byo-key`);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ api_key: 'sk-ant-api03-abc' });
    expect(String(url)).not.toContain('sk-ant');
  });

  it('removeByoKey is an authenticated DELETE with no body', async () => {
    const fetchMock = stubFetch({ removed: true });
    expect(await removeByoKey('tok')).toEqual({ removed: true });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`${BACKEND_BASE_URL}/v1/byo-key`);
    expect(init.method).toBe('DELETE');
    expect(init.headers['Authorization']).toBe('Bearer tok');
    expect(init.body).toBeUndefined();
  });

  it('a billing error keeps its structured detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 409, json: async () => ({ detail: { error: 'subscription_exists' } }),
    }));
    const error = (await createCheckoutSession('tok').catch((e: unknown) => e)) as BackendApiError;
    expect(error.status).toBe(409);
    expect(error.detail).toEqual({ error: 'subscription_exists' });
  });
});
