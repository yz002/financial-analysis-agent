import { createPkcePair } from './pkce';
import { PROVIDER_CONFIG, type AuthProvider } from './authConfig';

export class AuthFlowError extends Error {}

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

/** Exported for unit testing -- builds the full authorization URL, one branch per provider. */
export function buildAuthorizationUrl(
  provider: AuthProvider,
  opts: { codeChallenge: string; redirectUri: string },
): string {
  const config = PROVIDER_CONFIG[provider];
  const params = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: opts.redirectUri,
    response_type: 'code',
    scope: config.scopes.join(' '),
    code_challenge: opts.codeChallenge,
    code_challenge_method: 'S256',
  });
  // Pin the code to the query string explicitly for Microsoft, rather than relying on
  // the platform default, so parseAuthorizationCode can always assume it's there and
  // never in the fragment.
  if (provider === 'microsoft') {
    params.set('response_mode', 'query');
  }
  return `${config.authorizationEndpoint}?${params.toString()}`;
}

/**
 * Exported for unit testing -- extracts `code` from the URL launchWebAuthFlow resolves
 * with. Throws AuthFlowError if the redirect carries `error`/`error_description` instead
 * (user denied consent, provider-side failure) or carries neither `code` nor `error`.
 */
export function parseAuthorizationCode(redirectUrl: string): string {
  const url = new URL(redirectUrl);
  const error = url.searchParams.get('error');
  if (error) {
    const description = url.searchParams.get('error_description');
    throw new AuthFlowError(description ? `Sign-in failed: ${description}` : 'Sign-in was denied.');
  }
  const code = url.searchParams.get('code');
  if (!code) {
    throw new AuthFlowError('Sign-in did not return an authorization code.');
  }
  return code;
}

/**
 * Exported for unit testing -- exchanges an authorization code for an access token
 * against the provider's own token endpoint (PKCE, no client secret). Microsoft-only:
 * a genuine no-secret public-client exchange, safe to do client-side. Google's
 * equivalent exchange requires a client_secret (see the AuthFlowResult doc comment) and
 * happens backend-side instead -- deliberately not offered here for "google", so a
 * future caller can't accidentally reintroduce the insecure client-side path.
 */
export async function exchangeCodeForToken(
  provider: 'microsoft',
  params: { code: string; codeVerifier: string; redirectUri: string },
): Promise<string> {
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

  const json = (await response.json()) as { access_token?: string };
  if (!json.access_token) {
    throw new AuthFlowError('Token exchange response did not include an access token.');
  }
  return json.access_token;
}

/**
 * The one function the UI calls. Orchestrates: PKCE pair -> redirect URI ->
 * authorization URL -> browser.identity.launchWebAuthFlow -> parse code -> (Microsoft
 * only) exchange for a token. codeVerifier is a plain local variable held across the
 * single await on launchWebAuthFlow -- never persisted. See AuthFlowResult's doc
 * comment for why Google's and Microsoft's results carry different fields.
 */
export async function launchAuthFlow(provider: AuthProvider): Promise<AuthFlowResult> {
  const { codeVerifier, codeChallenge } = await createPkcePair();
  const redirectUri = browser.identity.getRedirectURL();
  const authUrl = buildAuthorizationUrl(provider, { codeChallenge, redirectUri });

  let redirectedTo: string | undefined;
  try {
    // TEMPORARY DEBUG LOGGING -- remove once the live sign-in flow is confirmed working.
    console.log('[authFlow] launching interactive auth flow', { provider, redirectUri, authUrl });
    redirectedTo = await browser.identity.launchWebAuthFlow({ url: authUrl, interactive: true });
  } catch (err) {
    // TEMPORARY: log the real underlying rejection before normalizing it away below --
    // launchWebAuthFlow's actual error (e.g. from chrome.runtime.lastError) is otherwise
    // discarded, which is exactly what's hiding the root cause right now.
    console.error('[authFlow] launchWebAuthFlow rejected', err);
    // launchWebAuthFlow rejects on user-cancel, popup-blocked, or provider load failure --
    // the underlying error message is not reliably user-presentable, so normalize it.
    throw new AuthFlowError('Sign-in was cancelled or the authorization window could not be opened.');
  }
  if (!redirectedTo) {
    throw new AuthFlowError('Sign-in did not complete.');
  }

  const code = parseAuthorizationCode(redirectedTo);

  if (provider === 'google') {
    // Google's Web application client type requires a client_secret at the token
    // endpoint (see AuthFlowResult's doc comment) -- send the raw code/verifier/
    // redirect_uri to the backend unexchanged, rather than exchanging it here.
    return { provider, code, codeVerifier, redirectUri };
  }

  const oauthToken = await exchangeCodeForToken(provider, { code, codeVerifier, redirectUri });
  return { provider, oauthToken };
}
