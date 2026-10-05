import { launchAuthFlow, AuthFlowError } from '../../lib/authFlow';
import { abortableSleep } from '../../lib/askRunner';
import { ANSWER_PENDING_NOTE } from '../../lib/chatModel';
import { clearChatState } from '../../lib/conversationStorage';
import { ChatController } from './chat';
import {
  ask,
  confirmMapping,
  exchangeToken,
  logout,
  parseCsv,
  proposeMapping,
  revokeAllSessions,
  BackendApiError,
  type ConfirmMappingResponse,
  type UnparsedCell,
} from '../../lib/backendApi';
import {
  buildConfirmRequest,
  carryOver,
  CURRENCY_OPTIONS,
  newDraft,
  ROLE_OPTIONS,
  roleLabel,
  rolesFromProposal,
  SCALE_OPTIONS,
  validateDraft,
  type MappingDraft,
  type ProposalEntry,
  type Scale,
} from '../../lib/mappingModel';
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
  getActiveStatement,
  setActiveStatement,
  clearActiveStatement,
  type ActiveStatement,
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
const mappingPanel = document.querySelector<HTMLElement>('#mapping-panel');
const mappingCaption = document.querySelector<HTMLElement>('#mapping-caption');
const mappingNote = document.querySelector<HTMLElement>('#mapping-note');
const mappingTable = document.querySelector<HTMLTableElement>('#mapping-table');
const entityNameInput = document.querySelector<HTMLInputElement>('#entity-name');
const scaleSelect = document.querySelector<HTMLSelectElement>('#scale-select');
const currencySelect = document.querySelector<HTMLSelectElement>('#currency-select');
const mappingErrors = document.querySelector<HTMLElement>('#mapping-errors');
const ackPanel = document.querySelector<HTMLElement>('#ack-panel');
const ackText = document.querySelector<HTMLElement>('#ack-text');
const ackCells = document.querySelector<HTMLElement>('#ack-cells');
const confirmAnywayButton = document.querySelector<HTMLButtonElement>('#confirm-anyway');
const ackBackButton = document.querySelector<HTMLButtonElement>('#ack-back');
const mappingActions = document.querySelector<HTMLElement>('#mapping-actions');
const confirmMappingButton = document.querySelector<HTMLButtonElement>('#confirm-mapping');
const resetMappingButton = document.querySelector<HTMLButtonElement>('#reset-mapping');
const cancelMappingButton = document.querySelector<HTMLButtonElement>('#cancel-mapping');
const statementCard = document.querySelector<HTMLElement>('#statement-card');
const statementSummary = document.querySelector<HTMLElement>('#statement-summary');
const statementWarnings = document.querySelector<HTMLElement>('#statement-warnings');
const changeMappingButton = document.querySelector<HTMLButtonElement>('#change-mapping');
const mappingPendingNote = document.querySelector<HTMLElement>('#mapping-pending-note');

function required<T extends Element>(selector: string): T {
  const found = document.querySelector<T>(selector);
  if (!found) throw new Error(`side panel is missing ${selector}`);
  return found;
}

// The chat (Phase D session 5). Shown only while a confirmed statement is active.
const chat = new ChatController(
  {
    panel: required('#chat-panel'),
    statementLine: required('#chat-statement'),
    mismatch: required('#chat-mismatch'),
    mismatchText: required('#chat-mismatch-text'),
    newConversationButton: required('#chat-new'),
    log: required('#chat-log'),
    notice: required('#chat-notice'),
    waiting: required('#chat-waiting'),
    waitingText: required('#chat-waiting-text'),
    stopButton: required('#chat-stop'),
    checkButton: required('#chat-check'),
    discardButton: required('#chat-discard'),
    pendingNote: required('#chat-pending-note'),
    form: required('#chat-form'),
    input: required('#chat-input'),
    sendButton: required('#chat-send'),
  },
  {
    getSession: () => getStoredSession(),
    onPendingChange: (pending) => {
      // Confirming starts a new conversation, which would abandon a pending question that
      // still counts: Confirm is blocked until it's answered or discarded.
      answerPending = pending;
      if (mappingPendingNote) {
        mappingPendingNote.textContent = pending ? ANSWER_PENDING_NOTE : '';
        mappingPendingNote.hidden = !pending;
      }
      if (mapping) updateMappingValidation(mapping);
    },
    send: (sessionToken, body, signal) => ask(sessionToken, body, signal),
    onUnauthorized: () => handleUnauthorized(),
    onStatementNeedsReconfirm: async (message) => {
      // The statement can't be used for questions any more (deleted, or confirmed before
      // units existed): drop it, so the person reads the range and confirms it again.
      await clearActiveStatement();
      lastConfirmed = null;
      renderStatementCard(null);
      setStatus(message, true);
    },
    newRequestId: () => crypto.randomUUID(),
    now: () => Date.now(),
    sleep: abortableSleep,
  },
);

