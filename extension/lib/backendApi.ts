import { BACKEND_BASE_URL } from './authConfig';
import type { CsvParseRequest } from './cellGrid';
import type { ConfirmRequestBody, ProposalEntry } from './mappingModel';

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

export interface GoogleDataTokenResponse {
  access_token: string;
  expires_in: number;
  scope: string;
  email: string;
}

/**
 * POST /v1/google/data-token (EXTENSION_INTEGRATION.md SS1a): the backend exchanges a Google
 * data-grant code with its client secret and returns the token. A 401 means the *session*
 * failed (discard it); a 400 means only the data grant failed (keep the session).
 */
export async function exchangeGoogleDataToken(
  sessionToken: string,
  params: { code: string; codeVerifier: string; redirectUri: string },
): Promise<GoogleDataTokenResponse> {
  return postJson<GoogleDataTokenResponse>('/v1/google/data-token', {
    sessionToken,
    body: {
      code: params.code,
      code_verifier: params.codeVerifier,
      redirect_uri: params.redirectUri,
    },
  });
}

export interface CsvParseResponse {
  csv_context_id: string | null;
  columns: string[];
  sample_rows: string[][];
  parse_error: string | null;
}

/**
 * POST /v1/csv/parse (EXTENSION_INTEGRATION.md SS6, amended 3b). Always 200 for a well-formed
 * request: a structural refusal comes back in-band as parse_error, never as an HTTP error.
 */
export async function parseCsv(
  sessionToken: string,
  request: CsvParseRequest,
): Promise<CsvParseResponse> {
  return postJson<CsvParseResponse>('/v1/csv/parse', { sessionToken, body: request });
}

export interface ProposeMappingResponse {
  proposal: ProposalEntry[];
  note: string | null;
}

/**
 * POST /v1/csv/{id}/propose-mapping (EXTENSION_INTEGRATION.md SS6, amended session 4). No
 * body. Idempotent per context; 404 for a missing or expired context, 409 once confirmed,
 * 429 `mapping_cap_reached`, 502/500 when the model call fails. A proposal is only a starting
 * point: the person reviews every role before /confirm.
 */
export async function proposeMapping(
  sessionToken: string,
  csvContextId: string,
): Promise<ProposeMappingResponse> {
  return postJson<ProposeMappingResponse>(
    `/v1/csv/${encodeURIComponent(csvContextId)}/propose-mapping`,
    { sessionToken },
  );
}

export interface UnparsedCell {
  cell: string | null;
  source_row: number;
  column: string;
  role: string;
  period_end: string;
  value: string;
}

export interface ConfirmMappingResponse {
  confirmed: boolean;
  cadence: string | null;
  warnings: string[];
  concepts_unavailable: string[];
  errors: string[];
  requires_acknowledgement: boolean;
  unparsed_cells: UnparsedCell[];
  ack_fingerprint: string | null;
  scale: string | null;
  currency: string | null;
}

/**
 * POST /v1/csv/{id}/confirm (amended session 4). A validation failure is an in-band 200 with
 * `confirmed: false`: `errors[]`, or `requires_acknowledgement` with the cells to show and the
 * fingerprint to send back. 404 for a missing or expired context, 409 once confirmed.
 */
export async function confirmMapping(
  sessionToken: string,
  csvContextId: string,
  body: ConfirmRequestBody,
): Promise<ConfirmMappingResponse> {
  return postJson<ConfirmMappingResponse>(`/v1/csv/${encodeURIComponent(csvContextId)}/confirm`, {
    sessionToken,
    body,
  });
}

/** No request body per the contract. Always resolves {revoked: true} on 2xx. */
export async function logout(sessionToken: string): Promise<{ revoked: true }> {
  return postJson<{ revoked: true }>('/v1/auth/logout', { sessionToken });
}

/** No request body per the contract. Always resolves {revoked: true} on 2xx. */
export async function revokeAllSessions(sessionToken: string): Promise<{ revoked: true }> {
  return postJson<{ revoked: true }>('/v1/auth/sessions/revoke-all', { sessionToken });
}
