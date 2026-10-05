import { BACKEND_BASE_URL } from './authConfig';
import type { CsvParseRequest } from './cellGrid';
import type { ConfirmRequestBody, ProposalEntry } from './mappingModel';

export class BackendApiError extends Error {
  /**
   * `fromBackend` is false when the error response didn't come from this backend's own error
   * handling: no JSON body, or JSON without FastAPI's `detail` key. The usual case is a bare
   * 502/504 from Render's proxy (EXTENSION_INTEGRATION.md SS6 /v1/ask, "Long answers and
   * recovery"): a gateway or server problem, not an answer to the request.
   */
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail: unknown,
    public readonly fromBackend = true,
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
  signal?: AbortSignal;
}

async function postJson<T>(path: string, opts: PostJsonOptions): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.sessionToken) headers['Authorization'] = `Bearer ${opts.sessionToken}`;

  const response = await fetch(`${BACKEND_BASE_URL}${path}`, {
    method: 'POST',
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });

  let json: unknown = null;
  try {
    json = await response.json();
  } catch {
    // no/invalid body
  }

  if (!response.ok) {
    const fromBackend = json !== null && typeof json === 'object' && 'detail' in (json as object);
    const detail = fromBackend ? (json as { detail: unknown }).detail : json;
    throw new BackendApiError(`${path} failed (${response.status}).`, response.status, detail, fromBackend);
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

// --- POST /v1/ask (EXTENSION_INTEGRATION.md SS6, amended session 5) ------------------------

export interface AskRequestBody {
  question: string;
  csv_context_id: string | null;
  conversation_id: string | null;
  /** One per question; resending it never runs or charges the question twice. */
  request_id: string;
}

export interface FigureCheckFigure {
  raw_text: string;
  start: number;
  end: number;
  traced: boolean;
  weak_match: boolean;
}

export interface FigureCheck {
  figures_checked?: number;
  figures_traced?: number;
  figures_untraced?: number;
  all_traced?: boolean;
  figures?: FigureCheckFigure[];
  figures_skipped?: { raw_text: string; start: number; end: number; reason: string }[];
}

/** A spreadsheet cell, a filing fact, or another tool's output (kind decides which fields). */
export interface CitationSource {
  cell?: string | null;
  concept?: string;
  column?: string | null;
  period_end?: string | null;
  read_value?: string | null;
  sheet_scale?: string | null;
  ticker?: string | null;
  tag?: string | null;
  filed?: string | null;
  is_derived?: boolean | null;
  derivation_method?: string;
  tool_name?: string;
  json_path?: string;
}

export interface CitationInput {
  role: string;
  value: number | null;
  source: CitationSource | null;
  computation?: CitationComputation;
}

export interface CitationComputation {
  name: string;
  formula: string | null;
  period_end: string | null;
  inputs: CitationInput[];
}

export interface CitationMatch {
  kind: 'cell' | 'derived' | 'filing' | 'tool';
  value: number;
  turn_id: string | null;
  source?: CitationSource;
  computation?: CitationComputation;
}

export type CitationStatus = 'traced' | 'ambiguous' | 'weak' | 'untraced';

export interface Citation {
  figure_index: number;
  raw_text: string;
  start: number;
  end: number;
  status: CitationStatus;
  matches: CitationMatch[];
}

export interface AskResponse {
  conversation_id: string;
  turn_id: string;
  final_answer: string;
  hit_iteration_cap: boolean;
  figure_check: FigureCheck;
  citations: Citation[];
  tool_calls_summary: { tool_name: string; is_error: boolean }[];
}

/**
 * POST /v1/ask. Errors are BackendApiError with the structured `detail` the contract defines
 * ({"error": "<code>"} for the session-5 errors); `fromBackend` false marks a gateway/proxy
 * response (see BackendApiError). Retrying with the same request_id is lib/askRunner.ts's job.
 */
export async function ask(
  sessionToken: string,
  body: AskRequestBody,
  signal?: AbortSignal,
): Promise<AskResponse> {
  return postJson<AskResponse>('/v1/ask', { sessionToken, body, signal });
}

/** No request body per the contract. Always resolves {revoked: true} on 2xx. */
export async function logout(sessionToken: string): Promise<{ revoked: true }> {
  return postJson<{ revoked: true }>('/v1/auth/logout', { sessionToken });
}

/** No request body per the contract. Always resolves {revoked: true} on 2xx. */
export async function revokeAllSessions(sessionToken: string): Promise<{ revoked: true }> {
  return postJson<{ revoked: true }>('/v1/auth/sessions/revoke-all', { sessionToken });
}
