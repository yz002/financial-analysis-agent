export default defineBackground(() => {
  // Mirrors host_permissions in wxt.config.ts (design doc §1a). Chrome's match-pattern
  // semantics for "*.host" require at least one subdomain label before the wildcard
  // resolves — it does NOT also match the bare apex domain — so each wildcard pattern
  // below uses `(label\.)+`, not `(label\.)*`.
  const ALLOWED_HOST_PATTERNS: RegExp[] = [
    /^https:\/\/docs\.google\.com\/spreadsheets\//,
    /^https:\/\/onedrive\.live\.com\//,
    /^https:\/\/([a-z0-9-]+\.)+sharepoint\.com\//,
    /^https:\/\/([a-z0-9-]+\.)+officeapps\.live\.com\//,
    /^https:\/\/([a-z0-9-]+\.)+cloud\.microsoft\//,
  ];

  function isSupportedUrl(url: string | undefined): boolean {
    if (!url) return false;
    return ALLOWED_HOST_PATTERNS.some((pattern) => pattern.test(url));
  }

  async function syncPanelForTab(tabId: number, url: string | undefined) {
    const enabled = isSupportedUrl(url);
    try {
      await browser.sidePanel.setOptions({
        tabId,
        path: 'sidepanel.html',
        enabled,
      });
    } catch (err) {
      console.error('[background] failed to update side panel for tab', tabId, err);
    }
  }

  // Disable the panel by default for any tab without a per-tab override --
  // load-bearing now that wxt.config.ts strips manifest.side_panel, since
  // sidePanel.setOptions' own documented default for `enabled` is true.
  browser.sidePanel
    .setOptions({ path: 'sidepanel.html', enabled: false })
    .catch((err) => console.error('[background] failed to set default side panel options', err));

  // Open the panel directly on a toolbar-icon click instead of requiring the
  // browser's side-panel dropdown. This only takes effect on tabs where the
  // panel is currently enabled -- Chrome evaluates each tab's PanelOptions
  // (set via setOptions above) before opening, so a click on a disabled/
  // unsupported tab no-ops rather than forcing the panel open there.
  browser.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.error('[background] failed to set side panel behavior', err));

  // Evaluate the tab the person is actually looking at right now: onUpdated/
  // onActivated only fire for *future* navigation/activation events, so a tab
  // that was already open and already active when the service worker
  // (re)starts would otherwise sit on the disabled default above until the
  // user happens to navigate it or switch away and back.
  browser.tabs.query({ active: true, currentWindow: true }).then(([activeTab]) => {
    if (activeTab?.id !== undefined) syncPanelForTab(activeTab.id, activeTab.url);
  });

  browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.url !== undefined || changeInfo.status === 'complete') {
      syncPanelForTab(tabId, tab.url ?? changeInfo.url);
    }
  });

  browser.tabs.onActivated.addListener(({ tabId }) => {
    browser.tabs.get(tabId).then((tab) => {
      syncPanelForTab(tabId, tab.url);
    });
  });
});
