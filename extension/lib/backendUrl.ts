/**
 * Validation for the build-time backend URL (WXT_BACKEND_BASE_URL, set per mode in
 * .env.production / .env.development). Shared by lib/authConfig.ts (the URL the bundle
 * fetch()es) and wxt.config.ts (the matching manifest host permission), so the two can
 * never disagree -- and a missing or malformed value fails the build/load loudly instead
 * of shipping an extension that silently calls the wrong place.
 */

const LOCAL_HOSTNAMES = new Set(['127.0.0.1', 'localhost']);

/**
 * Returns the URL's origin (scheme + host + port, no trailing slash). Throws when the
 * value is missing, isn't a URL, isn't https (plain http is allowed only for a local
 * backend), or carries anything beyond an origin -- a path, query, or fragment would
 * otherwise be silently concatenated with every request path.
 */
export function resolveBackendBaseUrl(raw: string | undefined): string {
  if (!raw) {
    throw new Error(
      'WXT_BACKEND_BASE_URL is not set. It comes from extension/.env.production or ' +
        'extension/.env.development, selected by the WXT build mode.',
    );
  }

  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`WXT_BACKEND_BASE_URL is not a valid URL: ${raw}`);
  }

  const isLocal = LOCAL_HOSTNAMES.has(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && isLocal)) {
    throw new Error(`WXT_BACKEND_BASE_URL must use https (http only for a local backend): ${raw}`);
  }
  if (url.pathname !== '/' || url.search || url.hash || raw.endsWith('/')) {
    throw new Error(`WXT_BACKEND_BASE_URL must be an origin only, with no path or trailing slash: ${raw}`);
  }

  return url.origin;
}

/** The manifest host_permissions match pattern covering every path on `origin`. */
export function hostPermissionFor(origin: string): string {
  return `${origin}/*`;
}