// The connected spreadsheet and the grid last previewed from it -- panel memory only.
let connectedFile: ConnectedFile | null = null;
let connectedEmail = '';
let previewGrid: NormalizedGrid | null = null;

/** Where a confirmed mapping came from: enough to re-send the same rows for Change mapping. */
interface MappingOrigin {
  grid: NormalizedGrid;
  file: Pick<ConnectedFile, 'name' | 'platform' | 'modifiedAt'>;
}

/** What "Change mapping" starts from: the last mapping confirmed in this panel. */
interface ConfirmedChoices extends MappingOrigin {
  roles: Record<string, string>;
  entityName: string;
  scale: Scale;
  currency: string | null;
}

/**
 * The mapping screen for one csv_context_id (Phase D session 4). Every response is checked
 * against the session that started it, so a reply for a context that's no longer on screen
 * is dropped rather than applied.
 */
interface MappingSession extends MappingOrigin {
  csvContextId: string;
  columns: string[];
  sampleRows: string[][];
  proposal: ProposalEntry[] | null;
  draft: MappingDraft;
  serverErrors: string[];
  acknowledgement: { fingerprint: string; cells: UnparsedCell[] } | null;
}

let mapping: MappingSession | null = null;
let lastConfirmed: ConfirmedChoices | null = null;
let busy = false;
let answerPending = false;

const SAMPLE_VALUES_SHOWN = 3;

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

/** A cell reference or A1 range, styled in mono. textContent only: sheet names come from user
 * files and can hold any characters, so this must never go through innerHTML. */
function refSpan(text: string): HTMLSpanElement {
  const span = document.createElement('span');
  span.className = 'ref';
  span.textContent = text;
  return span;
}

function headerCell(text: string, className?: string): HTMLTableCellElement {
  const th = document.createElement('th');
  th.textContent = text;
  if (className) th.className = className;
  return th;
}

/**
 * Exactly what will be sent, labelled with the sheet's own row numbers and column letters so
 * each value can be checked against its cell. Each cell's tooltip gives its address and what
 * the spreadsheet shows ("C4 — shown as 0.6145038168").
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
      const address = cellAddress(range.startRow + r, range.startCol + c);
      td.title = cell.display ? `${address} — shown as ${cell.display}` : address;
      tr.append(td);
    });
    return tr;
  });
  previewTable?.replaceChildren(letters, ...body);

  const dataRows = rows.length - 1;
  if (previewCaption) {
    previewCaption.replaceChildren(
      refSpan(rangeLabel(grid)),
      ` · ${dataRows} data row${dataRows === 1 ? '' : 's'} × ${width} column${width === 1 ? '' : 's'}` +
        (dataRows > PREVIEW_DATA_ROWS ? ` · showing the first ${PREVIEW_DATA_ROWS}` : ''),
    );
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

function rangeLabel(grid: NormalizedGrid): string {
  return `${quoteSheetName(grid.sheetName)}!${formatA1Range(grid.range)}`;
}

function option(value: string, label: string): HTMLOptionElement {
  const el = document.createElement('option');
  el.value = value;
  el.textContent = label;
  return el;
}

function listItems(items: string[]): HTMLLIElement[] {
  return items.map((text) => {
    const li = document.createElement('li');
    li.textContent = text;
    return li;
  });
}

/** The mapping table: one row per column, with its first values, a role menu and the
 * suggestion's reason. Rebuilt whenever a role changes. */
