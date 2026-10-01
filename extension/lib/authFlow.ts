import { createPkcePair } from './pkce';
import { PROVIDER_CONFIG, type AuthProvider } from './authConfig';

/**
 * `providerError` carries the provider's raw `error` value from the redirect (e.g.
 * `interaction_required`, `consent_required`, `login_required`) when there was one. The
 * session 3a spike showed why it matters: a silent-auth FAIL is only diagnosable from that
 * exact value, so callers log it next to every silent failure.
 */
export class AuthFlowError extends Error {
  constructor(
    message: string,
    public readonly providerError?: string,
  ) {
    super(message);
  }
}

/**
 * What launchAuthFlow hands back differs by provider (Phase D session 2 amendment):
 * Google's code-for-token exchange now happens backend-side (Google's "Web application"
 * client type is confidential -- PKCE alone doesn't satisfy its token endpoint, per
 * Google's own current OAuth 2.0 docs), so Google's result carries the raw
 * code/verifier/redirect_uri for the backend to exchange itself. Microsoft's Azure SPA
 * platform type is a genuine no-secret public client, so its result still carries an
 * already-exchanged access token, as before.
 */
export type AuthFlowResult =
  | { provider: 'google'; code: string; codeVerifier: string; redirectUri: string }
  | { provider: 'microsoft'; oauthToken: string };

export interface AuthorizationCode {
  code: string;
  codeVerifier: string;
  redirectUri: string;
}

/**
 * Exported for unit testing -- builds the full authorization URL, one branch per provider.
 * `scopes` defaults to the provider's identity-only sign-in scopes; the data grant
 * (lib/dataToken.ts) passes its own, plus `extraParams` such as prompt/login_hint.
 */
export function buildAuthorizationUrl(
  provider: AuthProvider,
  opts: {
    codeChallenge: string;
    redirectUri: string;
    scopes?: string[];
    extraParams?: Record<string, string>;
  },
): string {
  const config = PROVIDER_CONFIG[provider];
  const params = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: opts.redirectUri,
    response_type: 'code',
    scope: (opts.scopes ?? config.scopes).join(' '),
    code_challenge: opts.codeChallenge,
    code_challenge_method: 'S256',
  });
  // Pin the code to the query string explicitly for Microsoft, rather than relying on
  // the platform default, so parseAuthorizationCode can always assume it's there and
  // never in the fragment.
  if (provider === 'microsoft') {
    params.set('response_mode', 'query');
  }
  for (const [key, value] of Object.entries(opts.extraParams ?? {})) {
    params.set(key, value);
  }
  return `${config.authorizationEndpoint}?${params.toString()}`;
}

/**
 * Exported for unit testing -- extracts `code` from the URL launchWebAuthFlow resolves
 * with. Throws AuthFlowError if the redirect carries `error`/`error_description` instead
 * (user denied consent, provider-side failure, or a silent attempt that needs interaction)
 * or carries neither `code` nor `error`.
 */
export function parseAuthorizationCode(redirectUrl: string): string {
  const url = new URL(redirectUrl);
  const error = url.searchParams.get('error');
  if (error) {
    const description = url.searchParams.get('error_description');
    throw new AuthFlowError(
      description ? `Sign-in failed: ${description}` : 'Sign-in was denied.',
      error,
    );
  }
  const code = url.searchParams.get('code');
  if (!code) {
    throw new AuthFlowError('Sign-in did not return an authorization code.');
  }
  return code;
}

export interface MicrosoftTokenResult {
  accessToken: string;
  expiresIn: number;
  scope: string;
}

/**
 * Exported for unit testing -- exchanges an authorization code for an access token
 * against the provider's own token endpoint (PKCE, no client secret). Microsoft-only:
 * a genuine no-secret public-client exchange, safe to do client-side. Google's
 * equivalent exchange requires a client_secret (see the AuthFlowResult doc comment) and
 * happens backend-side instead -- deliberately not offered here for "google", so a
 * future caller can't accidentally reintroduce the insecure client-side path.
 *
 * Only access_token/expires_in/scope are read from the response. Microsoft returns a
 * refresh_token even without offline_access (session 3a spike); it is never read, so it
 * never leaves this function.
 */
