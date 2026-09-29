import { launchAuthFlow, AuthFlowError } from '../../lib/authFlow';
import { exchangeToken, logout, revokeAllSessions, BackendApiError } from '../../lib/backendApi';
import {
  getStoredSession,
  setStoredSession,
  clearStoredSession,
  onStoredSessionChanged,
  type StoredSession,
} from '../../lib/sessionStorage';
import { BACKEND_BASE_URL, type AuthProvider } from '../../lib/authConfig';

// Every build mode loads from the same unpacked folder (see wxt.config.ts's
// outDirTemplate), so which backend a loaded build talks to isn't otherwise visible.
console.info('[sidepanel] Backend:', BACKEND_BASE_URL);
// Non-production builds also show it in the panel itself; production renders nothing
// extra. MODE is replaced at build time, so a production bundle drops this block.
if (import.meta.env.MODE !== 'production') {
  const backendInfo = document.createElement('p');
  backendInfo.id = 'backend-info';
  backendInfo.textContent = `Backend: ${BACKEND_BASE_URL}`;
  document.querySelector('#app')?.append(backendInfo);
}

const signedOutView = document.querySelector<HTMLElement>('#signed-out-view');
const signedInView = document.querySelector<HTMLElement>('#signed-in-view');
const statusMessage = document.querySelector<HTMLElement>('#status-message');
const accountInfo = document.querySelector<HTMLElement>('#account-info');
const signinGoogleButton = document.querySelector<HTMLButtonElement>('#signin-google');
const signinMicrosoftButton = document.querySelector<HTMLButtonElement>('#signin-microsoft');
const signoutButton = document.querySelector<HTMLButtonElement>('#signout');
const revokeAllButton = document.querySelector<HTMLButtonElement>('#revoke-all');

const PROVIDER_LABEL: Record<AuthProvider, string> = {
  google: 'Google',
  microsoft: 'Microsoft',
};

function setStatus(message: string, isError = false): void {
  if (!statusMessage) return;
  statusMessage.textContent = message;
  statusMessage.classList.toggle('status-message--error', isError);
}

function clearStatus(): void {
  setStatus('');
}

function renderSignedOut(errorMessage?: string): void {
  signedOutView?.removeAttribute('hidden');
  signedInView?.setAttribute('hidden', '');
  if (errorMessage) {
    setStatus(errorMessage, true);
  } else {
    clearStatus();
  }
}

function renderSignedIn(session: StoredSession, errorMessage?: string): void {
  signedOutView?.setAttribute('hidden', '');
  signedInView?.removeAttribute('hidden');
  if (accountInfo) {
    accountInfo.textContent = `Signed in via ${PROVIDER_LABEL[session.provider]} · account ${session.accountId}`;
  }
  if (errorMessage) {
    setStatus(errorMessage, true);
  } else {
    clearStatus();
  }
}

function setBusy(busy: boolean): void {
  if (signinGoogleButton) signinGoogleButton.disabled = busy;
  if (signinMicrosoftButton) signinMicrosoftButton.disabled = busy;
  if (signoutButton) signoutButton.disabled = busy;
  if (revokeAllButton) revokeAllButton.disabled = busy;
}

function friendlyMessage(err: unknown): string {
  if (err instanceof AuthFlowError) return err.message;
  if (err instanceof BackendApiError) {
    if (err.status === 401) return 'Could not verify that sign-in — please try again.';
    if (err.status === 422) {
      // Two genuinely different 422 causes (see backend/EXTENSION_INTEGRATION.md SS1):
      // a real account-state condition (no usable email -- Microsoft-only, matched by
      // its exact detail string), vs. a "wrong fields for this provider" request-shape
      // error, which means a bug in this file's own request-building, not an account
      // problem, and never something a retry or re-sign-in would resolve -- so it gets
      // the same generic copy as any other unexpected failure, not a tailored message.
      if (err.detail === 'No usable email address is available for this account.') {
        return "This Microsoft account doesn't have a usable email address for sign-in.";
      }
      return 'Sign-in failed — please try again.';
    }
    return 'Sign-in failed — please try again.';
  }
  return 'Something went wrong — please try again.';
}