function renderMappingTable(m: MappingSession): void {
  const head = document.createElement('tr');
  head.append(headerCell('Column'), headerCell('Values'), headerCell('Is'), headerCell('Why suggested'));
  const rows = m.columns.map((column, index) => {
    const tr = document.createElement('tr');
    tr.dataset.column = column;
    const role = m.draft.roles[column] ?? 'unmapped';
    if (role === 'unmapped') tr.className = 'mapping-row--unused';

    const name = document.createElement('th');
    name.textContent = column;
    name.title = cellAddress(m.grid.range.startRow, m.grid.range.startCol + index);

    const values = document.createElement('td');
    values.className = 'mapping-values';
    values.textContent = m.sampleRows
      .slice(0, SAMPLE_VALUES_SHOWN)
      .map((row) => row[index] ?? '')
      .join(' · ');

    const roleCell = document.createElement('td');
    const select = document.createElement('select');
    select.className = 'role-select';
    select.setAttribute('aria-label', `What “${column}” is`);
    select.append(...ROLE_OPTIONS.map((o) => option(o.value, o.label)));
    select.value = role;
    select.disabled = busy;
    select.addEventListener('change', () => {
      if (mapping !== m) return;
      m.draft.roles[column] = select.value;
      draftChanged(m);
      renderMappingTable(m);
    });
    const conflict = document.createElement('p');
    conflict.className = 'mapping-conflict';
    roleCell.append(select, conflict);

    const why = document.createElement('td');
    why.className = 'mapping-rationale';
    why.textContent = m.draft.rationales[column] ?? '';

    tr.append(name, values, roleCell, why);
    return tr;
  });
  mappingTable?.replaceChildren(head, ...rows);
  updateMappingValidation(m);
}

/** Any edit invalidates an earlier acknowledgement and the server's last refusal. */
function draftChanged(m: MappingSession): void {
  m.acknowledgement = null;
  m.serverErrors = [];
  renderAcknowledgement(m);
  updateMappingValidation(m);
}

function updateMappingValidation(m: MappingSession): void {
  const result = validateDraft(m.draft);
  mappingErrors?.replaceChildren(...listItems([...m.serverErrors, ...result.formErrors]));
  mappingTable?.querySelectorAll<HTMLTableRowElement>('tr[data-column]').forEach((tr) => {
    const message = result.columnErrors[tr.dataset.column!] ?? '';
    const conflict = tr.querySelector<HTMLElement>('.mapping-conflict');
    if (conflict) conflict.textContent = message;
    tr.classList.toggle('mapping-row--conflict', message !== '');
  });
  if (confirmMappingButton) confirmMappingButton.disabled = busy || answerPending || !result.canConfirm;
  if (confirmAnywayButton) confirmAnywayButton.disabled = busy || answerPending;
  if (resetMappingButton) resetMappingButton.hidden = m.proposal === null;
}

function renderAcknowledgement(m: MappingSession): void {
  const ack = m.acknowledgement;
  if (!ack) {
    ackPanel?.setAttribute('hidden', '');
    mappingActions?.removeAttribute('hidden');
    ackCells?.replaceChildren();
    return;
  }
  const n = ack.cells.length;
  if (ackText) {
    ackText.textContent =
      `${n} cell${n === 1 ? '' : 's'} in mapped columns ${n === 1 ? "isn't a number" : "aren't numbers"}. ` +
      'If you confirm anyway, those periods will have no value for that item.';
  }
  ackCells?.replaceChildren(
    ...ack.cells.map((c) => {
      const li = document.createElement('li');
      li.append(
        c.cell ? refSpan(c.cell) : `Data row ${c.source_row + 1}`,
        ` — “${c.value}” (${roleLabel(c.role)}, ${c.period_end})`,
      );
      return li;
    }),
  );
  ackPanel?.removeAttribute('hidden');
  mappingActions?.setAttribute('hidden', '');
}

function showMappingPanel(m: MappingSession): void {
  if (mappingCaption) {
    mappingCaption.replaceChildren(
      `${m.file.name} · `,
      refSpan(rangeLabel(m.grid)),
      ` · ${m.grid.rows.length - 1} data rows. Choose what each column is. Nothing is used until you confirm.`,
    );
  }
  if (entityNameInput) entityNameInput.value = m.draft.entityName;
  scaleSelect?.replaceChildren(
    option('', 'Choose…'),
    ...SCALE_OPTIONS.map((o) => option(o.value, o.label)),
  );
  if (scaleSelect) scaleSelect.value = m.draft.scale ?? '';
  currencySelect?.replaceChildren(...CURRENCY_OPTIONS.map((o) => option(o.value ?? '', o.label)));
  if (currencySelect) currencySelect.value = m.draft.currency ?? '';
  renderAcknowledgement(m);
  renderMappingTable(m);
  readPanel?.setAttribute('hidden', '');
  mappingPanel?.removeAttribute('hidden');
}

