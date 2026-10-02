/**
 * The minimal "Connect data" read (Phase D session 3a): identify which spreadsheet the active
 * tab shows and fetch just its name with a data token -- enough to test the data-token
 * architecture live before session 3b's full file/range adapters. 3b builds on these rather
 * than replacing them (chrome-extension-design.md SS4).
 */

/**
 * How a Microsoft tab is resolved to its DriveItem:
 * - `url`: Graph /shares on the tab URL (onedrive.live.com, SharePoint, officeapps).
 * - `driveId` + `itemId`: read straight off an excel.cloud.microsoft/open/onedrive/ URL, whose
 *   `docId` is the Graph item id. /shares rejects that URL shape ("Invalid shares key").
 */
export type MicrosoftTab =
  | { kind: 'microsoft'; url: string }
  | { kind: 'microsoft'; driveId: string; itemId: string };

export type SpreadsheetTab =
  | { kind: 'google'; spreadsheetId: string }
  | MicrosoftTab
  // An excel.cloud.microsoft URL of a shape neither path above resolves. Never sent to /shares.
  | { kind: 'unsupported-excel-url' };

export const UNSUPPORTED_EXCEL_URL_MESSAGE =
  "This Excel link type isn't supported yet. Open the file from onedrive.live.com or " +
  'SharePoint, then click Connect data again.';

/** A non-2xx response from Google's or Microsoft's API, with its status kept for handling. */
export class ProviderApiError extends Error {
  constructor(public readonly status: number) {
    super(`Provider API request failed (${status}).`);
  }
}

const GOOGLE_SHEET_URL = /^https:\/\/docs\.google\.com\/spreadsheets\/d\/([A-Za-z0-9_-]+)/;

// The Microsoft page hosts that resolve through /shares, from design SS1a (background.ts
// enables the panel on these plus *.cloud.microsoft, handled separately below).
const SHARES_RESOLVABLE_HOSTS: RegExp[] = [
  /^https:\/\/onedrive\.live\.com\//,
  /^https:\/\/([a-z0-9-]+\.)+sharepoint\.com\//,
  /^https:\/\/([a-z0-9-]+\.)+officeapps\.live\.com\//,
];

export function classifyTab(url: string | undefined): SpreadsheetTab | null {
  if (!url) return null;
  const google = GOOGLE_SHEET_URL.exec(url);
  if (google) return { kind: 'google', spreadsheetId: google[1]! };
  if (SHARES_RESOLVABLE_HOSTS.some((pattern) => pattern.test(url))) return { kind: 'microsoft', url };
  return classifyCloudMicrosoftUrl(url);
}

/**
 * excel.cloud.microsoft (the unified Microsoft 365 domain, design SS1a). The one verified
 * shape is a personal file at /open/onedrive/?docId=<item id>&driveId=<drive id>: live
 * testing showed docId (URL-decoded, e.g. "951C971EBB28CD52!s029c...") is the same 50-char
 * item id /shares returned for that file when opened via onedrive.live.com. Every other
 * cloud.microsoft shape (e.g. /open/sharepoint/, or missing params) is unverified, so it's
 * reported as unsupported rather than guessed at.
 */
function classifyCloudMicrosoftUrl(url: string): SpreadsheetTab | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:' || !/^([a-z0-9-]+\.)+cloud\.microsoft$/.test(parsed.hostname)) {
    return null;
  }
  // URLSearchParams.get() already percent-decodes, so "%21" arrives as "!".
  const itemId = parsed.searchParams.get('docId');
  const driveId = parsed.searchParams.get('driveId');
  if (parsed.pathname.replace(/\/+$/, '') === '/open/onedrive' && itemId && driveId) {
    return { kind: 'microsoft', driveId, itemId };
  }
  return { kind: 'unsupported-excel-url' };
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
 * Fetches the open Excel file's name.
 * - A tab URL goes through Graph /shares with redeemSharingLinkIfNecessary -- the "just peek"
 *   mode, deliberately not redeemSharingLink, which would grant durable access as a side
 *   effect (design SS4).
 * - A known drive/item pair is read directly from /drives/{driveId}/items/{itemId}, each id
 *   encoded as one path segment (the item id's "!" is left as-is by encodeURIComponent).
 */
export async function fetchExcelFileName(accessToken: string, tab: MicrosoftTab): Promise<string> {
  const json = await fetchDriveItem<{ name?: string }>(accessToken, tab, 'name');
  return json.name ?? '(unnamed file)';
}

/** The open Excel file's DriveItem, with just the `select`ed properties (see above). */
export async function fetchDriveItem<T>(
  accessToken: string,
  tab: MicrosoftTab,
  select: string,
): Promise<T> {
  const url =
    'url' in tab
      ? `https://graph.microsoft.com/v1.0/shares/${encodeSharingUrl(tab.url)}/driveItem?$select=${select}`
      : `https://graph.microsoft.com/v1.0/drives/${encodeURIComponent(tab.driveId)}/items/${encodeURIComponent(tab.itemId)}?$select=${select}`;
  const headers: Record<string, string> = { Authorization: `Bearer ${accessToken}` };
  if ('url' in tab) headers.Prefer = 'redeemSharingLinkIfNecessary';

  const response = await fetch(url, { headers });
  if (!response.ok) throw new ProviderApiError(response.status);
  return (await response.json()) as T;
}
