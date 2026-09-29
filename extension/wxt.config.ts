import { defineConfig } from 'wxt';

// See https://wxt.dev/api/config.html
export default defineConfig({
  manifest: {
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
      // TODO(human): this codebase has no fixed deployed backend origin yet. Add it
      // here once known (must match lib/authConfig.ts's BACKEND_BASE_URL). Local dev
      // origin included for this session's manual verification.
      'http://127.0.0.1:8000/*',
    ],
  },
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
