// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BackendApiError } from '../../lib/backendApi';
import { fakeBrowser } from 'wxt/testing/fake-browser';
import { AuthFlowError } from '../../lib/authFlow';
import { ProviderApiError } from '../../lib/connectData';
import { InteractiveAfterSilentFailedError, ScopeNotGrantedError } from '../../lib/dataToken';
import type { StoredSession } from '../../lib/sessionStorage';
import {
  normalizeGrid,
  numberCell,
  textCell,
  type Cell,
  type NormalizedGrid,
} from '../../lib/cellGrid';
import { ExcelFileError } from '../../lib/excelReader';
import type { ConnectedFile } from '../../lib/spreadsheetSource';

/**
 * These tests check the `hidden` attribute main.ts's rendering logic toggles, and which
 * mocked calls fire -- not the resulting visual/computed `display`. jsdom's
 * `getComputedStyle` does not reliably implement the full CSS cascade (in particular an
 * attribute selector like `[hidden]` combined with `!important` overriding an author
 * stylesheet rule), so asserting on computed style here would not genuinely verify
 * style.css's fix -- it would just re-assert whatever this file assumes the cascade
 * does. That fix needs manual/live verification in a real browser.
 */

const SIDEPANEL_BODY = `
  <div id="app">
    <h1>Financial Analysis Agent</h1>
    <div id="status-message" role="status" aria-live="polite"></div>
    <div id="signed-out-view">
      <p>Sign in to connect your spreadsheet.</p>
      <button id="signin-google" type="button">Sign in with Google</button>
      <button id="signin-microsoft" type="button">Sign in with Microsoft</button>
    </div>
    <div id="signed-in-view" hidden>
      <p id="account-info"></p>
      <button id="connect-data" type="button">Connect data</button>
      <p id="data-connection"></p>
      <p id="data-note"></p>
      <section id="read-panel" hidden>
        <p id="file-as-of"></p>
        <select id="sheet-select"></select>
        <input id="range-input" type="text" />
        <button id="read-range" type="button">Preview</button>
        <ul id="read-notices"></ul>
        <p id="preview-caption"></p>
        <table id="preview-table"></table>
        <button id="send-data" type="button" hidden>Send to analysis</button>
        <div id="parse-summary" hidden>
          <p id="parse-summary-text"></p>
          <table id="parse-sample-table"></table>
        </div>
      </section>
      <button id="signout" type="button">Sign out</button>
      <button id="revoke-all" type="button">Sign out everywhere</button>
    </div>
  </div>
`;

const {
  getStoredSessionMock,
  setStoredSessionMock,
  clearStoredSessionMock,
  onStoredSessionChangedMock,
  launchAuthFlowMock,
  exchangeTokenMock,
  logoutMock,
  revokeAllSessionsMock,
  clearAllDataAccessMock,
  forgetDataGrantMock,
  getDataTokenMock,
  connectGoogleSheetMock,
  connectExcelFileMock,
  parseCsvMock,
} = vi.hoisted(() => ({
  clearAllDataAccessMock: vi.fn(),
  forgetDataGrantMock: vi.fn(),
  getDataTokenMock: vi.fn(),
  connectGoogleSheetMock: vi.fn(),
  connectExcelFileMock: vi.fn(),
  parseCsvMock: vi.fn(),
  getStoredSessionMock: vi.fn(),
  setStoredSessionMock: vi.fn(),
  clearStoredSessionMock: vi.fn(),
  onStoredSessionChangedMock: vi.fn(),
  launchAuthFlowMock: vi.fn(),
  exchangeTokenMock: vi.fn(),
  logoutMock: vi.fn(),
  revokeAllSessionsMock: vi.fn(),
}));

// Mocking the whole sessionStorage module (rather than a real/fake browser.storage)
// keeps these tests deterministic and focused on main.ts's own control flow.
vi.mock('../../lib/sessionStorage', () => ({
  getStoredSession: getStoredSessionMock,
  setStoredSession: setStoredSessionMock,
  clearStoredSession: clearStoredSessionMock,
  onStoredSessionChanged: onStoredSessionChangedMock,
}));

// Keep the real AuthFlowError class (main.ts does `instanceof AuthFlowError`) while
// mocking only launchAuthFlow itself.
vi.mock('../../lib/authFlow', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/authFlow')>();
  return { ...actual, launchAuthFlow: launchAuthFlowMock };
});