function closeMappingPanel(): void {
  mapping = null;
  mappingPanel?.setAttribute('hidden', '');
  mappingTable?.replaceChildren();
  mappingErrors?.replaceChildren();
  ackCells?.replaceChildren();
  if (mappingNote) mappingNote.textContent = '';
}

/** Back to the read panel, with the preview that was sent still shown when there is one. */
function returnToReadPanel(): void {
  closeMappingPanel();
  if (connectedFile) {
    readPanel?.removeAttribute('hidden');
    if (previewGrid) renderPreview(previewGrid);
  }
}

function describeScale(scale: string): string {
  return SCALE_OPTIONS.find((o) => o.value === scale)?.label ?? scale;
}

function renderStatementCard(statement: ActiveStatement | null, details: string[] = []): void {
  void chat.show(statement); // chat needs a confirmed statement
  if (!statement) {
    statementCard?.setAttribute('hidden', '');
    statementWarnings?.replaceChildren();
    return;
  }
  if (statementSummary) {
    const cadence = statement.cadence ? `${statement.cadence} periods` : 'a single period';
    statementSummary.textContent =
      `${statement.entityName} — ${statement.label}. ${cadence[0]!.toUpperCase()}${cadence.slice(1)}; ` +
      `numbers in ${describeScale(statement.scale).toLowerCase()}; currency ` +
      `${statement.currency ?? 'not specified'}. Ready for questions.`;
  }
  statementWarnings?.replaceChildren(...listItems(details));
  if (changeMappingButton) changeMappingButton.hidden = lastConfirmed === null;
  statementCard?.removeAttribute('hidden');
}

async function refreshStatementCard(): Promise<void> {
  renderStatementCard(await getActiveStatement());
}

function renderSignedOut(errorMessage?: string): void {
  resetReadPanel();
  closeMappingPanel();
  lastConfirmed = null;
  chat.reset();
  renderStatementCard(null);
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
  // Restored from storage on every render, with no network call; the card's details from
  // the confirm response itself exist only in the panel that confirmed.
  if (statementCard?.hasAttribute('hidden')) void refreshStatementCard();
}

function setBusy(isBusy: boolean): void {
  busy = isBusy;
  for (const control of [
    entityNameInput,
    scaleSelect,
    currencySelect,
    resetMappingButton,
    cancelMappingButton,
    confirmAnywayButton,
    ackBackButton,
    changeMappingButton,
  ]) {
    if (control) control.disabled = isBusy;
  }
  mappingTable?.querySelectorAll<HTMLSelectElement>('select').forEach((s) => (s.disabled = isBusy));
  if (mapping) updateMappingValidation(mapping);
  else if (confirmMappingButton) confirmMappingButton.disabled = true;
  setReadBusy(isBusy);
}

function setReadBusy(busy: boolean): void {
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
    // A new sign-in may be a different person on the same browser profile: no data token,
    // data-account email (a login_hint) or active statement may carry over from whoever was
    // signed in before.
    await clearAllDataAccess();
    await clearActiveStatement();
    await clearChatState();
    lastConfirmed = null;
    closeMappingPanel();
    chat.reset();
    renderStatementCard(null);
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
  closeMappingPanel();
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

const SHAPE_BUG_MESSAGE = 'The data was sent in a shape the server rejected. This is a bug; please report it.';
const EXPIRED_MESSAGE = 'This selection expired or is no longer available. Send it again.';

/**
 * Send to analysis: POST /v1/csv/parse with the grid and its sheet source, then straight into
 * the mapping screen. A fresh send asks for a suggested mapping; "Change mapping" (`previous`)
 * re-sends the same rows and starts from the choices confirmed before, with no model call.
 */
async function sendForMapping(origin: MappingOrigin, previous: ConfirmedChoices | null): Promise<void> {
  const session = await getStoredSession();
  if (!session) {
    renderSignedOut();
    return;
  }
  const request = buildParseRequest(origin.grid, origin.file);
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
    const m: MappingSession = {
      ...origin,
      csvContextId: response.csv_context_id,
      columns: response.columns,
      sampleRows: response.sample_rows,
      proposal: null,
      draft: newDraft(response.columns, null, origin.file.name),
      serverErrors: [],
      acknowledgement: null,
    };
    mapping = m;
    if (mappingNote) mappingNote.textContent = '';
    if (previous) {
      m.draft = carryOver(m.draft, previous);
      clearStatus();
      showMappingPanel(m);
      return;
    }
    showMappingPanel(m);
    setStatus('Suggesting a mapping…');
    await suggestMapping(session.sessionToken, m);
  } catch (err) {
    if (err instanceof BackendApiError && err.status === 401) {
      await handleUnauthorized();
      return;
    }
    console.error('[sidepanel] send data failed', err);
    setStatus(
      err instanceof BackendApiError && err.status === 422
        ? SHAPE_BUG_MESSAGE
        : 'Could not send the data — please try again.',
      true,
    );
  } finally {
    setBusy(false);
  }
}

