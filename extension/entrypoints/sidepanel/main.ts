import { launchAuthFlow, AuthFlowError } from '../../lib/authFlow';
import {
  exchangeToken,
  logout,
  parseCsv,
  revokeAllSessions,
  BackendApiError,
  type CsvParseResponse,
} from '../../lib/backendApi';
import {
  bodySizeError,
  buildParseRequest,
  cellAddress,
  columnLetter,
  formatA1Range,
  parseA1Range,
  precheckGrid,
  quoteSheetName,
  type NormalizedGrid,
} from '../../lib/cellGrid';
import { ExcelFileError } from '../../lib/excelReader';
import {
  connectExcelFile,
  connectGoogleSheet,
  type ConnectedFile,
} from '../../lib/spreadsheetSource';
import {
  getStoredSession,
  setStoredSession,
  clearStoredSession,
  onStoredSessionChanged,
  type StoredSession,
} from '../../lib/sessionStorage';
import { BACKEND_BASE_URL, type AuthProvider } from '../../lib/authConfig';
import { clearAllDataAccess, forgetDataGrant } from '../../lib/dataAccessStorage';
import {
  getDataToken,
  InteractiveAfterSilentFailedError,
  ScopeNotGrantedError,
} from '../../lib/dataToken';
import {
  classifyTab,
  ProviderApiError,
  UNSUPPORTED_EXCEL_URL_MESSAGE,
} from '../../lib/connectData';

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
const connectDataButton = document.querySelector<HTMLButtonElement>('#connect-data');
const dataConnection = document.querySelector<HTMLElement>('#data-connection');
const dataNote = document.querySelector<HTMLElement>('#data-note');
const readPanel = document.querySelector<HTMLElement>('#read-panel');
const fileAsOf = document.querySelector<HTMLElement>('#file-as-of');
const sheetSelect = document.querySelector<HTMLSelectElement>('#sheet-select');
const rangeInput = document.querySelector<HTMLInputElement>('#range-input');
const readRangeButton = document.querySelector<HTMLButtonElement>('#read-range');
const readNotices = document.querySelector<HTMLElement>('#read-notices');
const previewCaption = document.querySelector<HTMLElement>('#preview-caption');
const previewTable = document.querySelector<HTMLTableElement>('#preview-table');
const sendDataButton = document.querySelector<HTMLButtonElement>('#send-data');
const parseSummary = document.querySelector<HTMLElement>('#parse-summary');
const parseSummaryText = document.querySelector<HTMLElement>('#parse-summary-text');
const parseSampleTable = document.querySelector<HTMLTableElement>('#parse-sample-table');

// The connected spreadsheet and the grid last previewed from it -- panel memory only.
let connectedFile: ConnectedFile | null = null;
let connectedEmail = '';
let previewGrid: NormalizedGrid | null = null;

const PREVIEW_DATA_ROWS = 10;

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

function setDataConnection(text: string, note = ''): void {
  if (dataConnection) dataConnection.textContent = text;
  if (dataNote) dataNote.textContent = note;
}

function clearPreview(): void {
  previewGrid = null;
  readNotices?.replaceChildren();
  if (previewCaption) previewCaption.textContent = '';
  previewTable?.replaceChildren();
  sendDataButton?.setAttribute('hidden', '');
  parseSummary?.setAttribute('hidden', '');
  parseSampleTable?.replaceChildren();
}

function resetReadPanel(): void {
  connectedFile = null;
  connectedEmail = '';
  clearPreview();
  sheetSelect?.replaceChildren();
  if (rangeInput) rangeInput.value = '';
  if (fileAsOf) fileAsOf.textContent = '';
  readPanel?.setAttribute('hidden', '');
}

