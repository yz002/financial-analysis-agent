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

export const PROVIDER_CONFIG: Record<AuthProvider, ProviderConfig> = {
  google: {
    authorizationEndpoint: 'https://accounts.google.com/o/oauth2/v2/auth',
    tokenEndpoint: 'https://oauth2.googleapis.com/token',
    clientId: GOOGLE_CLIENT_ID,
    scopes: ['openid', 'email', 'https://www.googleapis.com/auth/spreadsheets.readonly'],
  },
  microsoft: {
    authorizationEndpoint: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    tokenEndpoint: 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
    clientId: MICROSOFT_CLIENT_ID,
    scopes: ['User.Read', 'Files.ReadWrite'],
  },
};

// TODO(human): this codebase does not yet fix the deployed backend's base URL anywhere
// (no extension/.env, nothing in backend/.env.example, no deploy config found). Point
// this at a local dev server for initial manual testing, or the real deployed origin
// once one exists. Keep this in sync with the matching host_permissions entry in
// wxt.config.ts.
export const BACKEND_BASE_URL = 'http://127.0.0.1:8000';