/**
 * POST /v1/csv/{id}/propose-mapping into `m`. Any failure other than a 401 or an expired
 * context leaves every column "Not used" for the person to map by hand: a suggestion is a
 * convenience, never required to confirm.
 */
async function suggestMapping(sessionToken: string, m: MappingSession): Promise<void> {
  try {
    const response = await proposeMapping(sessionToken, m.csvContextId);
    if (mapping !== m) return;
    m.proposal = response.proposal;
    m.draft = { ...m.draft, ...newDraft(m.columns, response.proposal, m.draft.entityName) };
    if (mappingNote) mappingNote.textContent = response.note ?? '';
    clearStatus();
  } catch (err) {
    if (mapping !== m) return;
    if (err instanceof BackendApiError && err.status === 401) {
      await handleUnauthorized();
      return;
    }
    if (err instanceof BackendApiError && err.status === 404) {
      returnToReadPanel();
      setStatus(EXPIRED_MESSAGE, true);
      return;
    }
    console.error('[sidepanel] propose mapping failed', err);
    if (mappingNote) mappingNote.textContent = suggestionFailureNote(err);
    clearStatus();
  }
  renderMappingTable(m);
}

function suggestionFailureNote(err: unknown): string {
  if (err instanceof BackendApiError && err.status === 429) {
    const resetsAt =
      err.detail && typeof err.detail === 'object' && 'resets_at' in err.detail
        ? (err.detail as { resets_at: string | null }).resets_at
        : null;
    return (
      "You've used today's suggested mappings" +
      (resetsAt ? ` (more from ${new Date(resetsAt).toLocaleString()})` : '') +
      '. Choose what each column is yourself.'
    );
  }
  if (err instanceof BackendApiError && err.status === 409) {
    return 'This selection was already confirmed. This is a bug; please report it.';
  }
  return "Couldn't suggest a mapping. Choose what each column is yourself.";
}

async function handleSendData(): Promise<void> {
  const file = connectedFile;
  const grid = previewGrid;
  if (!file || !grid) return;
  await sendForMapping({ grid, file: { name: file.name, platform: file.platform, modifiedAt: file.modifiedAt } }, null);
}

async function handleChangeMapping(): Promise<void> {
  if (!lastConfirmed) return;
  await sendForMapping(lastConfirmed, lastConfirmed);
}

