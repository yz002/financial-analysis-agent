import { BACKEND_BASE_URL } from './authConfig';

export class BackendApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail: unknown,
  ) {
    super(message);
  }
}

export interface ExchangeTokenResponse {
  session_token: string;
  account_id: string;
  expires_at: string;
}

interface PostJsonOptions {
  body?: unknown;
  sessionToken?: string;
}

async function postJson<T>(path: string, opts: PostJsonOptions): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.sessionToken) headers['Authorization'] = `Bearer ${opts.sessionToken}`;

  const response = await fetch(`${BACKEND_BASE_URL}${path}`, {
    method: 'POST',
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  let json: unknown = null;
  try {
    json = await response.json();
  } catch {
    // no/invalid body
  }

  if (!response.ok) {
    const detail =
      json && typeof json === 'object' && 'detail' in (json as object)
        ? (json as { detail: unknown }).detail
        : json;
    throw new BackendApiError(`${path} failed (${response.status}).`, response.status, detail);
  }

  return json as T;
}

/**
 * Request shape depends on provider (Phase D session 2 amendment) -- Google sends the
 * raw code/verifier/redirect_uri for this backend to exchange server-side (its "Web
 * application" client type requires a client_secret PKCE alone can't satisfy); Microsoft
 * sends an already-exchanged access token, since Azure's SPA platform type is a genuine
 * no-secret public client. Structurally identical to authFlow.ts's AuthFlowResult, so a
 * caller can pass launchAuthFlow's result straight through without adapting it.
 */
export type ExchangeTokenParams =
  | { provider: 'google'; code: string; codeVerifier: string; redirectUri: string }
  | { provider: 'microsoft'; oauthToken: string };

export async function exchangeToken(params: ExchangeTokenParams): Promise<ExchangeTokenResponse> {
  const body =
    params.provider === 'google'
      ? {
          provider: 'google',
          code: params.code,
          code_verifier: params.codeVerifier,
          redirect_uri: params.redirectUri,
        }
      : { provider: 'microsoft', oauth_token: params.oauthToken };

  return postJson<ExchangeTokenResponse>('/v1/auth/exchange', { body });
}

/** No request body per the contract. Always resolves {revoked: true} on 2xx. */
export async function logout(sessionToken: string): Promise<{ revoked: true }> {
  return postJson<{ revoked: true }>('/v1/auth/logout', { sessionToken });
}

/** No request body per the contract. Always resolves {revoked: true} on 2xx. */
export async function revokeAllSessions(sessionToken: string): Promise<{ revoked: true }> {
  return postJson<{ revoked: true }>('/v1/auth/sessions/revoke-all', { sessionToken });
}
