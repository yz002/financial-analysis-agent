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