export async function exchangeCodeForToken(
  provider: 'microsoft',
  params: AuthorizationCode,
): Promise<MicrosoftTokenResult> {
  const config = PROVIDER_CONFIG[provider];
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: config.clientId,
    code: params.code,
    code_verifier: params.codeVerifier,
    redirect_uri: params.redirectUri,
  });

  const response = await fetch(config.tokenEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  });

  if (!response.ok) {
    // The raw provider error body is never surfaced to the UI -- a generic message is
    // enough for the person to know what to retry.
    throw new AuthFlowError('Microsoft token exchange failed.');
  }

  const json = (await response.json()) as {
    access_token?: string;
    expires_in?: number;
    scope?: string;
  };
  if (!json.access_token) {
    throw new AuthFlowError('Token exchange response did not include an access token.');
  }
  return {
    accessToken: json.access_token,
    expiresIn: Number(json.expires_in ?? 0),
    scope: json.scope ?? '',
  };
}

/**
 * The shared Authorization Code + PKCE step behind both sign-in and the data grant: PKCE
 * pair -> redirect URI -> authorization URL -> launchWebAuthFlow -> parse code.
 * codeVerifier is a plain local variable held across the single await on
 * launchWebAuthFlow -- never persisted.
 *
 * Only ever reached from a user click (chrome-extension-design.md SS6 step 5): sign-in
 * buttons via launchAuthFlow, and the data grant via getDataToken's click guard.
 * `interactive: false` is the silent attempt (prompt=none), with the session 3a spike's
 * non-interactive settings.
 */
export async function runAuthorizationCodeFlow(opts: {
  provider: AuthProvider;
  scopes?: string[];
  extraParams?: Record<string, string>;
  interactive: boolean;
}): Promise<AuthorizationCode> {
  const { codeVerifier, codeChallenge } = await createPkcePair();
  const redirectUri = browser.identity.getRedirectURL();
  const url = buildAuthorizationUrl(opts.provider, {
    codeChallenge,
    redirectUri,
    scopes: opts.scopes,
    extraParams: opts.extraParams,
  });

  let redirectedTo: string | undefined;
  try {
    redirectedTo = await browser.identity.launchWebAuthFlow(
      opts.interactive
        ? { url, interactive: true }
        : {
            url,
            interactive: false,
            abortOnLoadForNonInteractive: false,
            timeoutMsForNonInteractive: 10000,
          },
    );
  } catch (err) {
    // launchWebAuthFlow rejects on user-cancel, popup-blocked, provider load failure, or a
    // silent attempt that timed out -- the underlying error message is not reliably
    // user-presentable, so normalize it, but keep it in the console.
    console.warn('[authFlow] launchWebAuthFlow rejected', { interactive: opts.interactive }, err);
    throw new AuthFlowError('Sign-in was cancelled or the authorization window could not be opened.');
  }
  if (!redirectedTo) {
    throw new AuthFlowError('Sign-in did not complete.');
  }

  return { code: parseAuthorizationCode(redirectedTo), codeVerifier, redirectUri };
}

/**
 * The one function the sign-in buttons call: an interactive, identity-scopes-only
 * runAuthorizationCodeFlow, then (Microsoft only) the token exchange. See
 * AuthFlowResult's doc comment for why Google's and Microsoft's results carry different
 * fields.
 */
export async function launchAuthFlow(provider: AuthProvider): Promise<AuthFlowResult> {
  const authorization = await runAuthorizationCodeFlow({ provider, interactive: true });

  if (provider === 'google') {
    // Google's Web application client type requires a client_secret at the token
    // endpoint (see AuthFlowResult's doc comment) -- send the raw code/verifier/
    // redirect_uri to the backend unexchanged, rather than exchanging it here.
    return { provider, ...authorization };
  }

  const { accessToken, scope } = await exchangeCodeForToken(provider, authorization);
  // Not a secret, and it's the evidence for whether Microsoft carries previously consented
  // data scopes (Files.Read/Files.ReadWrite) into an identity-only sign-in token -- see
  // chrome-extension-design.md SS2. The token itself is never logged.
  console.info('[authFlow] Microsoft sign-in token granted scope:', scope);
  return { provider, oauthToken: accessToken };
}
