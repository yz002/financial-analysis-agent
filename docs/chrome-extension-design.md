# Chrome Extension — Design (Phase D)

## Context

`backend/EXTENSION_INTEGRATION.md` (Phase C, session 7) closed out the backend side of the
Chrome extension's prerequisite work: real OAuth-verified sign-in, a revocable bearer session
token, and a full HTTP contract for CSV/Sheet ingestion and question-answering. That document
explicitly named two things it deliberately did **not** resolve, because they belong to the
extension, not the backend: the real OAuth data-access scopes needed to read a Sheet/Excel file,
and everything about the extension's own UI, manifest, and file/range-identification logic. This
session designs exactly that — the frontend side, end to end — so the next session can build
against it the way Phase C sessions built against `oauth-identity-session-design.md`.

**Decided, not re-litigated here:** Google Sheets and Excel Online are designed and built
together from day one — every layer below (auth, scopes, file/range identification, data-format
adapter) is specified for both platforms in the same pass, and the session breakdown at the end
pairs them per layer rather than doing all of Sheets first and Excel later.

**Read in full this session, per the brief:**
- `backend/EXTENSION_INTEGRATION.md` — the authoritative, code-verified contract this extension
  must satisfy exactly (§1 sign-in, §2–3 auth headers/401 handling, §4 logout/revoke, §6 routes).
- `oauth-identity-session-design.md` §2 — client-storage guidance: `chrome.storage.local`, never
  `.sync`, because `.sync` piggybacks on the browser's own account-sync guarantee, a different
  (and inapplicable) guarantee from this backend's own cross-device account resolution. Carried
  over verbatim below (§6).
- `sheets-backend-design.md` §1 — the Sheet-date-serialization contract: cell values must reach
  `POST /v1/csv/parse` as **display strings**, never a spreadsheet's internal numeric date-serial
  format. This is the load-bearing constraint on §5 below.

**Genuine unknowns resolved via fresh web search this session** (not assumed from training
data, per this project's own established practice after the Basil API and
userinfo-vs-tokeninfo incidents) — every numbered section below states what was confirmed and
cites where. Full source list at the end.

---

## 1. UI shell: `chrome.sidePanel`, not a popup or a content-script panel

Confirmed via research: `chrome.sidePanel` shipped stable in Chrome 114 (2023), is a first-class
Manifest V3 API (`side_panel` manifest key + `"sidePanel"` permission), and Edge ships a
compatible implementation. It persists across tab navigation, keeps its own state while the
person clicks around their spreadsheet, and renders in a native browser-chrome surface entirely
outside the host page's DOM/CSS — no injection, no host-page style conflicts.

- **Popup ruled out:** closes the instant it loses focus, which kills a chat session every time
  the person clicks back into a cell — unworkable for "a persistent UI the person can interact
  with while looking at their spreadsheet," the brief's own stated requirement.
