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

---

## 3. OAuth scopes — resolving `EXTENSION_INTEGRATION.md`'s named gap

### Google

- Identity scopes (already fixed by the backend, §1 of the contract): `openid`, `email`.
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
- Data-access scope: **`Files.ReadWrite` (delegated)** — **not** `Files.Read`. This is the
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
  files alike.

---

## 4. File/range identification per platform

### Google Sheets

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

---

## 5. Data format contract compliance — both APIs' real response shapes

`sheets-backend-design.md` §1's contract is unchanged and non-negotiable: `POST /v1/csv/parse`
needs `rows` as a list of list of **display strings** — dates included — never a raw typed value
or numeric serial. Confirmed exactly how each platform's real API satisfies this:

- **Sheets:** rely on `spreadsheets.values.get`'s **default** `valueRenderOption`, which is
  `FORMATTED_VALUE` (confirmed via Google's API reference) — this returns every cell, including
  dates, as the string it displays in the Sheets UI. The adapter must simply *not* override this
  to `UNFORMATTED_VALUE` (which would return a raw numeric date serial) — the contract is
  satisfied by doing nothing, not by extra client-side logic.
- **Excel:** the workbook `Range` resource's `values` property holds the *raw* typed value (a
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

1. `launchAuthFlow(provider)` (§2) → raw OAuth `oauth_token`.
2. `POST /v1/auth/exchange` with `{provider, oauth_token}` → `{session_token, account_id,
   expires_at}` (contract §1). Handle both documented error cases in the sign-in UI: `401`
   (token rejected/unverified email) and `422` (Microsoft account has no usable email — contract
   §1's specific `mail`/`userPrincipalName` fallback case).
3. **Store `session_token` in `chrome.storage.local` — never `.sync`** — carried over verbatim
   from `oauth-identity-session-design.md` §2: `.sync` rides Chrome's own browser-account sync,
   a different and inapplicable guarantee from this backend's own cross-device account
   resolution (a second device just re-runs step 1–2 against its own OAuth grant and resolves
   back to the same `account_id` server-side, per the contract's cross-provider identity note —
   the token itself never needs to travel between devices).
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
3. **File/range adapters for both platforms.** Sheets' URL-parse → `spreadsheets.get` →
   `spreadsheets.values.get` path and Excel's `/shares` → `workbook/range` path (§4), each
   normalized into the same rows-of-display-strings shape (§5), including Sheets' ragged-row
   padding step. Wired into `POST /v1/csv/parse`.
4. **Mapping-confirmation screen.** Side-panel view swap from chat to a review table:
   `POST /v1/csv/{id}/propose-mapping`'s proposal rendered editable, then
   `POST /v1/csv/{id}/confirm` — per the contract, this proposal is never auto-accepted.
5. **Chat interface.** Message list, `POST /v1/ask` wiring with `conversation_id` continuity,
   rendering `final_answer`/`figure_check`/`hit_iteration_cap`, and the documented error-type
   handling (404 on a stale/foreign `conversation_id`/`csv_context_id`, 429 cap responses, 502/500
   generic-message handling) — all per contract §6, nothing invented beyond what it specifies.
6. **Usage and billing surfacing.** `GET /v1/usage` display; the 429 body's
   `prompt_byo_key`/`prompt_upgrade` fields driving which upsell to show;
   `POST /v1/billing/checkout-session` (open the returned URL in a new tab, and don't assume the
   account's tier updated on return, per contract §6's own stated webhook-latency caveat);
   `POST /v1/byo-key` registration form.
7. **Polish + manual QA pass** across both OAuth providers and both spreadsheet platforms
   end-to-end (sign-in → file read → mapping confirm → ask → usage/billing surfacing). No new
   endpoints or scopes — a verification session, not a feature one.

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
  even though it never writes (§3).
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