// Same pattern for BackendApiError (main.ts does `instanceof BackendApiError`).
vi.mock('../../lib/backendApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/backendApi')>();
  return {
    ...actual,
    exchangeToken: exchangeTokenMock,
    logout: logoutMock,
    revokeAllSessions: revokeAllSessionsMock,
    parseCsv: parseCsvMock,
  };
});

vi.mock('../../lib/dataAccessStorage', () => ({
  clearAllDataAccess: clearAllDataAccessMock,
  forgetDataGrant: forgetDataGrantMock,
}));

// Keep the real error classes (main.ts does instanceof checks on them).
vi.mock('../../lib/dataToken', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/dataToken')>();
  return { ...actual, getDataToken: getDataTokenMock };
});

// classifyTab, ProviderApiError and the readers stay real; only connecting to a file (the
// network calls behind it) is mocked. Each test hands back a fake ConnectedFile.
vi.mock('../../lib/spreadsheetSource', () => ({
  connectGoogleSheet: connectGoogleSheetMock,
  connectExcelFile: connectExcelFileMock,
}));

// Pass-through mocks, so the error classes keep one identity: loadSidepanel's
// vi.resetModules() re-imports unmocked modules for main.ts, which would make main.ts's
// `instanceof ProviderApiError`/`ExcelFileError` miss the classes this file throws.
vi.mock('../../lib/connectData', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/connectData')>()),
}));
vi.mock('../../lib/excelReader', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/excelReader')>()),
}));

// A macrotask, not a fixed count of microtask ticks: every pending promise chain in a
// handler settles before it runs, however many awaits that handler has.
async function flushAsync(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

async function loadSidepanel(): Promise<void> {
  document.body.innerHTML = SIDEPANEL_BODY;
  vi.resetModules();
  await import('./main');
  await flushAsync();
}

function isHidden(selector: string): boolean {
  return document.querySelector(selector)?.hasAttribute('hidden') ?? true;
}

function statusText(): string | null {
  return document.querySelector('#status-message')?.textContent ?? null;
}

const existingSession: StoredSession = {
  sessionToken: 'old-token',
  accountId: 'account-old',
  expiresAt: '2026-01-01T00:00:00Z',
  provider: 'google',
};

beforeEach(() => {
  getStoredSessionMock.mockReset().mockResolvedValue(null);
  setStoredSessionMock.mockReset().mockResolvedValue(undefined);
  clearStoredSessionMock.mockReset().mockResolvedValue(undefined);
  onStoredSessionChangedMock.mockReset();
  launchAuthFlowMock.mockReset();
  exchangeTokenMock.mockReset();
  logoutMock.mockReset().mockResolvedValue({ revoked: true });
  revokeAllSessionsMock.mockReset().mockResolvedValue({ revoked: true });
  clearAllDataAccessMock.mockReset().mockResolvedValue(undefined);
  forgetDataGrantMock.mockReset().mockResolvedValue(undefined);
  getDataTokenMock.mockReset();
  connectGoogleSheetMock.mockReset();
  connectExcelFileMock.mockReset();
  parseCsvMock.mockReset();
});

afterEach(() => {
  document.body.innerHTML = '';
});

describe('sidepanel initial paint (hidden-attribute state only -- see file header)', () => {
  it('signed-out state: exactly #signed-out-view lacks the hidden attribute', async () => {
    getStoredSessionMock.mockResolvedValue(null);
    await loadSidepanel();

    expect(isHidden('#signed-out-view')).toBe(false);
    expect(isHidden('#signed-in-view')).toBe(true);
  });

  it('signed-in state: exactly #signed-in-view lacks the hidden attribute', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    await loadSidepanel();

    expect(isHidden('#signed-out-view')).toBe(true);
    expect(isHidden('#signed-in-view')).toBe(false);
  });
});

