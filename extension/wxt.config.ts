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
    permissions: ['identity', 'storage', 'tabs'],
    host_permissions: [
      'https://docs.google.com/spreadsheets/*',
      'https://onedrive.live.com/*',
      'https://*.sharepoint.com/*',
      'https://*.officeapps.live.com/*',
      'https://*.cloud.microsoft/*',
      // OAuth token-exchange endpoints this extension's own code fetch()es directly
      // (session 2). accounts.google.com and Microsoft's /authorize endpoint don't
      // need an entry -- those are only navigated to by launchWebAuthFlow, never
      // fetch()ed from extension code.
      'https://oauth2.googleapis.com/*',
      'https://login.microsoftonline.com/*',
      // Not called yet this session -- pre-declared for a later session's Graph calls.
      'https://graph.microsoft.com/*',
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
