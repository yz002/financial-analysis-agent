import { resolveBackendBaseUrl } from './backendUrl';

export type AuthProvider = 'google' | 'microsoft';

export interface ProviderConfig {
  authorizationEndpoint: string;
  tokenEndpoint: string;
  clientId: string;
  scopes: string[];
}

// TODO(human): paste the Google Cloud Console OAuth client ID for the "Web application"
// client registered with the https://<extension-id>.chromiumapp.org/ redirect URI added
// under its Authorized redirect URIs -- NOT the pre-existing "Chrome Extension" type
// client. That client type has no redirect-URI field and only works with
// chrome.identity.getAuthToken, which this design deliberately doesn't use. See the
// session plan's manual verification step 3.
const GOOGLE_CLIENT_ID = '213195483839-elbc3ijbbah2r63f3srm903fuo138hmt.apps.googleusercontent.com';

// TODO(human): paste the Azure app registration's Application (client) ID. Register the
// https://<extension-id>.chromiumapp.org/ redirect URI under the "Single-page
// application" platform type in Azure, NOT "Mobile and desktop applications" -- see the
// session plan's manual verification step 4.
const MICROSOFT_CLIENT_ID = 'cc09f763-f7b4-4bec-a66f-f2725723f79d';

// `scopes` here are sign-in's, and identity only (Phase D session 3a): nothing sent to
// /v1/auth/exchange may read spreadsheet data. Data access is the separate, click-time grant
// in DATA_GRANT_CONFIG below (chrome-extension-design.md SS2 "Data-access tokens").
export const PROVIDER_CONFIG: Record<AuthProvider, ProviderConfig> = {
  google: {
    authorizationEndpoint: 'https://accounts.google.com/o/oauth2/v2/auth',
    tokenEndpoint: 'https://oauth2.googleapis.com/token',
    clientId: GOOGLE_CLIENT_ID,
    scopes: ['openid', 'email'],
  },
  microsoft: {
    authorizationEndpoint: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    tokenEndpoint: 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
    clientId: MICROSOFT_CLIENT_ID,
    scopes: ['User.Read'],
  },
};

export const GOOGLE_SHEETS_READONLY_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly';

export interface DataGrantConfig {
  scopes: string[];
  extraParams: Record<string, string>;
}

/**
 * The data grant getDataToken (lib/dataToken.ts) requests, separately from sign-in. Google
 * includes openid/email so /v1/google/data-token can return the data account's email (the
 * login_hint for later silent re-auth), and include_granted_scopes so an existing grant is
 * reused. Never access_type=offline: no refresh token exists anywhere in this design.
 * Microsoft's is Files.Read, not Files.ReadWrite -- spike-verified sufficient (design SS3).
 */
export const DATA_GRANT_CONFIG: Record<AuthProvider, DataGrantConfig> = {
  google: {
    scopes: ['openid', 'email', GOOGLE_SHEETS_READONLY_SCOPE],
    extraParams: { include_granted_scopes: 'true' },
  },
  microsoft: {
    scopes: ['User.Read', 'Files.Read'],
    extraParams: {},
  },
};

// Selected at build time by WXT's mode: .env.production (the Render origin, the default
// for `npm run build`/`npm run zip`) or .env.development (127.0.0.1:8000, for
// `npm run build:local`/`npm run dev`). wxt.config.ts derives the matching
// host_permissions entry from the same variable through the same validator.
export const BACKEND_BASE_URL = resolveBackendBaseUrl(import.meta.env.WXT_BACKEND_BASE_URL);
