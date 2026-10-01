import { DATA_GRANT_CONFIG, GOOGLE_SHEETS_READONLY_SCOPE, type AuthProvider } from './authConfig';
import {
  AuthFlowError,
  exchangeCodeForToken,
  runAuthorizationCodeFlow,
  type AuthorizationCode,
} from './authFlow';
import { exchangeGoogleDataToken } from './backendApi';
import { getCachedDataToken, getDataAccountEmail, setDataGrant } from './dataAccessStorage';

/**
 * getDataToken: the one way this extension gets a provider token that can read a
 * spreadsheet (chrome-extension-design.md SS2 "Data-access tokens"). Sign-in tokens are
 * identity-only and never kept, so every data read goes through here:
 *
 *   cache (storage.session) -> silent (prompt=none + login_hint) -> interactive
 *   (prompt=select_account)
 *
 * all inside the user click that asked for the read. Nothing here runs on a timer, on panel
 * open, or after an error (SS6 step 5).
 */

export class UserClickRequiredError extends Error {}

/** Google's granular consent let the person untick the Sheets scope. */
export class ScopeNotGrantedError extends Error {}

/**
 * The silent attempt failed and then the interactive window didn't complete -- e.g. Chrome
 * wouldn't open it after a slow silent timeout used up the click. The person just needs to
 * click again; this must never fail silently.
 */
export class InteractiveAfterSilentFailedError extends Error {}

export interface DataToken {
  accessToken: string;
  email: string;
  scope: string;
}

// A cached token this close to expiry is treated as expired, so a read never starts with a
// token that dies halfway through it.
const EXPIRY_MARGIN_MS = 5 * 60 * 1000;

const MICROSOFT_ME_URL = 'https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName';

interface Grant extends DataToken {
  expiresIn: number;
}

export async function getDataToken(
  provider: AuthProvider,
  opts: { sessionToken: string; click: Event | undefined },
): Promise<DataToken> {
  // A runtime check of the "only inside a direct user click" rule, before anything can
  // reach browser.identity. isTrusted is false for script-dispatched events.
  if (!opts.click?.isTrusted) {
    throw new UserClickRequiredError('A data token can only be requested from a user click.');
  }

  const email = await getDataAccountEmail(provider);
  const cached = await getCachedDataToken(provider);
  if (cached && email && cached.expiresAt - EXPIRY_MARGIN_MS > Date.now()) {
    return { accessToken: cached.accessToken, email, scope: cached.scope };
  }

  const { scopes, extraParams } = DATA_GRANT_CONFIG[provider];

  // Silent first, only when there's a known data account to hint. No stored email means no
  // grant yet (or it was forgotten), and the spike showed prompt=none can't succeed then.
  let silentAttempted = false;
  if (email) {
    silentAttempted = true;
    let authorization: AuthorizationCode | null = null;
    try {
      authorization = await runAuthorizationCodeFlow({
        provider,
        scopes,
        extraParams: { ...extraParams, prompt: 'none', login_hint: email },
        interactive: false,
      });
    } catch (err) {
      if (!(err instanceof AuthFlowError)) throw err;
      console.info('[dataToken] silent attempt failed; falling back to interactive', {
        provider,
        providerError: err.providerError ?? null,
      });
    }
    if (authorization) {
      // Exchange errors (a backend 401/400, a Graph failure) propagate: they aren't fixed by
      // showing an account chooser.
      const grant = await exchangeGrant(provider, authorization, opts.sessionToken);
      if (hasRequiredScope(provider, grant.scope)) return storeGrant(provider, grant);
      console.info('[dataToken] silent grant lacked the Sheets scope; falling back to interactive');
    }
  }

  let authorization: AuthorizationCode;
  try {
    authorization = await runAuthorizationCodeFlow({
      provider,
      scopes,
      extraParams: { ...extraParams, prompt: 'select_account' },
      interactive: true,
    });
  } catch (err) {
    // A provider-reported error (e.g. access_denied on the consent screen) is a real answer
    // and keeps its own message. A bare launch failure after a silent attempt gets the
    // "click again" error instead of a generic cancel.
    if (silentAttempted && err instanceof AuthFlowError && !err.providerError) {
      console.warn('[dataToken] interactive launch failed after a silent attempt', err);
      throw new InteractiveAfterSilentFailedError(
        "Couldn't open the sign-in window — click Connect data again.",
      );
    }
    throw err;
  }

  const grant = await exchangeGrant(provider, authorization, opts.sessionToken);
  if (!hasRequiredScope(provider, grant.scope)) {
    // Store nothing -- not even the email. A remembered email would make the next click
    // silently fetch the same partial grant, and the consent screen would never return.
    throw new ScopeNotGrantedError(
      "Sheets access wasn't granted. Click Connect data and leave the Sheets box ticked.",
    );
  }
  return storeGrant(provider, grant);
}

function hasRequiredScope(provider: AuthProvider, scope: string): boolean {
  // Only Google's consent screen lets a person untick individual scopes.
  return provider !== 'google' || scope.split(' ').includes(GOOGLE_SHEETS_READONLY_SCOPE);
}

async function exchangeGrant(
  provider: AuthProvider,
  authorization: AuthorizationCode,
  sessionToken: string,
): Promise<Grant> {
  if (provider === 'google') {
    const response = await exchangeGoogleDataToken(sessionToken, authorization);
    return {
      accessToken: response.access_token,
      expiresIn: response.expires_in,
      scope: response.scope,
      email: response.email,
    };
  }

  // Microsoft's refresh_token never gets this far: exchangeCodeForToken doesn't read it.
  const token = await exchangeCodeForToken('microsoft', authorization);
  // Not a secret; it's the evidence for which previously consented scopes ride along on a
  // Files.Read request (chrome-extension-design.md SS2).
  console.info('[dataToken] Microsoft data token granted scope:', token.scope);
  return { ...token, email: await fetchMicrosoftDataEmail(token.accessToken) };
}

/** The data account's login_hint: Graph /me's `mail`, falling back to userPrincipalName. */
async function fetchMicrosoftDataEmail(accessToken: string): Promise<string> {
  const response = await fetch(MICROSOFT_ME_URL, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    throw new AuthFlowError('Could not read which Microsoft account was connected.');
  }
  const me = (await response.json()) as { mail?: string | null; userPrincipalName?: string | null };
  const email = me.mail || me.userPrincipalName;
  if (!email) {
    throw new AuthFlowError('Could not read which Microsoft account was connected.');
  }
  return email;
}

async function storeGrant(provider: AuthProvider, grant: Grant): Promise<DataToken> {
  await setDataGrant(
    provider,
    {
      accessToken: grant.accessToken,
      expiresAt: Date.now() + grant.expiresIn * 1000,
      scope: grant.scope,
    },
    grant.email,
  );
  return { accessToken: grant.accessToken, email: grant.email, scope: grant.scope };
}