async function handleSignIn(provider: AuthProvider): Promise<void> {
  setBusy(true);
  setStatus(`Signing in with ${PROVIDER_LABEL[provider]}…`);
  try {
    const flowResult = await launchAuthFlow(provider);
    const response = await exchangeToken(flowResult);
    const session: StoredSession = {
      sessionToken: response.session_token,
      accountId: response.account_id,
      expiresAt: response.expires_at,
      provider,
    };
    // Read whatever was stored BEFORE overwriting it -- needed below to best-effort
    // revoke it. This whole block only runs once the new sign-in has already fully
    // succeeded (launchAuthFlow/exchangeToken above throw first otherwise), so a
    // cancelled or failed sign-in never touches an existing session.
    const previousSession = await getStoredSession();
    await setStoredSession(session);
    renderSignedIn(session);
    if (previousSession && previousSession.sessionToken !== session.sessionToken) {
      try {
        await logout(previousSession.sessionToken);
      } catch (err) {
        // Best-effort only -- the new session is already stored and rendered above
        // regardless of whether this succeeds. A failure here (including a 401,
        // meaning the old token was already invalid) never touches the new session.
        console.warn('[sidepanel] best-effort revoke of previous session failed', err);
      }
    }
  } catch (err) {
    console.error('[sidepanel] sign-in failed', err);
    renderSignedOut(friendlyMessage(err));
  } finally {
    setBusy(false);
  }
}

async function handleSignOut(): Promise<void> {
  const session = await getStoredSession();
  setBusy(true);
  setStatus('Signing out…');
  try {
    if (session) await logout(session.sessionToken);
  } catch (err) {
    if (!(err instanceof BackendApiError && err.status === 401)) {
      // A 401 here just means the token was already invalid -- expected, not worth a
      // warning. Anything else is a genuine, unexpected failure worth logging, even
      // though local state is cleared regardless either way (below).
      console.warn('[sidepanel] logout call did not succeed cleanly', err);
    }
  } finally {
    await clearStoredSession();
    setBusy(false);
    renderSignedOut();
  }
}

async function handleRevokeAll(): Promise<void> {
  const session = await getStoredSession();
  if (!session) {
    renderSignedOut();
    return;
  }
  setBusy(true);
  setStatus('Signing out everywhere…');
  try {
    await revokeAllSessions(session.sessionToken);
    await clearStoredSession();
    renderSignedOut();
  } catch (err) {
    if (err instanceof BackendApiError && err.status === 401) {
      // Per the backend's 401 contract: the token was already invalid -- the outcome
      // this action wanted anyway.
      await clearStoredSession();
      renderSignedOut();
    } else {
      // Any other failure means it's genuinely unknown whether other devices were
      // signed out -- keep the local session rather than discarding state that may
      // still be valid, and say so plainly instead of failing silently. This never
      // launches a new sign-in -- sign-in only ever begins from an explicit button
      // click (chrome-extension-design.md SS6, EXTENSION_INTEGRATION.md SS3).
      console.warn('[sidepanel] revoke-all call did not succeed cleanly', err);
      renderSignedIn(
        session,
        'Could not sign out everywhere — other devices may still be signed in. Please try again.',
      );
    }
  } finally {
    setBusy(false);
  }
}

signinGoogleButton?.addEventListener('click', () => {
  void handleSignIn('google');
});
signinMicrosoftButton?.addEventListener('click', () => {
  void handleSignIn('microsoft');
});
signoutButton?.addEventListener('click', () => {
  void handleSignOut();
});
revokeAllButton?.addEventListener('click', () => {
  void handleRevokeAll();
});

// Restore a previously-stored session on side panel open. This does not validate the
// token against the backend (no authenticated GET exists yet this session) -- it trusts
// local storage until a future session's first real authenticated call needs otherwise.
getStoredSession().then((session) => {
  if (session) {
    renderSignedIn(session);
  } else {
    renderSignedOut();
  }
});

// Re-render whenever fa_session changes in storage -- covers this panel's own actions
// (already handled by the calls above) and, critically, any OTHER open side panel's
// sign-in/sign-out/revoke-all, since each panel is a separate document that otherwise
// never observes another panel's actions.
onStoredSessionChanged((session) => {
  if (session) {
    renderSignedIn(session);
  } else {
    renderSignedOut();
  }
});