function showConnectedFile(file: ConnectedFile): void {
  clearPreview();
  sheetSelect?.replaceChildren(
    ...file.sheets.map((title) => {
      const option = document.createElement('option');
      option.value = title;
      option.textContent = title;
      return option;
    }),
  );
  if (sheetSelect) sheetSelect.value = file.defaultSheet;
  if (rangeInput) rangeInput.value = file.defaultRange ?? '';
  if (fileAsOf) {
    fileAsOf.textContent = file.modifiedAt
      ? `Data as of last save: ${new Date(file.modifiedAt).toLocaleString()}. Edits from the ` +
        'last minute or two may not appear yet.'
      : '';
  }
  readPanel?.removeAttribute('hidden');
}

function headerCell(text: string, className?: string): HTMLTableCellElement {
  const th = document.createElement('th');
  th.textContent = text;
  if (className) th.className = className;
  return th;
}

/**
 * Exactly what will be sent, labelled with the sheet's own row numbers and column letters so
 * each value can be checked against its cell. The spreadsheet's displayed text, when it
 * differs, is in the cell's tooltip.
 */
function renderPreview(grid: NormalizedGrid): void {
  previewGrid = grid;
  const { range, rows } = grid;
  const width = rows[0]!.length;
  const shown = rows.slice(0, PREVIEW_DATA_ROWS + 1);

  const letters = document.createElement('tr');
  letters.append(headerCell('', 'address'));
  for (let c = 0; c < width; c++) letters.append(headerCell(columnLetter(range.startCol + c), 'address'));
  const body = shown.map((row, r) => {
    const tr = document.createElement('tr');
    tr.append(headerCell(String(range.startRow + r), 'address'));
    row.forEach((cell, c) => {
      const td = r === 0 ? headerCell(cell.value) : document.createElement('td');
      td.textContent = cell.value;
      td.title =
        cell.display && cell.display !== cell.value
          ? `${cellAddress(range.startRow + r, range.startCol + c)} shows "${cell.display}"`
          : cellAddress(range.startRow + r, range.startCol + c);
      tr.append(td);
    });
    return tr;
  });
  previewTable?.replaceChildren(letters, ...body);

  const dataRows = rows.length - 1;
  if (previewCaption) {
    previewCaption.textContent =
      `${quoteSheetName(grid.sheetName)}!${formatA1Range(range)} · ${dataRows} data ` +
      `row${dataRows === 1 ? '' : 's'} × ${width} column${width === 1 ? '' : 's'}` +
      (dataRows > PREVIEW_DATA_ROWS ? ` · showing the first ${PREVIEW_DATA_ROWS}` : '');
  }
  readNotices?.replaceChildren(
    ...grid.notices.map((notice) => {
      const li = document.createElement('li');
      li.textContent = notice.message;
      return li;
    }),
  );

  const problem = precheckGrid(grid);
  if (problem) {
    setStatus(problem, true);
    sendDataButton?.setAttribute('hidden', '');
  } else {
    clearStatus();
    sendDataButton?.removeAttribute('hidden');
  }
}

function renderParseSummary(response: CsvParseResponse, grid: NormalizedGrid): void {
  const dataRows = grid.rows.length - 1;
  if (parseSummaryText) {
    parseSummaryText.textContent =
      `Sent ${quoteSheetName(grid.sheetName)}!${formatA1Range(grid.range)}: ${dataRows} data ` +
      `rows × ${response.columns.length} columns. Mapping columns to financial concepts is the ` +
      `next step (coming soon). Reference ${response.csv_context_id}, kept for one hour.`;
  }
  const head = document.createElement('tr');
  head.append(...response.columns.map((column) => headerCell(column)));
  const body = response.sample_rows.map((row) => {
    const tr = document.createElement('tr');
    tr.append(
      ...row.map((value) => {
        const td = document.createElement('td');
        td.textContent = value;
        return td;
      }),
    );
    return tr;
  });
  parseSampleTable?.replaceChildren(head, ...body);
  sendDataButton?.setAttribute('hidden', '');
  parseSummary?.removeAttribute('hidden');
}