- **Content-script-injected panel ruled out:** this project already established that Google
  Sheets renders its grid on canvas, not scrapeable DOM — a content-script panel gains nothing
  from DOM proximity to the page it can't already get from a side panel, while adding real cost
  (fighting the host page's own CSS/z-index, re-surviving the page's internal SPA navigation,
  and being subject to the host page's CSP). A side panel avoids all of that by construction.
- Per-tab scoping: use `chrome.tabs.onUpdated`/`onActivated` plus
  `chrome.sidePanel.setOptions({tabId, path, enabled})` to enable the panel only on tabs matching
  the host permissions in §1a below, rather than `setPanelBehavior({openPanelOnActionClick:true})`
  globally — so the panel doesn't offer itself on unrelated tabs.
- Cross-browser note, stated plainly: Firefox has its own, differently-shaped `sidebar_action`
  API — out of scope, matching this project's existing precedent of naming platform boundaries
  explicitly (desktop Excel is likewise out of scope, unchanged).

### 1a. Host permissions

```json
"host_permissions": [
  "https://docs.google.com/spreadsheets/*",
  "https://onedrive.live.com/*",
  "https://*.sharepoint.com/*",
  "https://*.officeapps.live.com/*",
  "https://*.cloud.microsoft/*"
]
```
Confirmed via research: Microsoft has been migrating its web apps (Word/Excel/PowerPoint) to a
unified `cloud.microsoft` domain since 2023, with `officeapps.live.com` still live during the
transition — both are included since which one a given tenant/account actually lands on isn't
something this design controls. `*.sharepoint.com` covers OneDrive-for-Business/SharePoint
(tenant subdomain varies per organization); `onedrive.live.com` covers personal Microsoft
accounts. Google Sheets has one stable host.

(Amended Phase D session 3a (spike-verified): the list above holds the *page* hosts the side panel is enabled on. Extension code also
`fetch()`es these API hosts, which need their own `host_permissions` entries:
`https://graph.microsoft.com/*`, `https://sheets.googleapis.com/*` (the Sheets API; added
during the 3a implementation, since the spike amendment had missed it), the backend's own
origin, `https://login.microsoftonline.com/*` (Microsoft's token endpoint), and
`https://my.microsoftpersonalcontent.com/*`, which is the host `@microsoft.graph.downloadUrl`
pointed to for a personal-OneDrive `.xlsx` in the spike (§4). Download hosts for work/school
OneDrive and SharePoint weren't observed. They're expected under `*.sharepoint.com`, which is
already listed, but that's unverified. `https://oauth2.googleapis.com/*` is no longer needed,
because since session 2 the extension never calls Google's token endpoint. See `docs/spikes/session3a-auth-file-access.md`.)

---

## 2. OAuth token acquisition — two different mechanisms, not one

### Google: `launchWebAuthFlow`, deliberately not `getAuthToken`

`getAuthToken` is Chrome's built-in bridge to whichever Google account is signed into the
*browser profile* (the profile's sync account, or its first web account) — confirmed via
Chrome's own docs. That's a real mismatch for this extension's actual use case: someone's Chrome
profile and the Google account that owns the Sheet they want analyzed are commonly different
accounts (a personal browser profile opening a work Sheet, or vice versa), and `getAuthToken`
gives the person no way to pick a different one from inside the extension.

`launchWebAuthFlow` instead opens Google's own account chooser at consent time, letting the
person pick any Google account regardless of the browser profile — confirmed via research
specifically on this tradeoff. **Recommendation: use `launchWebAuthFlow` for Google too**, even
though `getAuthToken` is the simpler, more commonly-documented path for Google specifically —
the account-mismatch problem is real and named in the brief itself, and using
`launchWebAuthFlow` for both providers also means one auth state machine and one redirect
handler instead of two structurally different code paths.

Flow: standard OAuth 2.0 Authorization Code + PKCE against
`accounts.google.com/o/oauth2/v2/auth`, `redirect_uri = chrome.identity.getRedirectURL()`
(resolves to `https://<extension-id>.chromiumapp.org/`), registered as a Google Cloud Console
"Web application" OAuth client with that `chromiumapp.org` redirect URI added to its authorized
redirect URIs.

(Amended Phase D session 2: an earlier revision of this section had the token exchange done
extension-side against Google's token endpoint, as a PKCE-only public client with no client
secret. Live testing showed that doesn't work: Google's "Web application" client type — the one
compatible with `launchWebAuthFlow`'s https redirect — is a confidential client, and its token
endpoint requires `client_secret` even when PKCE is used. A secret can't be kept confidential
inside an extension, so the extension no longer calls Google's token endpoint at all. It sends
the raw authorization `code` + `code_verifier` + `redirect_uri` to the backend's
`POST /v1/auth/exchange`, which performs the code-for-token exchange server-side with
`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` from its own environment. See
`EXTENSION_INTEGRATION.md` §1 for the per-provider request shapes.)

### Microsoft: `launchWebAuthFlow` (the only option — `getAuthToken` is Google-only)

Confirmed via Microsoft Learn: Microsoft's current recommended flow for any public client (SPA,
desktop, or browser extension) is Authorization Code + PKCE — the old implicit flow is
deprecated. Register the extension as a public client in Entra ID (no secret), authorize against
the `common` tenant endpoint (`login.microsoftonline.com/common/oauth2/v2.0/authorize`) since
both work/school and personal Microsoft accounts must be supported — the same audience
`oauth-identity-session-design.md` §1 already assumes for `User.Read`/Graph `/me` verification.
Same `chromiumapp.org` redirect URI mechanism as Google. (Unchanged by the Phase D session 2
amendment above: the Azure "Single-page application" platform type is a genuine no-secret
public client, so Microsoft's code-for-token exchange stays client-side in the extension.)

### Shared downstream wiring

Both flows funnel into one client-side module, `launchAuthFlow(provider)`, then straight into
`EXTENSION_INTEGRATION.md` §1's `POST /v1/auth/exchange` — with an `oauth_token` for Microsoft,
or `code` + `code_verifier` + `redirect_uri` for Google (per the amendment above). Nothing new to design there — this section only had to resolve *how
the extension gets the token in the first place*, not what to do with it afterward.

### Data-access tokens: separate from sign-in (Amended Phase D session 3a (spike-verified))

The investigation before the spike found that **no provider token outlives sign-in**. The
backend uses Google's access token once for `userinfo` and drops it. The Microsoft token is sent
to `/v1/auth/exchange` and dropped, and nothing asks for a refresh token. This section's earlier
text never said how the extension gets a token to read a spreadsheet hours or days into a
90-day session. It gets one this way, verified live in `docs/spikes/session3a-auth-file-access.md`:

1. **Sign-in asks for identity scopes only** (§3): Google `openid email`, Microsoft
   `User.Read`.
   - **Google:** `/v1/auth/exchange` never receives a token that can read data. Sign-in
     doesn't send `include_granted_scopes`, so its token carries only what it asked for.
   - **Microsoft: not true once a data grant exists. Live-verified in the 3a test run.**
     Microsoft adds every scope the account has already consented to for Graph, whatever the
     request asks for. Before any data grant, the sign-in token's granted scope was
     `User.Read`. After the `Files.Read` data grant, a fresh `User.Read`-only sign-in
     returned `User.Read Files.Read`. So the backend does receive a `Files.Read`-capable
     Graph token at every Microsoft sign-in after the first data grant. It uses the token
     once for `/me` and discards it, never storing or logging it. Accounts that consented to
     `Files.ReadWrite` before 3a would carry that too, until the person removes the consent
     (see §3). Removing this exposure is open item 1 in §10.
2. **Data tokens are obtained only when the user clicks something** that reads a spreadsheet,
   by one module, `getDataToken(provider)`:
   - **First, the cache.** Return a token from `chrome.storage.session` if one is there and not
     expired, with a safety margin.
   - **Otherwise, silent.** `launchWebAuthFlow({interactive: false,
     abortOnLoadForNonInteractive: false, timeoutMsForNonInteractive: …})` with `prompt=none`,
     the data scopes (Google also sends `include_granted_scopes=true`), and `login_hint` set to
     the **data account's** email. That's the account the last data grant for this provider was
     made with, which may not be the sign-in identity (see "Data account vs sign-in identity"
     below). The extension keeps it per provider:
     - Google: the backend returns it as `email` from `/v1/google/data-token`.
     - Microsoft: the extension reads `mail`, falling back to `userPrincipalName`, from Graph
       `/me` with the data token.

     (Amended Phase D session 3a (live-verified): **the data email is kept in
     `chrome.storage.local`**, key `fa_data_accounts`, not in `chrome.storage.session` as this
     section first said. It isn't a credential. Keeping it is what lets the first read after a
     browser restart run silently instead of showing the account chooser again. It's
     tied to the session: it's cleared on sign-out, Sign out everywhere, any `401`, **and
     every successful sign-in**. The last case means a new sign-in, possibly a different
     person on the same browser profile, never inherits the previous person's `login_hint`. Tokens stay
     session-only. See item 4 below and §6 3a.)

     If no data email is stored for the provider (no grant yet, or it was cleared), skip
     silent and go straight to interactive.
     - Spike-verified: Google returns a code with no UI after a real grant, including after a
       full Chrome restart and with two Google accounts signed in, when `login_hint` names the
       granted account.
     - Microsoft returns a code with no UI right after an interactive grant.
     - **Live-verified in the 3a test run, for both providers:** with the email in `.local`,
       a read after a full Chrome restart re-authenticated silently, with no window. This
       closes the spike's "inferred" item for Microsoft.
     - A wrong remembered email (live-tested with one that wasn't signed in) makes the silent
       attempt fail, and the account chooser opens within the same click.
   - **If silent fails, interactive**, in the same click, with `prompt=select_account` so the
     person picks which account's files to read.
     - **Live-verified in the 3a test run: Chrome doesn't require a user gesture for
       `launchWebAuthFlow`.** Calling it with `interactive: true` from the panel console after
       a 12-second `setTimeout`, with no user gesture at all, opened the Microsoft window.
     - **This is good for the fallback.** Even a silent attempt that runs to its 10-second
       timeout can still open the interactive window. If it ever doesn't, the extension shows
       "click Connect data again" rather than failing silently.
     - **It also means Chrome enforces nothing.** The extension's own `isTrusted` check on the
       click event, at the top of `getDataToken`, is the **only** thing enforcing the
       "only inside a click" rule (§6 step 5).
   - **The first data grant for each provider is always interactive and must complete a real
     code exchange.** Spike-verified: Google's `prompt=none` returned `interaction_required`
     after an authorize step whose code was never redeemed, and succeeded once a code had
     actually been exchanged.
3. **Exchange:**
   - **Google:** the extension sends `code` + `code_verifier` + `redirect_uri` to the new
     authenticated endpoint **`POST /v1/google/data-token`** (`EXTENSION_INTEGRATION.md` §1a).
     The backend exchanges the code with `GOOGLE_CLIENT_SECRET`, never sends
     `access_type=offline`, reads the data account's `email` from `userinfo`, and returns only
     `access_token`, `expires_in`, `scope` and `email`. It stores nothing. **It does not check
     that the Google account matches the sign-in identity** (see below). The Google data grant
     asks for `openid email spreadsheets.readonly`, since `email` is what fills `login_hint`
     for later silent re-auth.
   - **Microsoft:** the exchange stays client-side (a public SPA client), the same as sign-in,
     with scopes `User.Read Files.Read`. **Spike-verified: Microsoft returns a `refresh_token`
     even though `offline_access` isn't requested. The extension must throw it away and never
     store it.**
4. **Where data tokens live:** `chrome.storage.session` only, key `fa_data_tokens`, as
   `{accessToken, expiresAt, scope}` per provider. Never `.local`, never `.sync`.
   Spike-verified: it survives closing and reopening the side panel and is cleared by a
   Chrome restart. No refresh token is kept anywhere, extension or backend.
   (Amended Phase D session 3a (live-verified): the data email is no longer stored with the
   token. It's in `chrome.storage.local` (`fa_data_accounts`, see item 2). The live test
   confirmed `.local` held only `fa_session` and `fa_data_accounts`, with emails only.)
5. **If Google's granular consent drops the Sheets scope:** the user can untick individual
   scopes on Google's consent screen. The extension checks the returned `scope` and shows a
   named "Sheets access wasn't granted" message rather than failing later on a 403.
   It stores nothing from that grant, not even the email: a remembered email would make the
   next click silently fetch the same partial grant, so the consent screen would never come
   back. Live-verified: after unticking Sheets, the next click brought consent back with
   Sheets as the only new item.

**Data account vs sign-in identity: allowed to differ, on purpose.** The Google or Microsoft
account a data token is granted for **may differ from the identity the person signed in with**,
and that's intended. Examples:
- A Microsoft-signed-in person reading a Google Sheet.
- A Google-signed-in person whose Sheet is owned by a different Google account, e.g. a work
  Sheet opened from a personal sign-in.

That second case is exactly the account-mismatch problem that ruled out `getAuthToken` above.
Binding data tokens to the sign-in identity would bring it back. So neither provider checks the
data account:
- `/v1/google/data-token` doesn't compare the Google `sub` to the caller's linked identities.
- The extension doesn't compare a Microsoft data token's `/me` to the signed-in Microsoft
  identity. This closes the open question the first draft of this amendment raised, for the
  same reason.

Account resolution for sessions, billing and usage stays tied to the sign-in identity
(`oauth-identity-session-design.md` §3). The data account only decides which files can be read.

(Amended Phase D session 3a (live-verified): both cases above work.
- Signed in with Microsoft, Connect data on a Google Sheet connected through a Google data
  account.
- On a Sheet owned by a second Google account, the remembered data account got a "can't open
  this file" message. The extension then forgot that provider's grant, and the next click
  opened the chooser and connected through the owner's account.

In Google's "Testing" publishing mode, the second account had to be added as a test user
first; see open item 5 in §10. Signing in to the app *itself* as that second account created
a separate app account, as expected with no account linking.)

**Threat model:**
- **Why an identity check wouldn't add security.** `/v1/google/data-token` only exchanges a code
  the caller already obtained through Google's own consent screen. The code is PKCE-bound to
  this extension's authorization request and `redirect_uri`. Someone holding a stolen session
  token can therefore only mint Sheets tokens for Google accounts they can themselves consent
  for, i.e. accounts they already control. A `sub` check would block legitimate use and prevent
  essentially no attack.
- **Why the session requirement stays.** It's for authentication and abuse control: an
  anonymous caller can't use this backend's confidential client as a free code-exchange
  service. *Future consideration, not built:* per-account rate limiting on the endpoint.
- **An extension compromise** exposes at most one hour-long read token per provider, plus the
  data account's email. (Amended Phase D session 3a: the email now persists on disk in
  `chrome.storage.local` until sign-out, revoke-all, a `401` or the next sign-in, rather than
  only until a browser restart.)
- **A backend compromise** exposes Google data tokens in flight during a data-token exchange.
  (Amended Phase D session 3a (live-verified): it also exposes **Microsoft sign-in tokens in
  flight that can read files.** As item 1 above records, once a Microsoft data grant exists,
  every later Microsoft sign-in token carries `Files.Read`, or `Files.ReadWrite` for accounts
  that consented before 3a. The backend uses each such token once for `/me` and keeps none,
  so an attacker would have to capture them live, during sign-in, rather than read them from storage. The earlier claim that the backend
  "never sees" a Microsoft token able to read data was wrong for sign-in tokens. It still
  holds for Microsoft *data* tokens, which stay in the extension. Open item 1 in §10 removes
  the exposure.) The backend never sees any spreadsheet file, only the rows posted to
  `/v1/csv/parse`. No refresh token exists anywhere.
- **The click rule has one enforcement point.** Chrome lets `launchWebAuthFlow` run without a
  user gesture (item 2 above), so the `isTrusted` guard in `getDataToken` is all that stops
  code from opening an OAuth window, or silently obtaining a token, outside a click.

---

## 3. OAuth scopes — resolving `EXTENSION_INTEGRATION.md`'s named gap

### Google

- Identity scopes (already fixed by the backend, §1 of the contract): `openid`, `email`.
- (Amended Phase D session 3a (spike-verified): **sign-in no longer asks for the data scope.** It's asked for only by the separate data
  grant in §2 "Data-access tokens", together with `openid email` and
  `include_granted_scopes=true`.)
- Data-access scope: **`https://www.googleapis.com/auth/spreadsheets.readonly`** — confirmed
  sufficient for both metadata (`spreadsheets.get`) and cell reads (`spreadsheets.values.get`);
  a Sheets API scope covers every read operation on spreadsheet files, not one scope per method.
- **A Drive scope is not needed — this corrects an assumption in the original brief.** The brief
  asked whether a Drive scope is "separately needed to resolve a file from its URL." Confirmed
  it is not: a Google Sheets URL already embeds the spreadsheet ID directly
  (`docs.google.com/spreadsheets/d/{spreadsheetId}/edit#gid={sheetId}`), so there is no file to
  "resolve" via Drive — the ID is already in hand from the tab's URL. A Drive scope would only
  earn its keep for a future "browse all my Sheets" picker feature, which isn't part of this
  design. Stated plainly, matching this project's own convention of naming a corrected
  assumption rather than quietly dropping it.

### Microsoft

- Identity scope (already fixed by the backend): `User.Read`.
- **Amended Phase D session 3a (spike-verified): the data-access scope is now `Files.Read`, not `Files.ReadWrite`, and it's asked
  for only by the separate data grant in §2 "Data-access tokens". Microsoft sign-in asks for
  `User.Read` only.** The spike ran on a personal account with a token whose granted scope
  was `User.Read Files.Read` and nothing more. With it, `/shares/{encoded-url}/driveItem`,
  item metadata including `@microsoft.graph.downloadUrl`, and the file download all
  succeeded. `driveitem-get-content` documents `Files.Read` as least-privileged. The
  `shares-get` table says `Files.ReadWrite`, but the live result contradicts it. The
  finding below was about the workbook API, which B2 (§4) no longer uses. It's kept for the
  record: the spike found the workbook API *also* works with `Files.Read` on a personal
  account, contradicting these same tables. **Unverified:** `Files.Read` on work/school
  accounts, since there's no business tenant to test on.
- **Existing `Files.ReadWrite` consents (Amended Phase D session 3a).** People who signed in
  before 3a consented to `Files.ReadWrite`. That consent stays with Microsoft until they remove
  it themselves: account.live.com/consent/Manage for personal accounts, myapps.microsoft.com
  for work/school. The extension has no API to revoke it.
  - Because Microsoft carries consented scopes into every token (§2 item 1), those accounts'
    data tokens, and their sign-in tokens, may carry `Files.ReadWrite`.
  - The extension does nothing automatically about it. When a Microsoft data token's granted
    scope includes `Files.ReadWrite`, the Connect result adds a one-line note saying the
    account still grants an older write permission, with the place to remove it.
  - The live test account had no leftover `Files.ReadWrite`: its data token's granted scope
    was `User.Read Files.Read`.
- *(Superseded by the amendment above.)* Data-access scope: **`Files.ReadWrite` (delegated)** — **not** `Files.Read`. This is the
  single most important, non-obvious finding of this section, confirmed against Microsoft's own
  Graph API reference tables for both Graph calls this design actually needs (the `/shares`
  file-resolution call in §4, and the `workbook/worksheets/{}/range` read call itself): **both
  list `Files.ReadWrite` as the least-privileged delegated permission available, for both
  personal Microsoft accounts and work/school accounts — there is no lower, read-only option at
  all** ("Not available" is the documented alternative). Stated as a genuine, checkable trade-off
  rather than glossed over: this extension will hold write-capable Graph access it never
  exercises in code, purely because Excel's workbook/session API model doesn't expose a
  read-only floor — the same "name it plainly" posture this project's other design docs already
  take for their own real trade-offs (e.g. the sliding-session-token trade-off in
  `oauth-identity-session-design.md` §2).
- **`Sites.Read.All` is not required**, resolving the brief's question of whether OneDrive and
  SharePoint need different scopes: they don't, under this design. §4 below resolves *which*
  file is open via Graph's generic `/shares/{url}` endpoint rather than by enumerating SharePoint
  sites/drives directly, and `/shares` itself lists `Files.ReadWrite` as sufficient — with
  `Sites.ReadWrite.All` appearing only as a *higher*-privileged alternative, never a requirement.
  So the same single scope covers personal OneDrive, OneDrive for Business, and SharePoint-hosted
  files alike. (Amended Phase D session 3a (spike-verified): that single scope is now `Files.Read`. It's verified for personal OneDrive
  only, and unverified for OneDrive for Business and SharePoint.)

---

## 4. File/range identification per platform

### Google Sheets

(Amended Phase D session 3a (spike-verified): the steps below stand, with four changes.
- **Step 1:** the spike verified that `gid` updates live in both the query string and the hash
  when the user switches sheet tabs, with no reload. And "Get link to this range" puts
  `range=A3:G7` in the hash. **When `range=` is present, it pre-fills the range field.**
- **Steps 2–3** use a data token from `getDataToken('google')` (§2 "Data-access tokens").
  Nothing in sign-in provides one.
- **Step 4's escape hatch is now a range field in the read panel**, next to a sheet selector
  filled from `spreadsheets.get`'s sheet titles, not typed into chat. Chat doesn't exist until
  session 5, and the default whole-used-range read fails on typical financial layouts: title
  rows, blank rows and footnotes break `rows_to_raw_csv`'s header-row checks.
- **Freshness of Sheets data wasn't tested.** The Sheets API reads the live document.)

(Amended Phase D session 3b (live-verified, local and production):
- **One read call replaces steps 2–3's `values.get`.** `spreadsheets.get` with `ranges=` and a
  grid-data field mask returns each cell's raw value, displayed text and number-format type
  together, plus the range's origin, hidden rows and merges (§5 says how cells are encoded).
  The sheet list for the selector still comes from `fields=sheets.properties(sheetId,title)`.
- **Sheet selector and range field.** The selector defaults to the live `gid` sheet. A `range=`
  link pre-fills the range field only when its `gid` matches a sheet. **Any sheet switch clears
  the range field**, whether the range was pre-filled or typed: a range belongs to its sheet.
  An empty range field means the sheet's whole used range.
- **A requested range is never shrunk silently.** Trailing empty rows and columns are trimmed
  (only trailing ones, so every cell keeps its address), and the preview says what was left
  out, for example "Column H was empty and was left out." Confirmed live on `A3:J7`.
- **Freshness.** A committed edit (after Enter) shows up on the very next read, including
  formulas recalculated from it. An unfinished edit (still typing, Enter not pressed) isn't
  visible. People need to press Enter before reading; §10 item 9.)

1. Read the active tab's URL (`chrome.tabs.query({active:true,currentWindow:true})`). Extract
   `spreadsheetId` (the path segment after `/d/`) and `gid` (the `#gid=N` fragment — Sheets'
   internal numeric ID for whichever tab was open when the URL was captured).
2. Call `spreadsheets.get` with `fields=sheets.properties(sheetId,title)` to map that numeric
   `gid` to the sheet's actual name — the Sheets values API needs a name, not a raw numeric ID,
   in its A1-notation range string.
3. Default range: call `spreadsheets.values.get` with `range` set to **the sheet title alone, no
   cell span**. Confirmed via Google's API reference: a range given as just a sheet name returns
   that sheet's entire used range — the natural "sensible default" the brief asked for, without
   guessing at a fixed span like `A1:F20` that could either clip real data or pad in a lot of
   empty cells.
4. Escape hatch: let the person type an explicit range straight into the chat input (e.g. "use
   Sheet1!A1:F20"), parsed client-side and passed through verbatim as the `range` parameter —
   the natural fit for a chat-first interface; no separate range-picker UI is needed.

### Excel Online — a genuinely harder problem, not a formality

Confirmed via research: **Excel Online has no single clean "file ID in the URL" the way Sheets
does.** The URL shape actually varies by host: personal OneDrive edit URLs carry a `resid`/`cid`
query parameter (`onedrive.live.com/edit.aspx?resid=...`); OneDrive-for-Business/SharePoint URLs
instead carry a `sourcedoc` GUID (`.../Doc.aspx?sourcedoc={GUID}&file=...`); and which host a
given account even lands on depends on Microsoft's still-in-progress `cloud.microsoft` migration
(§1a). Hand-parsing three different query-string shapes per host is fragile and exactly the kind
of thing this brief warned against assuming.

**Amended Phase D session 3a (spike-verified): file resolution (steps 1–3 below) was verified on a personal account. Reading the data
(steps 4–5) is replaced by "download + parse" (B2), described right after step 5.** The
spike's personal edit URL looked like
`onedrive.live.com/personal/<cid>/_layouts/15/doc.aspx?sourcedoc={GUID}&action=edit`: no
`resid`, and the file GUID in `sourcedoc`. `/shares` resolved it with `Files.Read` alone
(200, `driveType: "personal"`). **There's no verified fallback** if `/shares` fails for some
other URL shape, and work/school URLs are unverified.

**Amended Phase D session 3a (live-verified): a second personal URL shape, which `/shares`
rejects.**
- **The URL.** Excel for the web also opens personal files at
  `excel.cloud.microsoft/open/onedrive/?docId=<id>&driveId=<id>`. For that URL,
  `/shares/u!…/driveItem` returns `400 invalidRequest "Invalid shares key."`
- **Why the ids are usable directly.** The URL-decoded `docId` is the Graph item id itself.
  For the test file it was `<driveId>!s<sourcedoc GUID without dashes>`: 50 characters, the
  same id `/shares` returned when the file was opened via `onedrive.live.com`.
- **How each shape is resolved now:**
  - this shape: `GET /drives/{driveId}/items/{itemId}`, each id encoded as one path segment;
  - `onedrive.live.com`, SharePoint and `officeapps.live.com` URLs: `/shares`, as before;
  - any other `*.cloud.microsoft` shape (e.g. `/open/sharepoint/`, or a missing
    `docId`/`driveId`): reported as unsupported with a clear message, before any token is
    requested, and never sent to `/shares`.
- **Live result:** both personal URL shapes connected to the same file.
- **Still unverified:** `officeapps.live.com` URLs through `/shares`, and every other
  `cloud.microsoft` shape. These are open items 2 and 3 in §10.

Instead: use Graph's own **`/shares/{shareIdOrEncodedSharingUrl}/driveItem`** endpoint, which
Microsoft documents specifically for resolving an arbitrary access URL to a `DriveItem` — this
sidesteps needing separate parsing logic per host entirely:
1. Base64url-encode the active tab's full URL per Graph's documented algorithm: base64-encode
   the URL, strip `=` padding, replace `/`→`_` and `+`→`-`, prefix with `u!`.
2. `GET /shares/u!<encoded>/driveItem` with header `Prefer: redeemSharingLinkIfNecessary` — the
   documented "just peek at metadata" mode, deliberately not `redeemSharingLink` (which grants
   the caller durable access as a side effect — not wanted every time someone opens the panel).
3. Response gives the `DriveItem`'s `id` and `parentReference.driveId`, usable directly in
   `/drives/{driveId}/items/{itemId}/workbook/...`.
4. **Real, flagged limitation, not silently assumed:** Graph's workbook REST API has no
   `activeWorksheet` concept — confirmed via research; that notion only exists in the in-document
   Office.js add-in API, not the external REST surface this extension calls. Unlike Sheets'
   `gid`, nothing in an Excel Online URL or in Graph tells this extension which worksheet tab was
   visually open. Default to `worksheets[0]` (first tab in position order, per `workbook/list
   worksheets`' default ordering) rather than guessing at "active," and rely on the same
   person-typed-range escape hatch as Sheets (a sheet name/range typed in chat) to override when
   the data isn't on the first tab. State this asymmetry between the two platforms explicitly
   in-product if it becomes a real point of confusion (e.g. a short note in the mapping-review
   screen naming which sheet was read).
5. Default range: call `range` (Graph's `GET .../worksheets/{name}/range`) with no `address`
   parameter — confirmed this returns the entire used range, the same "sensible default" as the
   Sheets path.

**Replacement for steps 4–5: download + parse (B2) (Amended Phase D session 3a (spike-verified)).** The Graph workbook API isn't used.
Microsoft documents that it doesn't support workbooks on consumer OneDrive (Q&A answers from
2024 and 2026 say the same). In the spike it *did* work on a personal account, even with
`Files.Read`. But being officially unsupported makes it unsafe to depend on, and it has **no
freshness advantage**: after an edit, the workbook API and the downloaded file were both stale
on the first read and both fresh at +90s. The workbook API reads the saved file, not the live
editing session. Instead:
1. `GET /drives/{driveId}/items/{itemId}?select=id,name,size,lastModifiedDateTime,eTag,@microsoft.graph.downloadUrl`
   with the `Files.Read` data token.
2. `fetch(downloadUrl)` with **no** `Authorization` header. It's a preauthenticated URL. Treat
   it as a credential: never log or store it. Graph's `/content` endpoint responds with a 302,
   which fails CORS preflight from JS, so this is the documented way for JS clients. For a
   personal account the host was `my.microsoftpersonalcontent.com` (§1a). Refuse files over a
   size cap before downloading.
3. Parse in the extension with **SheetJS 0.20.3, installed from `cdn.sheetjs.com`'s tarball**,
   not the npm registry copy, which is stuck at 0.18.5. `workbook.SheetNames` fills the sheet
   selector. This replaces step 4's blind `worksheets[0]` default: the first sheet is still
   the default, but the user can pick another. The file never goes to the backend; only the
   rows do, as today.
4. Show the file's **`lastModifiedDateTime` as "data as of last save"**, with a note that edits
   made in the last minute or two may not appear yet. In the spike, a cell edit bumped
   `lastModifiedDateTime` about 24s later and showed up in the download within 90s. A sheet
   rename took several minutes.

(Amended Phase D session 3b (live-verified, local and production): how B2 is actually built.
- **Step 1 requests the whole DriveItem, with no `$select`.** The first 3b build sent
  `?$select=id,name,size,lastModifiedDateTime,eTag,@microsoft.graph.downloadUrl`. Graph
  returned the item **without** `@microsoft.graph.downloadUrl`, on both URL shapes. The spike's
  working request (step 1 above, as written) used `select=` **without the `$`**. That isn't a
  query option on Graph v1.0, so the spike was really getting the whole item, which includes
  the download URL by default. That part is inferred: what was observed is that `$select`
  dropped the URL and the whole item carries it. Live result with the whole item: driveItem
  200, then the download from `download.aspx` on `my.microsoftpersonalcontent.com`, on both
  URL shapes.
- **Fallback: `/content`.** If the item still has no download URL, the extension requests
  `.../driveItem/content` (or `/drives/{d}/items/{i}/content`) with the token and lets `fetch`
  follow the 302. The CORS concern in step 2 doesn't apply to an extension page whose host
  permissions cover both hops (`graph.microsoft.com`, `my.microsoftpersonalcontent.com`). From
  Chrome 119, `fetch` drops `Authorization` on a cross-origin redirect, so the token never
  reaches the download host. The manifest therefore sets **`minimum_chrome_version: "119"`**,
  which is also above the side panel's 114 floor. The fallback is unit-tested, but it didn't
  run live, because the primary path worked.
- **Neither URL is kept.** The download URL and the redirect target are never logged, stored
  or shown. Only the workbook bytes stay in panel memory, so switching sheets re-parses them
  without downloading again (confirmed in the Network tab).
- **SheetJS ships with the extension.** It's pinned at install time to the `cdn.sheetjs.com`
  0.20.3 tarball, because MV3 forbids loading remote code at runtime. It's loaded with a
  dynamic `import()` as a separate ~492 kB chunk, only when an Excel file is read (confirmed
  live). Dates use `SSF.parse_date_code` with the workbook's 1900/1904 setting, and hidden rows
  need `cellStyles: true`.
- **Save lag (step 4).** Twice in the 3b live tests, edits took about **2 minutes** to reach the
  downloaded file. The "data as of last save" line and its "last minute or two" note are what
  tell people about it.)

---

## 5. Data format contract compliance — both APIs' real response shapes

(Amended Phase D session 3b (live-verified, local and production): **both open questions below
are closed**, and the contract was amended to match (`backend/EXTENSION_INTEGRATION.md` §6). The
Sheets `FORMATTED_VALUE` bullet and the Excel `cell.w` approach below are superseded.
- **Q1, dates: converted to ISO-8601 in the extension, on both platforms.** Display strings
  were unsafe: the backend parses `01/02/2025` month-first, so a non-US date would silently
  become the wrong date. Mixed formats in one column dropped rows, and SheetJS renders Excel's
  Short Date as `3/31/25`.
  - Google: a cell whose format type is `DATE`/`DATE_TIME` is converted from its serial
    (counted from 1899-12-30, in UTC).
  - Excel: a numeric cell whose number format is a date format is converted with
    `SSF.parse_date_code`, which honors the 1900/1904 setting and uses no JS `Date`.
  - A date with a time is sent as `YYYY-MM-DDTHH:MM:SS`.
  - Text that only looks like a date is sent as shown, and the preview names those cells.
  - Live: Excel's built-in Short Date (numFmt 14) and Google dates both arrived as
    `2025-03-31`.
- **Q2, numbers: the underlying value, sent as `String(value)`** (plain decimal, with exponent
  form at the extremes such as `1e+21`, which the backend parses).
  - Display strings can hide scale (`#,##0,` shows 1,250,000 as `1,250`) and round.
    `61.5%` doesn't parse at all.
  - Live: `0.6145038167938931` (shown as `61.5%`), `1250000` (shown as `$1,250,000`), and
    `-45000` (shown as `($45,000)`), on both platforms. Formula cells send their cached
    results.
- **Google's numeric rule** (live finding): plain, unformatted numbers have **no**
  `numberFormat` at all, and their `formattedValue` is truncated (`0.6145038168`).
  - So a cell is numeric when `effectiveValue.numberValue` is present and the format type
    isn't `DATE`/`DATE_TIME`/`TIME`.
  - `NUMBER`/`CURRENCY`/`PERCENT` are never consulted. The format type decides only
    `DATE`/`DATE_TIME` (sent as ISO) and `TIME` (sent as shown).
  - Text, booleans and error literals are sent as shown.
- **The preview shows exactly what's sent**, labelled with the sheet's row numbers and column
  letters. Each cell's tooltip gives the address and the displayed value, for example
  `B4 — shown as $1,250,000`. Notices name the cells concerned: hidden rows, merges, formula
  errors, date-looking text, periods across columns, and a trimmed range.
- **Shape:** both readers pad ragged rows and trim only *trailing* empty rows and columns, so
  the rows sent are one contiguous rectangle. That rectangle's `source.range` lets the backend
  cite figures as cells, e.g. `'P&L'!B4`.)

`sheets-backend-design.md` §1's contract is unchanged and non-negotiable: `POST /v1/csv/parse`
needs `rows` as a list of list of **display strings** — dates included — never a raw typed value
or numeric serial. Confirmed exactly how each platform's real API satisfies this:

- **Sheets:** rely on `spreadsheets.values.get`'s **default** `valueRenderOption`, which is
  `FORMATTED_VALUE` (confirmed via Google's API reference) — this returns every cell, including
  dates, as the string it displays in the Sheets UI. The adapter must simply *not* override this
  to `UNFORMATTED_VALUE` (which would return a raw numeric date serial) — the contract is
  satisfied by doing nothing, not by extra client-side logic.
- **Excel (Amended Phase D session 3a (spike-verified)):** because of B2 (§4), this path doesn't read the Graph `Range` resource. The
  display string for each cell is SheetJS's rendered text (`cell.w`, via `sheet_to_json(ws,
  {header: 1, raw: false, defval: ""})`). Spike result: 29 of 33 checks matched exactly
  (currency, 1-decimal percent, a parenthesized negative, custom `d-mmm-yy` dates, and text).
  **The 4 misses were all Excel's built-in Short Date format: SheetJS renders `3/31/25` where
  Excel shows `3/31/2025`.** Formula cells carry cached values, so no recalculation is
  needed. Two things are **open for session 3b and not decided here**:
  1. Should dates be normalized to ISO-8601 on *both* platforms? Both the Short Date gap and
     Google's locale-dependent `FORMATTED_VALUE` strings argue for it.
  2. Should numbers be sent as underlying values or display strings? This matters for
     traceability: `0.6145…` displays as `61.5%`.

  The rest of this bullet describes the Graph `Range` path, which is no longer used.
- *(Superseded for the reason above.)* **Excel:** the workbook `Range` resource's `values` property holds the *raw* typed value (a
  date comes back as a number, since Excel's `valueTypes` classifies a date cell as `Double`) —
  confirmed via the Graph resource reference. The `text` property instead holds the *displayed*
  string for every cell, explicitly documented as matching the Excel UI regardless of column
  width. **The adapter must read `text`, never `values`**, for exactly the same reason the
  Sheets adapter must rely on `FORMATTED_VALUE` rather than `UNFORMATTED_VALUE` — this is the
  Excel-side equivalent of the same contract, not a separate concern.
- **Shape normalization, once both are string-cast:** Sheets' name-only `values.get` can return
  *ragged* rows (trailing empty cells are dropped per-row, not padded) — the adapter must pad
  every row to the response's max column count with empty strings before handing rows to
  `/v1/csv/parse`. Excel's `range.text` by contrast always returns a full rectangular grid for
  the addressed range (blank cells included as `""`) — no padding needed there, a real asymmetry
  worth noting rather than writing one shared padding step and assuming it's a no-op for Excel.

---

## 6. Full auth flow wiring

Mechanically, this is `EXTENSION_INTEGRATION.md`'s already-fully-specified contract, wired
end-to-end using §2's token-acquisition mechanism:

1. `launchAuthFlow(provider)` (§2) → for Microsoft, an OAuth `oauth_token` (identity scope
   `User.Read` only); for Google, the raw `code` + `code_verifier` + `redirect_uri`.
   (Amended Phase D session 3a (spike-verified): the earlier text said `oauth_token` for both providers, which has been stale since the
   session 2 amendment in §2.)
2. `POST /v1/auth/exchange` with `{provider, oauth_token}` (Microsoft) or `{provider, code,
   code_verifier, redirect_uri}` (Google) → `{session_token, account_id, expires_at}`
   (contract §1). Handle both documented error cases in the sign-in UI: `401`
   (token rejected/unverified email) and `422` (Microsoft account has no usable email — contract
   §1's specific `mail`/`userPrincipalName` fallback case).
3. **Store `session_token` in `chrome.storage.local` — never `.sync`** — carried over verbatim
   from `oauth-identity-session-design.md` §2: `.sync` rides Chrome's own browser-account sync,
   a different and inapplicable guarantee from this backend's own cross-device account
   resolution (a second device just re-runs step 1–2 against its own OAuth grant and resolves
   back to the same `account_id` server-side, per the contract's cross-provider identity note —
   the token itself never needs to travel between devices).
3a. **Provider data tokens (Amended Phase D session 3a (spike-verified)) go in `chrome.storage.session` only**, never `.local` or
   `.sync`, and never together with `session_token`. A Microsoft `refresh_token` is thrown away
   on receipt. See §2 "Data-access tokens". Sign-out, revoke-all, and any `401` also clear the
   stored data tokens.
   (Amended Phase D session 3a (live-verified): the **data-account email** per provider goes in
   `chrome.storage.local` (`fa_data_accounts`), so silent re-auth works after a browser
   restart. It isn't a credential.
   - Sign-out, revoke-all, any `401` and every successful sign-in clear the tokens and the
     emails together. `clearStoredSession()` is the single place that does it, so no future
     path that ends the session can leave data-access state behind.
   - Live-verified for sign-out, Sign out everywhere and a forced `401` (a `curl` logout of a
     copied session token): all three cleared `fa_session`, `fa_data_tokens` and
     `fa_data_accounts`, with no OAuth window.)
4. Every subsequent request: `Authorization: Bearer <session_token>` header. Never send
   `X-Install-Id` — it does nothing server-side anymore (contract §1 step 4).
5. **Any `401` — indistinguishably expired, revoked, or never valid, by the backend's own
   anti-enumeration design (contract §3) — is handled identically: discard the stored token and
   show the signed-out UI.** "Re-run step 1" means *offering* sign-in again (the same static
   buttons a fresh load shows) and waiting for an explicit click — **never** calling
   `launchAuthFlow` automatically. Do not attempt to branch on cause; the contract is explicit
   that no such signal exists or is ever planned. (Amended Phase D session 2: an earlier revision
   of this section had revoke-all auto-relaunch sign-in, below — live testing showed this
   produces a surprise OAuth popup with no user action, and per the same principle, no code path
   may ever call `launchAuthFlow` except a direct click on a sign-in button.)
   (Amended Phase D session 3a (spike-verified): the same rule, widened to cover data grants. **No `launchWebAuthFlow` call of any
   kind, silent or interactive, starts except inside the handler for a direct user click**:
   a sign-in button, or a "read this sheet" style action. A silent data-token attempt counts
   as part of that click, never as a background refresh. Nothing re-acquires tokens on a
   timer, on panel open, or after an error.)
   (Amended Phase D session 3a (live-verified): **Chrome doesn't enforce this.**
   `launchWebAuthFlow({interactive: true})` opened a window with no user gesture at all (§2).
   The rule is enforced only by `getDataToken` refusing any click event that isn't
   `isTrusted`, before it touches `browser.identity`, plus unit tests that no other code path
   reaches it. Any new code that obtains a provider token must go through that check.)
6. Sign-out UI: `POST /v1/auth/logout` (this device only) and `POST /v1/auth/sessions/revoke-all`
   ("sign out everywhere" / compromised-credential case) — both take no body, both return
   `{"revoked": true}` unconditionally (contract §4). Neither ever calls `launchAuthFlow` — both
   end at the signed-out UI, same as step 5's 401 handling. `revoke-all` specifically: on success
   (or a `401`, meaning the token was already invalid), clear the stored session and show
   signed-out; on any other failure, the outcome for other devices is genuinely unknown, so keep
   the local session rather than discarding state that may still be valid, and say so plainly
   rather than failing silently.

---

## 7. Build tooling: WXT + vanilla TypeScript

Confirmed via research into the current (2026) Manifest V3 tooling landscape: three real
contenders — Plasmo, CRXJS, and WXT.
- **Plasmo**: batteries-included, React-oriented, file-based routing — but its core release
  cadence has stalled (last core release well over a year old at time of research), a real
  maintenance-risk signal for a new project to build on today.
- **CRXJS**: a minimal Vite plugin, not a framework — reads a hand-written `manifest.json` and
  wires Vite to it. Its own documentation-derived comparison notes it provides no abstractions
  for messaging/storage/multi-surface routing, and its own development pace has slowed relative
  to WXT.
- **WXT**: the current consensus choice — actively released on a weekly cadence, framework
  chosen for `entrypoints/sidepanel/` file-based scaffolding (side panel is a first-class,
  documented entrypoint type, not something to hand-configure), generates the manifest rather
  than requiring one hand-written, and supports vanilla TypeScript with no UI framework
  requirement.

**Recommendation: WXT, vanilla TypeScript, no UI framework (no React/Vue/Svelte).** The
reasoned trade-off the brief asked for stated plainly: this extension's actual UI surface is two
screens — a scrolling chat view and a mapping-confirmation table — not a complex app with
routing, nested state, or a large component tree. A UI framework's value (component reuse,
declarative re-renders across many interacting views) doesn't pay for itself at this scope, and
skipping it avoids a real dependency and bundle-size cost for no functional gain. WXT itself is
still the right call over hand-rolling Vite+CRXJS config, independent of the framework
question — it's the build tool, not a UI framework, and its manifest generation, side-panel
entrypoint convention, and active maintenance are worth taking regardless of which UI approach
sits on top. Revisit only if the chat view later grows real complexity (streaming markdown
rendering, virtualized long histories) that starts to make manual DOM updates genuinely painful —
not assumed as a certainty today.

---

## 8. Explicit out of scope

Named plainly, matching this project's own design-doc convention:
- Chrome Web Store submission, listing, and review-process requirements.
- The privacy policy / terms of service documents — separate, later work per this project's own
  prior decision.
- Desktop Excel (the standalone Windows/Mac application) — still explicitly out of scope,
  unchanged from earlier in this project.
- Firefox/Safari support — `chrome.sidePanel`/`chrome.identity` are the Chrome (and
  Chromium-derived, e.g. Edge) surface; Firefox's differently-shaped `sidebar_action` API is not
  designed for here.
- A Drive-backed "browse all my Sheets" file picker, and any SharePoint site/library browsing UI
  beyond resolving whatever URL the person already has open — neither scope (§3) nor the file
  resolution mechanism (§4) is built to support either.
- Any change to the backend contract itself — this document treats
  `backend/EXTENSION_INTEGRATION.md` as fixed and builds only the client side against it.
  **(Amended Phase D session 3a (spike-verified): explicitly relaxed for exactly one addition, `POST /v1/google/data-token`**
  (`EXTENSION_INTEGRATION.md` §1a). Google's "Web application" client is confidential, so a
  Google data grant can't be exchanged client-side (§2). Without this endpoint the extension
  couldn't read Sheets at all. Every other part of the contract stays fixed.)

---

## 9. Session breakdown

Paired per the "built together, not staged" decision — most sessions touch both platforms'
version of that session's layer, rather than finishing Sheets end-to-end before starting Excel.

1. **Extension skeleton.** WXT scaffold (vanilla TS), manifest fields (`side_panel`,
   `"sidePanel"`/`"identity"`/`"storage"`/`"tabs"` permissions, host permissions from §1a), the
   side-panel shell with per-tab enable/disable wiring (§1). No auth yet — static "Sign in with
   Google" / "Sign in with Microsoft" buttons only.
2. **OAuth for both providers.** `launchWebAuthFlow` + PKCE for Google and Microsoft (§2), the
   shared auth module, full wiring through §6 (`/v1/auth/exchange`, `chrome.storage.local`,
   `Authorization` header, uniform 401→re-sign-in handling, logout/revoke-all buttons).
3. *(Amended Phase D session 3a (spike-verified): split into 3a and 3b. The spike came first; its results are in `docs/spikes/session3a-auth-file-access.md`.)*
   - **3a. Identity/data-token architecture.** *(Implemented and live-tested against the local
     backend. Results are recorded in the amendments to §2, §3, §4 and §6; the open items it
     left are listed in §10.)*
     - Sign-in scopes reduced to identity only: Google `openid email`, Microsoft `User.Read`.
       No `Files.ReadWrite`.
     - A `getDataToken(provider)` module: cache, then silent, then interactive, inside a click
       (§2 "Data-access tokens").
     - Backend `POST /v1/google/data-token`, with tests: session required, code exchange
       without `access_type=offline`, the data account's `email` returned, no check against
       the sign-in identity, nothing stored or logged.
     - Microsoft client-side data exchange that throws away the `refresh_token`.
     - `chrome.storage.session` token storage, cleared on sign-out, revoke-all and `401`.
     - Manifest host-permission updates (§1a).
     - The widened click rule (§6 step 5).
   - **3b. File/range adapters and range UI.** *(Done. Live-tested locally and against
     production on Google Sheets and on Excel through both personal URL shapes. It ends at a
     successful `/v1/csv/parse` with a summary. How it was built is recorded in the 3b
     amendments to §4 and §5 and in `EXTENSION_INTEGRATION.md` §6. The open items it left
     are §10 items 6–9; it also settled item 4.)*
     - Sheets: URL parse (live `gid`, `range=` pre-fill), then `spreadsheets.get`, then
       `spreadsheets.values.get`.
     - Excel: `/shares`, then item metadata, then `downloadUrl` fetch, then SheetJS 0.20.3
       (§4 B2).
     - Sheet selector plus range field, and a "data as of last save" timestamp for Excel.
     - Both normalized into one rows shape (§5), including padding for ragged rows, and wired
       into `POST /v1/csv/parse`.
     - Decide §5's two open questions first: ISO date normalization, and values vs display
       strings.
4. *(Done. Live-tested locally on Google Sheets and on Excel through both personal URL shapes,
   then in production (deploy `011226e`) on Google Sheets and on Excel through
   `excel.cloud.microsoft`. Contract changes are in `EXTENSION_INTEGRATION.md` §6, amended
   session 4; it closed §10 items 6 and 8.)*
   - **Flow:** Send to analysis goes straight into a mapping screen: parse, then a suggested
     mapping, one role menu per column, then confirm.
   - **No silent defaults:** Confirm stays disabled until a period column, a revenue column, a
     business name and a **scale** are chosen. The scale menu starts on "Choose…", and currency
     starts on "Not specified".
   - **Failures:** a failed or capped suggestion (429/502/500) leaves every column for the
     person to map. An expired context (404) returns to the preview.
   - **Immutable statements:** a confirmed statement can't be changed (409). Change mapping
     re-sends the same rows as a new statement.
   - **Active statement:** kept in `chrome.storage.local` (`fa_active_statement`, an id rather
     than a credential) and shown as a card. It's restored after a panel reopen and a Chrome
     restart, and cleared with the session (verified with a forced `401` mid-edit).
   - **Proposal cap, verified in production:** each fresh proposal wrote exactly one
     `mapping_proposal` usage event (3 proposals, 3 events). A repeat `propose-mapping` call on
     an unconfirmed context returned the stored proposal with no new event.
   - **Not live-tested; covered by unit tests:** re-confirm `409`, and a bad API key's `502`
     falling back to manual mapping.
   - **The backend side needed migration 0004** (`usage_event_outcome` value
     `mapping_proposal`). Render never runs migrations, so it was applied by hand before the
     push, per `backend/DEPLOYMENT.md` "Database migrations".

   Original plan: **Mapping-confirmation screen.** Side-panel view swap from chat to a review table:
   `POST /v1/csv/{id}/propose-mapping`'s proposal rendered editable, then
   `POST /v1/csv/{id}/confirm` — per the contract, this proposal is never auto-accepted.
5. **Chat interface.** Message list, `POST /v1/ask` wiring with `conversation_id` continuity,
   rendering `final_answer`/`figure_check`/`hit_iteration_cap`, and the documented error-type
   handling (404 on a stale/foreign `conversation_id`/`csv_context_id`, 429 cap responses, 502/500
   generic-message handling) — all per contract §6, nothing invented beyond what it specifies.
   *(Done. Amended Phase D session 5, verified locally and in production, commit `aea7b33`: planning found that the contract
   needed changes before a chat panel could be correct. They're specified in
   `EXTENSION_INTEGRATION.md` §6 `/v1/ask`, amended session 5:*
   - *Citations: each figure links to its cell, or to the cells and formula behind a derived
     value. A figure that matches several cells shows all of them.*
   - *Binding: a conversation stays bound to its statement, with a `409` instead of a silent
     switch.*
   - *Legacy statements: they're rejected with a request to re-confirm.*
   - *Replay: a `request_id` makes a slow answer recoverable after the panel closes, and never
     charges a question twice.*
   - *The extension requires a confirmed statement before it offers chat. It keeps the
     conversation and pending question in `chrome.storage.session`.*
   - *Click-to-cell navigation is deferred, because neither platform's cell-selection URL
     behavior is verified.*
   - *Bug found and fixed while adding growth-rate citations: `get_csv_ratios` crashed
     (`IndexError`) on a statement with more than 8 quarters. Its default window is the last 8
     rows, and that tail kept its original row labels, which the growth lookup then used as
     positions. The model saw a crashed tool instead of a growth rate. Fixed by resetting the
     window's index, with a regression test (`tests/test_tools.py`).*
   - ***Done.** Verified locally (2026-10-05, local backend, shared production database), then
     in production: commit `aea7b33`, deployed on Render 2026-10-06, `/v1/health` returned
     `{"status":"ok","db":"ok","commit":"aea7b33c7aef255dfc5b852e2e6c0a53fef536ce"}`, with the
     production build of the extension.*
     - *Production checks, all passed:*
       1. *Formatted ratios (62.0%, 13.6%, -4.6%) traced as derived, with their formula and
          cells. Answers name the data as "your sheet 'P&L', A3:G7", never "uploaded CSV".
          Extreme values keep their format too: -38,449.6% traced to its formula and cells.*
       2. *Ambiguous: 1.25 million lists both 'P&L'!B4 and 'P&L'!B6.*
       3. *Scale and sheet name: 'P&L (000s)'!B5 = 1310 (as read, in thousands) gives
          $1,310,000. "P&L (000s)" stays intact, with no false "000" figure.*
       4. *Cross-window: a question asked in window 1 showed "An answer is being prepared in
          another window" in window 2, with Check again and Discard, and Confirm blocked. The
          block lifted and the answer appeared in both. This confirms that
          `storage.onChanged` fires for the session area in side panels.*
       5. *Panel closed about 5 s into an answer and reopened about 60 s later: the answer
          appeared. Render's logs show one run (iteration 1: 5751 ms, `tool_use`; iteration 2:
          11499 ms, `end_turn`). The replay POST line wasn't captured in the production logs;
          the earlier local run showed the replay POST returning 200 with no new model calls.*
       6. *The logs carry metadata only (iteration, `duration_ms`, stop reason, model). The
          largest model call seen was 12.1 s, well under the 120 s timeout.*
     - *Local checks, also verified* (closing the panel mid-answer gave one model run, 2.7 s
       and 12.1 s, recovered by replay with the same `request_id`):
       - *a direct value, cited to its cell;*
       - *derived values: a margin, and a growth rate with its prior-period cell;*
       - *a figure from an earlier turn ("from an earlier answer");*
       - *a new confirm starts a new conversation;*
       - *a statement mismatch on reopen shows the New conversation offer and charges nothing.*
     - *The five findings from the first local live test, re-checked locally before the push:*
       1. *Ranges ("61–62%"): only the second end carried the unit, so the first was wrongly
          marked "Not traced". **Unit-tested only**: the model didn't write a range during
          the live checks, so it wasn't observed live.*
       2. *Sheet names: digits in a name like "P&L (000s)" were read as figures. Verified
          fixed, locally and in production (check 3).*
       3. *Ratios were quoted as raw floats. Verified fixed: they're quoted as formatted
          percentages (check 1).*
       4. *Other windows: a statement confirmed in one window didn't update another window's
          panel. Verified fixed: other panels update without reopening, and the pending block
          holds across windows (check 4).*
       5. *Labels: two statements from the same range looked identical, and the name was
          repeated ("FA Spike Test — FA Spike Test"). Verified fixed: labels carry the scale
          and confirmed time, without the repeat.*
     - *Also verified:* data from a sheet is described as "your sheet", not an "uploaded
       CSV".)*
6. **Usage and billing surfacing.** `GET /v1/usage` display; the 429 body's
   `prompt_byo_key`/`prompt_upgrade` fields driving which upsell to show;
   `POST /v1/billing/checkout-session` (open the returned URL in a new tab, and don't assume the
   account's tier updated on return, per contract §6's own stated webhook-latency caveat);
   `POST /v1/byo-key` registration form.
7. **Polish + manual QA pass** across both OAuth providers and both spreadsheet platforms
   end-to-end (sign-in → file read → mapping confirm → ask → usage/billing surfacing). No new
   endpoints or scopes — a verification session, not a feature one.

---

## 10. Open items (from Phase D sessions 3a, 3b and 4)

Recorded so they aren't rediscovered later. Items 1–5 come from 3a, and none of them blocked 3b.
Items 6–9 come from 3b; session 4 closed 6 and 8. Items 10–12 come from session 4.

1. **Verify Microsoft identity with a validated ID token, not a Graph access token.** Today
   `/v1/auth/exchange` verifies Microsoft sign-in by calling Graph `/me` with an access token.
   Microsoft carries every consented Graph scope into that token, so after the first data grant
   the backend receives a `Files.Read`-capable token at every sign-in (§2 item 1, live-verified).
   - The fix: request `openid` and send the backend the ID token instead. The backend would
     then validate the ID token's signature, issuer, audience and expiry against Microsoft's
     published keys, and take `oid`/`sub` plus `email`/`preferred_username` from its claims.
   - The backend would then never hold a Graph token.
   - It has to keep `oauth-identity-session-design.md`'s stable-subject and email-fallback
     rules, and those rules' anti-takeover reasoning (`SECURITY.md` §7).
2. **`officeapps.live.com` URLs still resolve through `/shares`, unverified.** No live test
   has opened a file on that host (§4).
3. **`excel.cloud.microsoft` shapes other than `/open/onedrive/` are unverified**, for example
   work/school or SharePoint files. They're reported as unsupported for now rather than sent to
   `/shares`, which rejected the one cloud.microsoft shape tested (§4).
4. **3b idea: remember the data account per file, not one per provider.** Today the extension
   keeps one data-account email per provider. A person who reads Sheets from two Google
   accounts will hit "can't open this file", and then the chooser, each time they switch.
   Keying the remembered account by spreadsheet/drive item would let each file re-authenticate
   silently as the account that last opened it.
   - *Decided in 3b: still one data account per provider.* Per-file storage would need three
     things:
     - a key from the URL, since Graph's item id is only known after a token is obtained;
     - a token cache keyed by provider and email;
     - a capped list of opened file ids in `chrome.storage.local`.
   - That list is a disk-backed history of which files the person opened. Today the friction
     is one account chooser when switching accounts, which is recoverable. Revisit in
     session 7 only if live use shows it actually happens.
5. **Google "Testing" publishing mode limits data access to listed test users.** The live test
   needed a second Google account added as a test user before it could grant Sheets access.
   `spreadsheets.readonly` is a sensitive scope, so **Google's OAuth app verification is needed
   before public launch**. Until then, only listed test users can connect Sheets.
6. **A mapped numeric cell that can't be parsed becomes "no value" silently (session 4).**
   - `normalize`'s numeric cleaner turns cells like these into `None` without a warning:
     - `61.5%` typed as text;
     - `#DIV/0!`;
     - `€1,250`;
     - `1 250 000`.
   - The read panel already flags error cells, but the confirm step should warn when a cell
     in a *mapped* column comes back empty after parsing.
   - **Closed in session 4: a two-step acknowledgement.**
     - **What's reported:** `/confirm` lists every non-blank mapped cell that doesn't parse, by
       address (`find_unparsed_cells`). Blank cells count as "not reported" and get only a
       warning line.
     - **How it's refused:** `/confirm` refuses until the person acknowledges that exact list.
       The extension sends `accept_unparsed_cells` plus an `ack_fingerprint` over the mapping,
       the scale and the cells. Any change produces a new list and a new fingerprint.
     - **Live result:** the acknowledgement listed `'P&L'!B6 "61.5%"` (Revenue, 2025-09-30)
       and `'P&L'!F5 "#DIV/0!"` (Net income, 2025-06-30). "Confirm anyway" confirmed the
       statement, and the card showed "No value for: …".
7. **Layouts with periods across columns aren't supported.** P&L sheets often put the dates in
   the header row and the line items down the rows. `normalize` needs one period per row. The
   preview warns when the header looks like dates, but nothing transposes the grid. If
   transposing is added, `source` needs an orientation flag so cell citations stay right.
8. **Units and scale aren't captured anywhere.** A title row saying "in thousands" or "(USD)"
   is lost. Sending underlying values fixes display scaling (`#,##0,`), but not a sheet whose
   numbers were *typed* in thousands. A typed `1,250` meaning $1.25M is stored as 1250. This
   needs unit metadata on the source or the mapping.
   - **Closed in session 4: a scale chosen at confirm, applied in Python.**
     - **The choice:** the person picks the scale (ones, thousands, millions or billions) with
       **no default**, and Confirm is disabled until one is chosen. Currency is an optional
       label that starts on "Not specified".
     - **Where it's applied:** `normalize` converts every mapped value to ones with exact
       `Decimal` arithmetic and records `scale` and `currency` in `csv_source`. The tools
       report values in ones, with a `units` object and `sheet_scale` on each citation, so
       the model never multiplies and `check_figures` needs no change.
     - **Live result (read-only DB check):** for the same cell, `'P&L'!B4` (1,250,000),
       revenue for 2025-03-31 was stored as `1250000` with scale ones and `1250000000` with
       scale thousands. Both cite `source_cell 'P&L'!B4`.
     - **A deliberate behavior change, not a bug:** a statement with no stated currency is
       reported **without any currency symbol**. That includes Streamlit CSV uploads, which
       used to get "$" (`NOTES.md`, `CLAUDE.md`).
     - Not done: detecting a scale from a title row. Title rows aren't in the range that's
       sent.
9. **Google mid-edit freshness: "press Enter before reading."** A cell still being typed into
   isn't visible to the API. Committed edits are immediate, including recalculated formulas.
   The panel doesn't say this yet. A short hint near Preview (session 7 polish) would cover
   it.

**From Phase D session 4** (items 10–12; details in `NOTES.md`):

10. **Session 7 polish for the mapping screen and statement card**, from the live test:
    - ~~The card repeats the name when the business and file names match ("FA Spike Test —
      FA Spike Test").~~ Done in session 5 (statement labels).
    - Show the period count ("4 quarterly periods").
    - Unmapped-concept warnings use internal names (`operating_cash_flow`). Use friendly
      labels, grouped on one line.
    - Change mapping's editor has no suggestion, so it has no Reset button. Offer "Reset to
      confirmed mapping".
    - Strip the file extension from the pre-filled business name.
    - A restored card has no actions. Add "Read another range".
    - Add a test that changing the sheet hides Send and clears the preview. Only the range
      edit is tested today.
    - The preview shows only the first 10 rows. Consider a "Show all rows" toggle.
    - The disabled mint button looks murky in dark mode, because it's opacity-based. Consider
      a dedicated disabled color token.
    - Optional: a manual light/dark/system theme switch. Today the panel follows the system
      setting only.
    - **Before the session 6 Stripe work:** delete the `manual_test` `subscriptions` row
      that raises the owner's question cap for session 5 live testing. It exists, for account
      `36f87f76-1ff4-4a13-809d-2d54d23b5745` (`backend/DEPLOYMENT.md`).
    - Answer markdown shows as raw text (`**`, backticks, tables). Consider a safe formatter
      (bold, code, lists, tables) built with createElement, or plain-text answers.
    - The source list repeats the same figure once per mention. Group it by figure and
      provenance.
    - Make it clearer which file and tab the chat is about. The chat follows the active
      statement, even while another document is open.
    - A newly added tab appeared only after reconnecting. Refresh the sheet list.
    - Optional: a soft warning when the chosen scale gives implausible values (e.g. revenue in
      billions from a small sheet).
    - From the session 5 production run:
      - When another window starts a new conversation, show a short notice instead of the
        transcript silently disappearing.
      - The model sometimes writes loose estimates ("low-60s", "roughly 380x"). They're
        correctly flagged "Not traced"; consider a prompt nudge to avoid them.
      - The answer sometimes puts a percentage in quotes ("-4.6%"). Minor prompt polish.
11. **Legacy confirmed `csv_statements` rows** with NULL `confirmed_at` and no
    `statement_attrs` exist in production, predating the current confirm code. One belongs to
    account `b90f9f33…`. In session 5, check whether `/v1/ask` handles them
    (`statement_from_records` reads the attrs), and decide on cleanup.
    - **Session 5 counts** (read-only check, 2026-10-04, before migration 0005):
      - 13 confirmed statements in total.
      - 1 with NULL `confirmed_at`, 1 with NULL `statement_attrs`, 1 with NULL
        `statement_data`, and 2 whose attrs have no `scale`.
      - 3 accounts affected. No conversation points at any of them.
    - **What `/v1/ask` did with them before session 5** (code-verified):
      - NULL `statement_attrs` crashed with a bare 500, after the question had already been
        counted.
      - Attrs without `scale` were silently reported as "ones".
    - **Decided in session 5: reject, never backfill.** Such a statement gets
      `409 statement_needs_reconfirm` (`EXTENSION_INTEGRATION.md` §6, amended session 5),
      checked before the question is counted. The rows stay as they are.
12. **Newest-first sorts must put NULLs last.** Postgres sorts NULLs first in `DESC`, so
    `ORDER BY confirmed_at DESC LIMIT 1` picked one of the rows in item 11, from another
    account, during a read-only check. Audit every app query that picks "the latest" row; use
    `DESC NULLS LAST` or filter `IS NOT NULL`.

**From Phase D session 5** (backlog, not session 5 work):

13. **8 leftover test accounts in production** (`sub-*@example.com`, created 2026-09-19).
    They predate session 5: the DB-backed test run on 2026-10-05 left the count unchanged
    before and after, so they come from an earlier run whose teardown didn't complete. Delete
    them in a future, separately approved cleanup. Deleting an account cascades to all of its
    rows.

**Migrations are manual** (session 4): Render never runs them. Its Pre-Deploy command isn't
available on the current plan. Run an additive migration with `alembic upgrade head` against
production before pushing the code that needs it, verify the change, and record it. The local
`.env` points at production, so any local `alembic` command changes production immediately.
See `backend/DEPLOYMENT.md` "Database migrations".

Related, already tracked elsewhere: live-testing against the local backend writes to the shared
production database, because there's no separate dev/test database (`backend/DEPLOYMENT.md`,
"Known risk / backlog"). In the 3a test run, a sign-in as the second Google account created a
real, separate account there.

---

## Verification

This produces a design document, not code.
- `backend/EXTENSION_INTEGRATION.md`, `oauth-identity-session-design.md` §2, and
  `sheets-backend-design.md` §1 were all read in full this session and are carried forward
  verbatim where they impose a real requirement (§6's storage rule, §5's date-string contract).
- Every numbered unknown in the brief (Manifest V3 shell choice, per-provider OAuth mechanism,
  per-provider data-access scopes, per-platform file/range identification, per-platform data
  format compliance, and build tooling) was resolved via fresh web search this session and is
  cited below — not assumed from training data, per this project's explicit instruction and its
  own prior history of being burned by exactly that (the Basil API version change, the
  userinfo-vs-tokeninfo distinction).
- Two corrections to assumptions embedded in the original brief are called out explicitly rather
  than silently folded in: no Drive scope is needed for Google (§3), and Files.ReadWrite alone —
  not a separate Sites.Read.All — covers both OneDrive and SharePoint-hosted Excel files (§3),
  because file resolution goes through Graph's generic `/shares` endpoint rather than a
  site/drive-enumeration path.
- One real, unavoidable trade-off is flagged plainly rather than glossed over: Microsoft Graph's
  workbook API has no read-only permission floor, so this extension must request `Files.ReadWrite`
  even though it never writes (§3). (Amended Phase D session 3a (spike-verified): no longer true. B2 doesn't use the workbook API, and
  `Files.Read` was verified sufficient on a personal account, so the extension never requests
  write access. See §3 and `docs/spikes/session3a-auth-file-access.md`.)
- One implementation-time detail is explicitly flagged as unverified rather than guessed: the
  exact Google Cloud Console OAuth client type to register for `launchWebAuthFlow` (§2) — a
  console-UI detail more prone to drift than the protocol behavior surrounding it. (Resolved
  Phase D session 2: a "Web application" client, which is confidential — see §2's amendment.)

### Sources
- [browser.sidePanel | Chrome for Developers](https://developer.chrome.com/docs/extensions/reference/api/sidePanel) — stable since Chrome 114, MV3 shape, per-tab options.
- [browser.identity | Chrome for Developers](https://developer.chrome.com/docs/extensions/reference/api/identity) — `getAuthToken`'s browser-profile account binding; manifest `oauth2` requirements; `launchWebAuthFlow`/`getRedirectURL` mechanics.
- Chrome extensions Google group / community sources on `getAuthToken` vs. `launchWebAuthFlow`'s account-chooser behavior (browser-profile-bound vs. explicit account picker).
- [Microsoft identity platform and OAuth 2.0 authorization code flow | Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) — current PKCE-for-public-clients guidance.
- [Choose Google Sheets API scopes | Google for Developers](https://developers.google.com/workspace/sheets/api/scopes) — exact `spreadsheets`/`spreadsheets.readonly` scope strings and Drive-scope guidance.
- [spreadsheets.values.get | Google for Developers](https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values/get) — `FORMATTED_VALUE` default; sheet-name-only range behavior.
- [Microsoft Graph permissions reference | Microsoft Learn](https://learn.microsoft.com/en-us/graph/permissions-reference) — `Files.Read`/`Files.Read.All`/`Sites.Read.All` delegated-permission shapes.
- [Access shared items (shares-get) | Microsoft Graph v1.0](https://learn.microsoft.com/en-us/graph/api/shares-get?view=graph-rest-1.0) — URL-to-`DriveItem` resolution mechanism, encoding algorithm, `Files.ReadWrite` least-privileged permission table, `redeemSharingLinkIfNecessary`.
- [Worksheet: range | Microsoft Graph v1.0](https://learn.microsoft.com/en-us/graph/api/worksheet-range?view=graph-rest-1.0) — `Files.ReadWrite` permission floor for range reads; no-`address` default behavior.
- [workbookRange resource type | Microsoft Graph v1.0](https://learn.microsoft.com/en-us/graph/api/resources/workbookrange?view=graph-rest-1.0) — `values` (raw) vs. `text` (displayed-string) property semantics.
- Research confirming no `activeWorksheet` concept exists in the Graph REST workbook surface (only in the in-document Office.js add-in API).
- [Unified cloud.microsoft domain for Microsoft 365 apps | Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365/enterprise/cloud-microsoft-domain?view=o365-worldwide) — the in-progress domain migration behind §1a's host-permission list.
- Community sources on SharePoint `Doc.aspx?sourcedoc={GUID}` and personal-OneDrive `resid` URL shapes, confirming no single clean file-ID-in-URL pattern exists for Excel Online.
- WXT vs. CRXJS vs. Plasmo 2026 comparison sources (WXT's release cadence, side-panel entrypoint support, framework-agnostic vanilla-TS scaffolding).