describe('friendlyMessage 422 disambiguation (via the real sign-in flow)', () => {
  it('shows the Microsoft-specific message for the "no usable email" 422', async () => {
    launchAuthFlowMock.mockResolvedValue({ provider: 'microsoft', oauthToken: 'token' });
    exchangeTokenMock.mockRejectedValue(
      new BackendApiError(
        '/v1/auth/exchange failed (422).',
        422,
        'No usable email address is available for this account.',
      ),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-microsoft')!.click();
    await flushAsync();

    expect(statusText()).toBe(
      "This Microsoft account doesn't have a usable email address for sign-in.",
    );
  });

  it('shows a generic message for a "wrong fields for provider" 422, not the email message', async () => {
    launchAuthFlowMock.mockResolvedValue({
      provider: 'google',
      code: 'c',
      codeVerifier: 'v',
      redirectUri: 'r',
    });
    exchangeTokenMock.mockRejectedValue(
      new BackendApiError('/v1/auth/exchange failed (422).', 422, 'Microsoft sign-in requires oauth_token.'),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(statusText()).toBe('Sign-in failed — please try again.');
    expect(statusText()).not.toContain('usable email address');
  });
});

describe('bug 1: revoke-all never starts a new sign-in', () => {
  it('revoke-all never calls launchAuthFlow, and ends signed-out', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    revokeAllSessionsMock.mockResolvedValue({ revoked: true });
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#revoke-all')!.click();
    await flushAsync();

    expect(launchAuthFlowMock).not.toHaveBeenCalled();
    expect(clearStoredSessionMock).toHaveBeenCalled();
    expect(isHidden('#signed-out-view')).toBe(false);
    expect(isHidden('#signed-in-view')).toBe(true);
  });

  it('a 401 from revoke-all clears the session and shows signed-out', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    revokeAllSessionsMock.mockRejectedValue(
      new BackendApiError('failed', 401, 'Invalid or expired session token.'),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#revoke-all')!.click();
    await flushAsync();

    expect(launchAuthFlowMock).not.toHaveBeenCalled();
    expect(clearStoredSessionMock).toHaveBeenCalled();
    expect(isHidden('#signed-out-view')).toBe(false);
  });

  it('a non-401 failure from revoke-all keeps the session and shows an error, still signed in', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    revokeAllSessionsMock.mockRejectedValue(new Error('network error'));
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#revoke-all')!.click();
    await flushAsync();

    expect(launchAuthFlowMock).not.toHaveBeenCalled();
    expect(clearStoredSessionMock).not.toHaveBeenCalled();
    expect(isHidden('#signed-in-view')).toBe(false);
    expect(statusText()).toContain('other devices may still be signed in');
  });
});

describe('bug 3: old session revoked only after a successful new sign-in', () => {
  it('a successful sign-in with an existing session revokes the OLD token', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    launchAuthFlowMock.mockResolvedValue({
      provider: 'google',
      code: 'c',
      codeVerifier: 'v',
      redirectUri: 'r',
    });
    exchangeTokenMock.mockResolvedValue({
      session_token: 'new-token',
      account_id: 'account-new',
      expires_at: '2026-02-01T00:00:00Z',
    });
    logoutMock.mockResolvedValue({ revoked: true });
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(setStoredSessionMock).toHaveBeenCalledWith(
      expect.objectContaining({ sessionToken: 'new-token' }),
    );
    expect(logoutMock).toHaveBeenCalledWith('old-token');
    expect(isHidden('#signed-in-view')).toBe(false);
  });

  it('a cancelled sign-in never revokes the existing session', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    launchAuthFlowMock.mockRejectedValue(
      new AuthFlowError('Sign-in was cancelled or the authorization window could not be opened.'),
    );
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(logoutMock).not.toHaveBeenCalled();
    expect(setStoredSessionMock).not.toHaveBeenCalled();
  });

  it('a 401 from the best-effort old-token logout still leaves the NEW session stored and shown', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    launchAuthFlowMock.mockResolvedValue({
      provider: 'google',
      code: 'c',
      codeVerifier: 'v',
      redirectUri: 'r',
    });
    exchangeTokenMock.mockResolvedValue({
      session_token: 'new-token',
      account_id: 'account-new',
      expires_at: '2026-02-01T00:00:00Z',
    });
    logoutMock.mockRejectedValue(new BackendApiError('failed', 401, 'Invalid or expired session token.'));
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-google')!.click();
    await flushAsync();

    expect(setStoredSessionMock).toHaveBeenCalledWith(
      expect.objectContaining({ sessionToken: 'new-token' }),
    );
    expect(clearStoredSessionMock).not.toHaveBeenCalled();
    expect(isHidden('#signed-in-view')).toBe(false);
    expect(isHidden('#signed-out-view')).toBe(true);
  });
});