/** Confirm, or (`acknowledge`) Confirm anyway for the unparsed cells just shown. */
async function handleConfirmMapping(acknowledge: boolean): Promise<void> {
  const m = mapping;
  if (!m || !validateDraft(m.draft).canConfirm || answerPending) return;
  const session = await getStoredSession();
  if (!session) {
    renderSignedOut();
    return;
  }
  const body = buildConfirmRequest(
    m.draft,
    acknowledge && m.acknowledgement ? { fingerprint: m.acknowledgement.fingerprint } : null,
  );
  setBusy(true);
  setStatus('Confirming…');
  try {
    const response = await confirmMapping(session.sessionToken, m.csvContextId, body);
    if (mapping !== m) return;
    if (response.confirmed) {
      await statementConfirmed(m, response);
    } else if (response.requires_acknowledgement && response.ack_fingerprint) {
      m.serverErrors = [];
      m.acknowledgement = { fingerprint: response.ack_fingerprint, cells: response.unparsed_cells };
      renderAcknowledgement(m);
      updateMappingValidation(m);
      clearStatus();
    } else {
      // The backend's own refusal (period spacing, duplicate dates, ...), shown as it says it.
      m.acknowledgement = null;
      m.serverErrors = response.errors.length ? response.errors : ['The mapping was not accepted.'];
      renderAcknowledgement(m);
      updateMappingValidation(m);
      setStatus("The mapping wasn't accepted — see the problems listed.", true);
    }
  } catch (err) {
    if (mapping !== m) return;
    if (err instanceof BackendApiError && err.status === 401) {
      await handleUnauthorized();
      return;
    }
    if (err instanceof BackendApiError && err.status === 404) {
      returnToReadPanel();
      setStatus(EXPIRED_MESSAGE, true);
      return;
    }
    console.error('[sidepanel] confirm mapping failed', err);
    if (err instanceof BackendApiError && err.status === 409) {
      setStatus('This selection was already confirmed. This is a bug; please report it.', true);
    } else if (err instanceof BackendApiError && err.status === 422) {
      setStatus(SHAPE_BUG_MESSAGE, true);
    } else {
      setStatus('Could not confirm — please try again.', true);
    }
  } finally {
    setBusy(false);
  }
}

async function statementConfirmed(m: MappingSession, response: ConfirmMappingResponse): Promise<void> {
  const scale = m.draft.scale!;
  const statement: ActiveStatement = {
    csvContextId: m.csvContextId,
    entityName: m.draft.entityName.trim(),
    label: `${m.file.name} · ${rangeLabel(m.grid)}`,
    confirmedAt: new Date().toISOString(),
    cadence: response.cadence,
    scale,
    currency: m.draft.currency,
  };
  await setActiveStatement(statement);
  // A newly confirmed statement starts a new conversation: a conversation is bound to one
  // statement (EXTENSION_INTEGRATION.md SS6 /v1/ask, amended session 5).
  await chat.startNewConversation(statement);
  lastConfirmed = {
    grid: m.grid,
    file: m.file,
    roles: { ...m.draft.roles },
    entityName: statement.entityName,
    scale,
    currency: m.draft.currency,
  };
  closeMappingPanel();
  readPanel?.setAttribute('hidden', '');

  const details = [...response.warnings];
  if (response.unparsed_cells.length) {
    details.push(
      `No value for: ${response.unparsed_cells
        .map((c) => `${c.cell ?? `data row ${c.source_row + 1}`} (“${c.value}”)`)
        .join(', ')}.`,
    );
  }
  renderStatementCard(statement, details);
  setStatus('Statement confirmed.');
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
confirmMappingButton?.addEventListener('click', () => {
  void handleConfirmMapping(false);
});
confirmAnywayButton?.addEventListener('click', () => {
  void handleConfirmMapping(true);
});
ackBackButton?.addEventListener('click', () => {
  if (mapping) draftChanged(mapping);
});
resetMappingButton?.addEventListener('click', () => {
  // Back to the stored suggestion -- no network call.
  const m = mapping;
  if (!m || !m.proposal) return;
  m.draft.roles = rolesFromProposal(m.columns, m.proposal);
  draftChanged(m);
  renderMappingTable(m);
});
cancelMappingButton?.addEventListener('click', () => {
  clearStatus();
  returnToReadPanel();
});
changeMappingButton?.addEventListener('click', () => {
  void handleChangeMapping();
});
entityNameInput?.addEventListener('input', () => {
  if (!mapping) return;
  mapping.draft.entityName = entityNameInput.value;
  draftChanged(mapping);
});
scaleSelect?.addEventListener('change', () => {
  if (!mapping) return;
  mapping.draft.scale = scaleSelect.value === '' ? null : (scaleSelect.value as Scale);
  draftChanged(mapping);
});
currencySelect?.addEventListener('change', () => {
  if (!mapping) return;
  mapping.draft.currency = currencySelect.value === '' ? null : currencySelect.value;
  draftChanged(mapping);
});
sheetSelect?.addEventListener('change', () => {
  // A range belongs to the sheet it was entered or linked for, so any sheet switch clears it.
  if (rangeInput) rangeInput.value = '';
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
