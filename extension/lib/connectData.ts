/**
 * The minimal "Connect data" read (Phase D session 3a): identify which spreadsheet the active
 * tab shows and fetch just its name with a data token -- enough to test the data-token
 * architecture live before session 3b's full file/range adapters. 3b builds on these rather
 * than replacing them (chrome-extension-design.md SS4).
 */

export type SpreadsheetTab =
  | { kind: 'google'; spreadsheetId: string }
  | { kind: 'microsoft'; url: string };

/** A non-2xx response from Google's or Microsoft's API, with its status kept for handling. */
export class ProviderApiError extends Error {
  constructor(public readonly status: number) {
    super(`Provider API request failed (${status}).`);
  }
}

const GOOGLE_SHEET_URL = /^https:\/\/docs\.google\.com\/spreadsheets\/d\/([A-Za-z0-9_-]+)/;

// The Microsoft page hosts from design SS1a (the same set background.ts enables the panel on).
const MICROSOFT_PAGE_HOSTS: RegExp[] = [
  /^https:\/\/onedrive\.live\.com\//,
  /^https:\/\/([a-z0-9-]+\.)+sharepoint\.com\//,
  /^https:\/\/([a-z0-9-]+\.)+officeapps\.live\.com\//,
  /^https:\/\/([a-z0-9-]+\.)+cloud\.microsoft\//,
];

export function classifyTab(url: string | undefined): SpreadsheetTab | null {
  if (!url) return null;
  const google = GOOGLE_SHEET_URL.exec(url);
  if (google) return { kind: 'google', spreadsheetId: google[1]! };
  if (MICROSOFT_PAGE_HOSTS.some((pattern) => pattern.test(url))) return { kind: 'microsoft', url };
  return null;
}

/**
 * Graph's sharing-URL encoding (shares-get): base64 of the URL's UTF-8 bytes, `=` padding
 * stripped, `/` -> `_`, `+` -> `-`, prefixed with `u!`.
 */
export function encodeSharingUrl(url: string): string {
  const bytes = new TextEncoder().encode(url);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const base64 = btoa(binary).replace(/=+$/, '').replace(/\//g, '_').replace(/\+/g, '-');
  return `u!${base64}`;
}

export async function fetchSheetTitle(accessToken: string, spreadsheetId: string): Promise<string> {
  const response = await fetch(
    `https://sheets.googleapis.com/v4/spreadsheets/${encodeURIComponent(spreadsheetId)}?fields=properties.title`,
    { headers: { Authorization: `Bearer ${accessToken}` } },
  );
  if (!response.ok) throw new ProviderApiError(response.status);
  const json = (await response.json()) as { properties?: { title?: string } };
  return json.properties?.title ?? '(untitled spreadsheet)';
}

/**
 * Resolves the open Excel tab's URL to its DriveItem via Graph /shares, with
 * redeemSharingLinkIfNecessary -- the "just peek" mode, deliberately not redeemSharingLink,
 * which would grant durable access as a side effect (design SS4).
 */
export async function fetchExcelFileName(accessToken: string, tabUrl: string): Promise<string> {
  const response = await fetch(
    `https://graph.microsoft.com/v1.0/shares/${encodeSharingUrl(tabUrl)}/driveItem?$select=name`,
    {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        Prefer: 'redeemSharingLinkIfNecessary',
      },
    },
  );
  if (!response.ok) throw new ProviderApiError(response.status);
  const json = (await response.json()) as { name?: string };
  return json.name ?? '(unnamed file)';
}