const SHEET_URL = 'https://docs.google.com/spreadsheets/d/1AbC/edit#gid=0';
const EXCEL_URL =
  'https://onedrive.live.com/personal/abc/_layouts/15/doc.aspx?sourcedoc={GUID}&action=edit';

function setActiveTabUrl(url: string | undefined): void {
  vi.spyOn(fakeBrowser.tabs, 'query').mockResolvedValue([{ url }] as never);
}

async function clickConnect(): Promise<void> {
  document.querySelector<HTMLButtonElement>('#connect-data')!.click();
  await flushAsync();
}

function connectionText(): string | null {
  return document.querySelector('#data-connection')?.textContent ?? null;
}

// A P&L like the FA Spike Test sheet's, already in sent form: header in row 3, ISO dates,
// underlying numbers. Built with the real cellGrid so notices and pre-checks are real too.
function pnlGrid(cells?: Cell[][]): NormalizedGrid {
  const rows = cells ?? [
    [textCell('Period'), textCell('Revenue'), textCell('Margin')],
    [
      { kind: 'date', value: '2025-03-31', display: '3/31/2025' },
      numberCell(1250000, '$1,250,000'),
      numberCell(0.6145038167938931, '61.5%'),
    ],
  ];
  return normalizeGrid('P&L', { originRow: 3, originCol: 0, cells: rows, hiddenRows: [], merges: [] })!;
}

function fakeFile(overrides: Partial<ConnectedFile> = {}): ConnectedFile {
  return {
    provider: 'google',
    platform: 'google_sheets',
    name: 'Q3 P&L',
    modifiedAt: null,
    sheets: ['P&L', 'Notes'],
    defaultSheet: 'P&L',
    defaultRange: 'A3:C4',
    read: vi.fn(async () => pnlGrid()),
    ...overrides,
  };
}

const el = <T extends HTMLElement>(selector: string) => document.querySelector<T>(selector)!;

async function click(selector: string): Promise<void> {
  el<HTMLButtonElement>(selector).click();
  await flushAsync();
}

