import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  classifyTab,
  encodeSharingUrl,
  fetchExcelFileName,
  fetchSheetTitle,
  ProviderApiError,
} from './connectData';

// The personal Excel-for-the-web edit URL shape observed in the session 3a spike.
const ONEDRIVE_URL =
  'https://onedrive.live.com/personal/abc123/_layouts/15/doc.aspx?sourcedoc={0F1E2D3C-AAAA-BBBB-CCCC-112233445566}&action=edit';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('classifyTab', () => {
  it('reads the spreadsheet ID from a Sheets URL with a gid/range fragment', () => {
    expect(
      classifyTab('https://docs.google.com/spreadsheets/d/1AbC_d-EF/edit?gid=0#gid=0&range=A3:G7'),
    ).toEqual({ kind: 'google', spreadsheetId: '1AbC_d-EF' });
  });

  it.each([
    ONEDRIVE_URL,
    'https://contoso.sharepoint.com/:x:/r/sites/fin/_layouts/15/Doc.aspx?sourcedoc={GUID}&file=Q3.xlsx',
    'https://excel.officeapps.live.com/x/_layouts/xlviewerinternal.aspx',
    'https://excel.cloud.microsoft/open/onedrive/?docId=ABC',
  ])('recognizes the Microsoft page host in %s', (url) => {
    expect(classifyTab(url)).toEqual({ kind: 'microsoft', url });
  });

  it.each([
    undefined,
    'https://example.com/',
    'https://docs.google.com/document/d/1AbC/edit',
    'https://sharepoint.com/', // a bare apex is not a tenant subdomain
  ])('returns null for %s', (url) => {
    expect(classifyTab(url)).toBeNull();
  });
});

describe('encodeSharingUrl', () => {
  it("matches Graph's documented encoding for a hand-computed vector", () => {
    // base64("https://a.b/c?d=e") = "aHR0cHM6Ly9hLmIvYz9kPWU=" -> strip "=", prefix "u!".
    expect(encodeSharingUrl('https://a.b/c?d=e')).toBe('u!aHR0cHM6Ly9hLmIvYz9kPWU');
  });

  it("maps base64's '/' and '+' to '_' and '-'", () => {
    // "??>" encodes to "Pz8+" and "???" to "Pz8/" in standard base64.
    expect(encodeSharingUrl('??>')).toBe('u!Pz8-');
    expect(encodeSharingUrl('???')).toBe('u!Pz8_');
  });
});

describe('fetchSheetTitle', () => {
  it('calls spreadsheets.get for the title only, with the bearer token', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ properties: { title: 'Q3 P&L' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(fetchSheetTitle('ya29.token', '1AbC')).resolves.toBe('Q3 P&L');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('https://sheets.googleapis.com/v4/spreadsheets/1AbC?fields=properties.title');
    expect(init.headers).toEqual({ Authorization: 'Bearer ya29.token' });
  });

  it('throws ProviderApiError with the status on a non-2xx response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({}) }));

    await expect(fetchSheetTitle('t', '1AbC')).rejects.toEqual(new ProviderApiError(403));
    await expect(fetchSheetTitle('t', '1AbC')).rejects.toMatchObject({ status: 403 });
  });
});

describe('fetchExcelFileName', () => {
  it('resolves the tab URL via /shares with redeemSharingLinkIfNecessary', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ name: 'P&L.xlsx' }) });
    vi.stubGlobal('fetch', fetchMock);

    await expect(fetchExcelFileName('ms-token', ONEDRIVE_URL)).resolves.toBe('P&L.xlsx');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(
      `https://graph.microsoft.com/v1.0/shares/${encodeSharingUrl(ONEDRIVE_URL)}/driveItem?$select=name`,
    );
    expect(init.headers).toEqual({
      Authorization: 'Bearer ms-token',
      Prefer: 'redeemSharingLinkIfNecessary',
    });
  });

  it('throws ProviderApiError with the status on a non-2xx response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));

    await expect(fetchExcelFileName('t', ONEDRIVE_URL)).rejects.toMatchObject({ status: 404 });
  });
});
