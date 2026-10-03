import { defineConfig } from 'wxt';
import { hostPermissionFor, resolveBackendBaseUrl } from './lib/backendUrl';

// See https://wxt.dev/api/config.html
export default defineConfig({
  // Every mode builds into the same .output/chrome-mv3 folder (WXT's default template
  // appends "-dev" for development mode). The unpacked extension's ID is derived from
  // its folder path -- there's no manifest `key` -- and the Google/Azure OAuth redirect
  // URIs (https://<id>.chromiumapp.org/) are registered for that ID, so a mode-specific
  // folder would silently break sign-in with a redirect-URI mismatch. Switching modes
  // therefore overwrites the previous build; reload the extension afterward.
  outDirTemplate: '{{browser}}-mv{{manifestVersion}}',
  // A function so it runs after WXT has loaded .env.[mode] into process.env (the object
  // form is evaluated when this file is imported, before that happens).
  manifest: () => ({
    name: 'Financial Analysis Agent',
    // 119+: fetch drops the Authorization header on a cross-origin redirect, which the Excel
    // download's /content fallback relies on so the Graph token never reaches the download
    // host (lib/excelReader.ts). Also above sidePanel's 114 floor.
    minimum_chrome_version: '119',
    permissions: ['identity', 'storage', 'tabs'],
    host_permissions: [
      'https://docs.google.com/spreadsheets/*',
      'https://onedrive.live.com/*',
      'https://*.sharepoint.com/*',
      'https://*.officeapps.live.com/*',
      'https://*.cloud.microsoft/*',
      // Microsoft's token endpoint, which this extension's own code fetch()es directly
      // (a public SPA client). Google's token endpoint isn't listed: since session 2 the
      // backend does every Google code exchange. accounts.google.com and Microsoft's
      // /authorize endpoint don't need an entry -- those are only navigated to by
      // launchWebAuthFlow, never fetch()ed from extension code.
      'https://login.microsoftonline.com/*',
      // Data APIs called with data tokens (session 3a): Graph (/me, /shares, item
      // metadata), the Sheets API, and the personal-OneDrive download host that
      // @microsoft.graph.downloadUrl pointed to in the session 3a spike (used from 3b).
      'https://graph.microsoft.com/*',
      'https://sheets.googleapis.com/*',
      'https://my.microsoftpersonalcontent.com/*',
      // This build's backend only -- Render for production, 127.0.0.1:8000 for
      // development -- from the same env var and validator as lib/authConfig.ts's
      // BACKEND_BASE_URL. Load-bearing: the backend sends no CORS headers, and this
      // permission is what exempts the side panel's fetch() from CORS.
      hostPermissionFor(resolveBackendBaseUrl(process.env.WXT_BACKEND_BASE_URL)),
    ],
  }),
  hooks: {
    // WXT unconditionally sets manifest.side_panel.default_path whenever a
    // `sidepanel` entrypoint exists, which makes every tab side-panel-enabled
    // by default -- the opposite of this extension's per-tab-enable design.
    // Strip it post-generation; background.ts calls sidePanel.setOptions with
    // an explicit `path` on every tab itself, so no manifest-level default
    // is needed. (The "sidePanel" permission WXT also adds is untouched.)
    'build:manifestGenerated': (_wxt, manifest) => {
      delete manifest.side_panel;
    },
  },
});