describe('data grants only ever start from the Connect data click', () => {
  it('panel load, storage-change re-renders, sign-out and every revoke-all branch never request a data token', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    await loadSidepanel();
    const onChanged = onStoredSessionChangedMock.mock.calls[0]![0] as (
      s: StoredSession | null,
    ) => void;
    onChanged(existingSession);
    onChanged(null);
    await flushAsync();

    for (const outcome of [
      () => revokeAllSessionsMock.mockResolvedValue({ revoked: true }),
      () => revokeAllSessionsMock.mockRejectedValue(new BackendApiError('x', 401, 'x')),
      () => revokeAllSessionsMock.mockRejectedValue(new Error('network')),
    ]) {
      outcome();
      document.querySelector<HTMLButtonElement>('#revoke-all')!.click();
      await flushAsync();
    }
    document.querySelector<HTMLButtonElement>('#signout')!.click();
    await flushAsync();

    expect(getDataTokenMock).not.toHaveBeenCalled();
    expect(launchAuthFlowMock).not.toHaveBeenCalled();
  });

  it('a Connect data click on a Sheets tab requests a Google token once, passing that click event', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    setActiveTabUrl(SHEET_URL);
    getDataTokenMock.mockResolvedValue({
      accessToken: 'ya29.t',
      email: 'data@example.com',
      scope: 'openid email',
    });
    connectGoogleSheetMock.mockResolvedValue(fakeFile());
    await loadSidepanel();

    await clickConnect();

    expect(getDataTokenMock).toHaveBeenCalledTimes(1);
    const [provider, opts] = getDataTokenMock.mock.calls[0]!;
    expect(provider).toBe('google');
    expect(opts.sessionToken).toBe('old-token');
    expect(opts.click).toBeInstanceOf(Event);
    expect(opts.click.type).toBe('click');
    expect(connectGoogleSheetMock).toHaveBeenCalledWith('ya29.t', '1AbC', SHEET_URL);
    expect(connectionText()).toBe('Connected: Q3 P&L via data@example.com');
  });

  it('an Excel tab uses a Microsoft token, and flags a leftover Files.ReadWrite grant', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    setActiveTabUrl(EXCEL_URL);
    getDataTokenMock.mockResolvedValue({
      accessToken: 'ms-t',
      email: 'me@outlook.example',
      scope: 'User.Read Files.ReadWrite Files.Read',
    });
    connectExcelFileMock.mockResolvedValue(fakeFile({ provider: 'microsoft', platform: 'excel', name: 'P&L.xlsx' }));
    await loadSidepanel();

    await clickConnect();

    expect(getDataTokenMock.mock.calls[0]![0]).toBe('microsoft');
    expect(connectExcelFileMock).toHaveBeenCalledWith('ms-t', { kind: 'microsoft', url: EXCEL_URL });
    expect(connectionText()).toBe('Connected: P&L.xlsx via me@outlook.example');
    expect(document.querySelector('#data-note')?.textContent).toContain('older write permission');
  });

  it('an excel.cloud.microsoft OneDrive tab is resolved by its drive and item ids', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    setActiveTabUrl(
      'https://excel.cloud.microsoft/open/onedrive/?docId=951C971EBB28CD52%21s029c348d1f474d1a8fbb0ca639995392&driveId=951C971EBB28CD52',
    );
    getDataTokenMock.mockResolvedValue({ accessToken: 'ms-t', email: 'me@outlook.example', scope: 'User.Read Files.Read' });
    connectExcelFileMock.mockResolvedValue(fakeFile({ provider: 'microsoft', platform: 'excel', name: 'P&L.xlsx' }));
    await loadSidepanel();

    await clickConnect();

    expect(connectExcelFileMock).toHaveBeenCalledWith('ms-t', {
      kind: 'microsoft',
      driveId: '951C971EBB28CD52',
      itemId: '951C971EBB28CD52!s029c348d1f474d1a8fbb0ca639995392',
    });
    expect(connectionText()).toBe('Connected: P&L.xlsx via me@outlook.example');
  });

  it('an unsupported excel.cloud.microsoft URL shows a clear message and never requests a token', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    setActiveTabUrl('https://excel.cloud.microsoft/open/sharepoint/?docId=X&driveId=Y');
    await loadSidepanel();

    await clickConnect();

    expect(getDataTokenMock).not.toHaveBeenCalled();
    expect(connectExcelFileMock).not.toHaveBeenCalled();
    expect(statusText()).toBe(
      "This Excel link type isn't supported yet. Open the file from onedrive.live.com or SharePoint, then click Connect data again.",
    );
    expect(isHidden('#signed-in-view')).toBe(false);
  });

  it('an unsupported tab never requests a token', async () => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    setActiveTabUrl('https://example.com/');
    await loadSidepanel();

    await clickConnect();

    expect(getDataTokenMock).not.toHaveBeenCalled();
    expect(statusText()).toBe('Open a Google Sheet or an Excel file first.');
  });
});

describe('Connect data error handling', () => {
  beforeEach(() => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    setActiveTabUrl(SHEET_URL);
  });

  it('a backend 401 clears the session (and with it data access), shows signed-out, and never launches a flow', async () => {
    getDataTokenMock.mockRejectedValue(
      new BackendApiError('failed', 401, 'Invalid or expired session token.'),
    );
    await loadSidepanel();

    await clickConnect();

    expect(clearStoredSessionMock).toHaveBeenCalled();
    expect(isHidden('#signed-out-view')).toBe(false);
    expect(statusText()).toBe('Your session ended — please sign in again.');
    expect(getDataTokenMock).toHaveBeenCalledTimes(1);
    expect(launchAuthFlowMock).not.toHaveBeenCalled();
  });

  it('a 400 keeps the session and asks to try again', async () => {
    getDataTokenMock.mockRejectedValue(
      new BackendApiError('failed', 400, 'Google authorization could not be exchanged.'),
    );
    await loadSidepanel();

    await clickConnect();

    expect(clearStoredSessionMock).not.toHaveBeenCalled();
    expect(isHidden('#signed-in-view')).toBe(false);
    expect(statusText()).toBe("Google didn't complete the authorization — try again.");
  });

  it('a failed interactive window after a silent attempt says to click again, never silently', async () => {
    getDataTokenMock.mockRejectedValue(
      new InteractiveAfterSilentFailedError(
        "Couldn't open the sign-in window — click Connect data again.",
      ),
    );
    await loadSidepanel();

    await clickConnect();

    expect(isHidden('#signed-in-view')).toBe(false);
    expect(statusText()).toBe("Couldn't open the sign-in window — click Connect data again.");
  });

  it('a missing Sheets scope shows the named message', async () => {
    getDataTokenMock.mockRejectedValue(
      new ScopeNotGrantedError(
        "Sheets access wasn't granted. Click Connect data and leave the Sheets box ticked.",
      ),
    );
    await loadSidepanel();

    await clickConnect();

    expect(statusText()).toContain("Sheets access wasn't granted");
  });

  it("a 403 from the Sheets API forgets that provider's grant, so the next click shows the chooser", async () => {
    getDataTokenMock.mockResolvedValue({ accessToken: 't', email: 'data@example.com', scope: '' });
    connectGoogleSheetMock.mockRejectedValue(new ProviderApiError(403));
    await loadSidepanel();

    await clickConnect();

    expect(forgetDataGrantMock).toHaveBeenCalledWith('google');
    expect(clearStoredSessionMock).not.toHaveBeenCalled();
    expect(statusText()).toBe(
      "data@example.com can't open this file — click Connect data to choose another account.",
    );
  });

  it('a 401 from the Sheets API forgets the grant and asks to click again, with no retry', async () => {
    getDataTokenMock.mockResolvedValue({ accessToken: 't', email: 'data@example.com', scope: '' });
    connectGoogleSheetMock.mockRejectedValue(new ProviderApiError(401));
    await loadSidepanel();

    await clickConnect();

    expect(forgetDataGrantMock).toHaveBeenCalledWith('google');
    expect(getDataTokenMock).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe('Access expired — click Connect data again.');
  });
});