function renderSignedOut(errorMessage?: string): void {
  resetReadPanel();
  setDataConnection('');
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
  if (connectDataButton) connectDataButton.disabled = busy;
  if (readRangeButton) readRangeButton.disabled = busy;
  if (sendDataButton) sendDataButton.disabled = busy;
  if (sheetSelect) sheetSelect.disabled = busy;
  if (rangeInput) rangeInput.disabled = busy;
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
    // A new sign-in may be a different person on the same browser profile: no data token or
    // data-account email (a login_hint) may carry over from whoever was signed in before.
    await clearAllDataAccess();
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

/**
 * A backend 401 on an authenticated call (EXTENSION_INTEGRATION.md SS3): discard the session
 * -- which also clears every data token and data-account email -- and show the signed-out UI.
 * Never starts a new sign-in or data grant; that only ever begins from a click.
 */
async function handleUnauthorized(): Promise<void> {
  await clearStoredSession();
  renderSignedOut('Your session ended — please sign in again.');
}

function isWritePermissionScope(scope: string): boolean {
  return scope.split(' ').some((s) => s === 'Files.ReadWrite' || s.endsWith('/Files.ReadWrite'));
}

function connectErrorMessage(err: unknown): string {
  if (
    err instanceof ScopeNotGrantedError ||
    err instanceof InteractiveAfterSilentFailedError ||
    err instanceof AuthFlowError
  ) {
    return err.message;
  }
  if (err instanceof BackendApiError) {
    if (err.status === 400) return "Google didn't complete the authorization — try again.";
    if (err.status === 500) return 'Google access is currently unavailable.';
  }
  return 'Something went wrong — please try again.';
}

/**
 * Shared by Connect data and Preview: a failed data-token or provider read. Never retried
 * automatically.
 */
async function handleDataError(
  err: unknown,
  provider: AuthProvider | null,
  dataEmail: string,
): Promise<void> {
  if (err instanceof BackendApiError && err.status === 401) {
    await handleUnauthorized();
    return;
  }
  console.error('[sidepanel] data read failed', err);
  if (err instanceof ProviderApiError && provider) {
    // The data token itself didn't work for this file: forget it and its account, so the
    // next click goes through the account chooser.
    await forgetDataGrant(provider);
    resetReadPanel();
    if (err.status === 401) {
      setStatus('Access expired — click Connect data again.', true);
    } else if (err.status === 403 || err.status === 404) {
      setStatus(
        `${dataEmail} can't open this file — click Connect data to choose another account.`,
        true,
      );
    } else {
      setStatus('Could not read this file — please try again.', true);
    }
  } else if (err instanceof ExcelFileError) {
    setStatus(err.message, true);
  } else {
    setStatus(connectErrorMessage(err), true);
  }
}

/**
 * Connect data: a data token for whichever platform the active tab shows (Google Sheets or
 * Excel Online), then the file's sheet list -- for Excel, by downloading the workbook once.
 * `click` is passed through to getDataToken, whose click guard is what keeps every
 * launchWebAuthFlow inside a direct user click.
 */
async function handleConnectData(click: MouseEvent): Promise<void> {
  const session = await getStoredSession();
  if (!session) {
    renderSignedOut();
    return;
  }
  setBusy(true);
  setStatus('Connecting…');
  setDataConnection('');
  resetReadPanel();
  let provider: AuthProvider | null = null;
  let dataEmail = '';
  try {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    const target = classifyTab(tab?.url);
    if (!target) {
      setStatus('Open a Google Sheet or an Excel file first.', true);
      return;
    }
    if (target.kind === 'unsupported-excel-url') {
      // Checked before any token is requested: there's nothing this click could read.
      setStatus(UNSUPPORTED_EXCEL_URL_MESSAGE, true);
      return;
    }
    provider = target.kind;
    const token = await getDataToken(provider, { sessionToken: session.sessionToken, click });
    dataEmail = token.email;
    const file =
      target.kind === 'google'
        ? await connectGoogleSheet(token.accessToken, target.spreadsheetId, tab!.url!)
        : await connectExcelFile(token.accessToken, target);
    connectedFile = file;
    connectedEmail = token.email;
    clearStatus();
    setDataConnection(
      `Connected: ${file.name} via ${token.email}`,
      provider === 'microsoft' && isWritePermissionScope(token.scope)
        ? 'Your Microsoft account still grants this app an older write permission it no longer ' +
            'uses — you can remove it at account.live.com/consent/Manage.'
        : '',
    );
    showConnectedFile(file);
  } catch (err) {
    await handleDataError(err, provider, dataEmail);
  } finally {
    setBusy(false);
  }
}

/** Preview: reads the chosen sheet and range and shows exactly what would be sent. */
async function handleReadRange(click: MouseEvent): Promise<void> {
  const file = connectedFile;
  if (!file || !sheetSelect || !rangeInput) return;
  const session = await getStoredSession();
  if (!session) {
    renderSignedOut();
    return;
  }
  const rangeText = rangeInput.value.trim();
  const range = rangeText === '' ? null : parseA1Range(rangeText);
  if (rangeText !== '' && !range) {
    setStatus('Enter a range like A3:G7, or leave it empty for the whole sheet.', true);
    return;
  }
  setBusy(true);
  setStatus('Reading…');
  clearPreview();
  try {
    // Google reads go to the live sheet and need a data token (normally the cached one);
    // Excel re-parses the copy downloaded at Connect data.
    const accessToken =
      file.provider === 'google'
        ? (await getDataToken('google', { sessionToken: session.sessionToken, click })).accessToken
        : null;
    const grid = await file.read(sheetSelect.value, range, accessToken);
    if (connectedFile !== file) return; // reconnected or signed out meanwhile
    if (!grid) {
      setStatus('That range is empty.', true);
      return;
    }
    renderPreview(grid);
  } catch (err) {
    await handleDataError(err, file.provider, connectedEmail);
  } finally {
    setBusy(false);
  }
}

/** Send to analysis: POST /v1/csv/parse with the previewed grid and its sheet source. */
async function handleSendData(): Promise<void> {
  const file = connectedFile;
  const grid = previewGrid;
  if (!file || !grid) return;
  const session = await getStoredSession();
  if (!session) {
    renderSignedOut();
    return;
  }
  const request = buildParseRequest(grid, {
    name: file.name,
    platform: file.platform,
    modifiedAt: file.modifiedAt,
  });
  const sizeError = bodySizeError(request);
  if (sizeError) {
    setStatus(sizeError, true);
    return;
  }
  setBusy(true);
  setStatus('Sending…');
  try {
    const response = await parseCsv(session.sessionToken, request);
    if (response.parse_error !== null || !response.csv_context_id) {
      // The backend's own refusal, shown as it says it.
      setStatus(response.parse_error ?? 'The data could not be read.', true);
      return;
    }
    clearStatus();
    renderParseSummary(response, grid);
  } catch (err) {
    if (err instanceof BackendApiError && err.status === 401) {
      await handleUnauthorized();
      return;
    }
    console.error('[sidepanel] send data failed', err);
    setStatus(
      err instanceof BackendApiError && err.status === 422
        ? 'The data was sent in a shape the server rejected. This is a bug; please report it.'
        : 'Could not send the data — please try again.',
      true,
    );
  } finally {
    setBusy(false);
  }
}

connectDataButton?.addEventListener('click', (event) => {
  void handleConnectData(event);
});
readRangeButton?.addEventListener('click', (event) => {
  void handleReadRange(event);
});
sendDataButton?.addEventListener('click', () => {
  void handleSendData();
});
sheetSelect?.addEventListener('change', () => {
  // A range= link only applies to the sheet it was copied from.
  if (rangeInput && connectedFile) {
    rangeInput.value =
      sheetSelect.value === connectedFile.defaultSheet ? (connectedFile.defaultRange ?? '') : '';
  }
  clearPreview();
});
rangeInput?.addEventListener('input', () => {
  clearPreview();
});
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