describe('read panel: sheet, range, preview and send (session 3b)', () => {
  const googleToken = { accessToken: 'ya29.t', email: 'data@example.com', scope: '' };

  beforeEach(() => {
    getStoredSessionMock.mockResolvedValue(existingSession);
    setActiveTabUrl(SHEET_URL);
    getDataTokenMock.mockResolvedValue(googleToken);
  });

  it('Connect data fills the sheet selector and pre-fills the range= link for its sheet', async () => {
    connectGoogleSheetMock.mockResolvedValue(fakeFile());
    await loadSidepanel();
    expect(isHidden('#read-panel')).toBe(true);

    await clickConnect();

    expect(isHidden('#read-panel')).toBe(false);
    expect([...el<HTMLSelectElement>('#sheet-select').options].map((o) => o.value)).toEqual(['P&L', 'Notes']);
    expect(el<HTMLSelectElement>('#sheet-select').value).toBe('P&L');
    expect(el<HTMLInputElement>('#range-input').value).toBe('A3:C4');
    expect(el('#file-as-of').textContent).toBe('');

    // The link's range belongs to its own sheet only.
    el<HTMLSelectElement>('#sheet-select').value = 'Notes';
    el('#sheet-select').dispatchEvent(new Event('change'));
    expect(el<HTMLInputElement>('#range-input').value).toBe('');
    el<HTMLSelectElement>('#sheet-select').value = 'P&L';
    el('#sheet-select').dispatchEvent(new Event('change'));
    expect(el<HTMLInputElement>('#range-input').value).toBe('A3:C4');
  });

  it('an Excel file shows "data as of last save"', async () => {
    setActiveTabUrl(EXCEL_URL);
    getDataTokenMock.mockResolvedValue({ accessToken: 'ms-t', email: 'me@outlook.example', scope: '' });
    connectExcelFileMock.mockResolvedValue(
      fakeFile({ provider: 'microsoft', platform: 'excel', modifiedAt: '2026-09-30T12:17:57Z', defaultRange: null }),
    );
    await loadSidepanel();

    await clickConnect();

    expect(el('#file-as-of').textContent).toContain('Data as of last save:');
    expect(el('#file-as-of').textContent).toContain('may not appear yet');
  });

  it('an Excel file refused before download shows why', async () => {
    setActiveTabUrl(EXCEL_URL);
    getDataTokenMock.mockResolvedValue({ accessToken: 'ms-t', email: 'me@outlook.example', scope: '' });
    connectExcelFileMock.mockRejectedValue(new ExcelFileError('"big.xlsx" is 12.0 MB; files over 10 MB can\'t be read.'));
    await loadSidepanel();

    await clickConnect();

    expect(statusText()).toContain('files over 10 MB');
    expect(isHidden('#read-panel')).toBe(true);
    expect(forgetDataGrantMock).not.toHaveBeenCalled();
  });

  it('Preview reads the chosen sheet and range with a token from that click, and shows what will be sent', async () => {
    const file = fakeFile();
    connectGoogleSheetMock.mockResolvedValue(file);
    await loadSidepanel();
    await clickConnect();

    await click('#read-range');

    expect(getDataTokenMock).toHaveBeenCalledTimes(2);
    expect(getDataTokenMock.mock.calls[1]![1].click.type).toBe('click');
    expect(file.read).toHaveBeenCalledWith('P&L', { startRow: 3, startCol: 0, endRow: 4, endCol: 2 }, 'ya29.t');
    const cells = [...el('#preview-table').querySelectorAll('td')];
    expect(cells.map((c) => c.textContent)).toEqual(['2025-03-31', '1250000', '0.6145038167938931']);
    expect(cells[1]!.title).toBe('B4 shows "$1,250,000"');
    expect(el('#preview-caption').textContent).toBe("'P&L'!A3:C4 · 1 data row × 3 columns");
    expect(isHidden('#send-data')).toBe(false);
  });

  it('an Excel preview re-parses the downloaded copy without asking for a token', async () => {
    setActiveTabUrl(EXCEL_URL);
    getDataTokenMock.mockResolvedValue({ accessToken: 'ms-t', email: 'me@outlook.example', scope: '' });
    const file = fakeFile({ provider: 'microsoft', platform: 'excel', defaultRange: null });
    connectExcelFileMock.mockResolvedValue(file);
    await loadSidepanel();
    await clickConnect();

    await click('#read-range');

    expect(getDataTokenMock).toHaveBeenCalledTimes(1); // the Connect data click only
    expect(file.read).toHaveBeenCalledWith('P&L', null, null);
  });

  it('an unreadable range is caught before any read', async () => {
    const file = fakeFile();
    connectGoogleSheetMock.mockResolvedValue(file);
    await loadSidepanel();
    await clickConnect();
    el<HTMLInputElement>('#range-input').value = 'P&L!A3';

    await click('#read-range');

    expect(file.read).not.toHaveBeenCalled();
    expect(statusText()).toBe('Enter a range like A3:G7, or leave it empty for the whole sheet.');
  });

  it('a title row picked up as the header points at the range field and offers no Send', async () => {
    const titleGrid = pnlGrid([
      [textCell('Acme Co — Quarterly P&L'), { kind: 'empty', value: '', display: '' }, { kind: 'empty', value: '', display: '' }],
      [textCell('Period'), textCell('Revenue'), textCell('Margin')],
    ]);
    connectGoogleSheetMock.mockResolvedValue(fakeFile({ read: vi.fn(async () => titleGrid) }));
    await loadSidepanel();
    await clickConnect();

    await click('#read-range');

    expect(statusText()).toMatch(/^Row 3 looks like a title/);
    expect(isHidden('#send-data')).toBe(true);
  });

  it('a 403 during Preview forgets the grant and closes the read panel', async () => {
    connectGoogleSheetMock.mockResolvedValue(
      fakeFile({ read: vi.fn(async () => Promise.reject(new ProviderApiError(403))) }),
    );
    await loadSidepanel();
    await clickConnect();

    await click('#read-range');

    expect(forgetDataGrantMock).toHaveBeenCalledWith('google');
    expect(isHidden('#read-panel')).toBe(true);
    expect(statusText()).toContain("can't open this file");
  });

  it('Send posts the previewed values with the sheet source, then shows the backend summary', async () => {
    connectGoogleSheetMock.mockResolvedValue(fakeFile());
    parseCsvMock.mockResolvedValue({
      csv_context_id: 'ctx-123',
      columns: ['Period', 'Revenue', 'Margin'],
      sample_rows: [['2025-03-31', '1250000', '0.6145038167938931']],
      parse_error: null,
    });
    await loadSidepanel();
    await clickConnect();
    await click('#read-range');

    await click('#send-data');

    expect(parseCsvMock).toHaveBeenCalledWith('old-token', {
      rows: [
        ['Period', 'Revenue', 'Margin'],
        ['2025-03-31', '1250000', '0.6145038167938931'],
      ],
      filename: 'Q3 P&L — P&L',
      source: { platform: 'google_sheets', sheet_name: 'P&L', range: 'A3:C4', file_name: 'Q3 P&L', modified_at: null },
    });
    expect(isHidden('#parse-summary')).toBe(false);
    expect(el('#parse-summary-text').textContent).toContain("Sent 'P&L'!A3:C4: 1 data rows × 3 columns");
    expect(el('#parse-summary-text').textContent).toContain('ctx-123');
    expect([...el('#parse-sample-table').querySelectorAll('td')].map((c) => c.textContent)).toEqual([
      '2025-03-31',
      '1250000',
      '0.6145038167938931',
    ]);
    expect(isHidden('#send-data')).toBe(true);
  });

  it("shows the backend's own refusal as it says it", async () => {
    connectGoogleSheetMock.mockResolvedValue(fakeFile());
    parseCsvMock.mockResolvedValue({
      csv_context_id: null,
      columns: [],
      sample_rows: [],
      parse_error: "'Q3 P&L — P&L' has 2001 data rows -- the limit is 2000.",
    });
    await loadSidepanel();
    await clickConnect();
    await click('#read-range');

    await click('#send-data');

    expect(statusText()).toBe("'Q3 P&L — P&L' has 2001 data rows -- the limit is 2000.");
    expect(isHidden('#parse-summary')).toBe(true);
  });

  it('a 401 from Send ends the session', async () => {
    connectGoogleSheetMock.mockResolvedValue(fakeFile());
    parseCsvMock.mockRejectedValue(new BackendApiError('failed', 401, 'x'));
    await loadSidepanel();
    await clickConnect();
    await click('#read-range');

    await click('#send-data');

    expect(clearStoredSessionMock).toHaveBeenCalled();
    expect(isHidden('#signed-out-view')).toBe(false);
    expect(isHidden('#read-panel')).toBe(true);
  });

  it('editing the range discards a stale preview', async () => {
    connectGoogleSheetMock.mockResolvedValue(fakeFile());
    await loadSidepanel();
    await clickConnect();
    await click('#read-range');
    expect(isHidden('#send-data')).toBe(false);

    el<HTMLInputElement>('#range-input').value = 'A3:C9';
    el('#range-input').dispatchEvent(new Event('input'));

    expect(isHidden('#send-data')).toBe(true);
    expect(el('#preview-table').children).toHaveLength(0);
  });
});

describe('a successful sign-in starts with no data-access state', () => {
  it('clears data tokens and data-account emails before storing the new session', async () => {
    launchAuthFlowMock.mockResolvedValue({ provider: 'microsoft', oauthToken: 't' });
    exchangeTokenMock.mockResolvedValue({
      session_token: 'new-token',
      account_id: 'account-new',
      expires_at: '2026-02-01T00:00:00Z',
    });
    await loadSidepanel();

    document.querySelector<HTMLButtonElement>('#signin-microsoft')!.click();
    await flushAsync();

    expect(clearAllDataAccessMock).toHaveBeenCalled();
    expect(clearAllDataAccessMock.mock.invocationCallOrder[0]!).toBeLessThan(
      setStoredSessionMock.mock.invocationCallOrder[0]!,
    );
  });
});

// import.meta.env.MODE is a build-time constant in real builds; under Vitest it's a
// runtime value vi.stubEnv can change, and loadSidepanel() re-imports main.ts fresh each
// time (vi.resetModules), so each test sees its own mode.
describe('backend label (non-production builds only)', () => {
  let consoleInfo: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleInfo = vi.spyOn(console, 'info').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    consoleInfo.mockRestore();
  });

  it('development mode shows "Backend: <origin>" and logs it', async () => {
    vi.stubEnv('MODE', 'development');
    await loadSidepanel();

    expect(document.querySelector('#backend-info')?.textContent).toBe(
      'Backend: https://backend.invalid',
    );
    expect(consoleInfo).toHaveBeenCalledWith('[sidepanel] Backend:', 'https://backend.invalid');
  });

  it('production mode renders no label but still logs the backend', async () => {
    vi.stubEnv('MODE', 'production');
    await loadSidepanel();

    expect(document.querySelector('#backend-info')).toBeNull();
    expect(document.body.textContent).not.toContain('Backend:');
    expect(consoleInfo).toHaveBeenCalledWith('[sidepanel] Backend:', 'https://backend.invalid');
  });
});
