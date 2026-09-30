# Spike — Phase D session 3a: auth and file access

**Date:** 2026-09-30. **Branch:** `spike/session3-auth`, local only. It was never pushed or
merged. The harness commits are `645e8ad` and `f372adf`. **Run by:** the project owner, live, in
Chrome, on their own personal Google and personal Microsoft accounts. Claude built the harness
but can't run Chrome. Every result below is what was actually observed. Where the harness
couldn't observe something directly, this doc says what was inferred and why.

## Why this spike existed

The session 3 investigation found that `docs/chrome-extension-design.md` §4, as written before
this spike, couldn't work with session 2's auth as built:
- The extension never holds a Google access token. The backend exchanges the code, uses the
  token for `userinfo`, and drops it.
- Neither provider's token is kept past sign-in.
- Nothing in the design covered how to get a provider token hours or days after a 90-day
  session began.

Current Microsoft docs also say the Graph Excel workbook API doesn't support workbooks on
personal (consumer) OneDrive, and the only test account is personal.

Three decisions came out of that investigation. They depended on behavior that no document
settled, so this spike tested it before any design doc was amended:
- **A3:** keep identity and data access separate. Data tokens are short-lived, obtained only
  when the user clicks something, and kept only in `chrome.storage.session`.
- **B2:** download the `.xlsx` and parse it in the extension.
- **C:** a sheet selector plus a range field.

## The harness

The harness was a "Spike tests" block added to the existing side panel. It had to be the side
panel so the spreadsheet tab stayed the active tab, and so the extension ID and the registered
`chromiumapp.org` redirect URIs didn't change. Each test logged one
`[SPIKE] <time> (<test>) <VERDICT> | <evidence JSON>` line to the console and to an on-panel log.
Secrets were redacted in the log:
- tokens and auth codes: first 6 characters plus the length
- `@microsoft.graph.downloadUrl`: hostname only
- Graph errors: `error.code` and `message` only

**Test data:** a Google Sheet and an Excel-for-the-web workbook (created and saved in Excel for
the web, on personal OneDrive). Each had a realistic `P&L` sheet:
- a title row, then a blank row
- headers in row 3, four quarters in rows 4–7
- Short Date period ends
- currency columns, and formula columns `=B-C` and `=D/B`
- a 1-decimal percent column
- a negative number shown in parentheses
- a custom `d-mmm-yy` date column
- a blank row, then a footnote

Both files used the en-US locale. The data was entered by pasting a TSV file. Formulas were
checked in the formula bar to be real formulas, not pasted values.

## Results

### (i) Silent re-auth with `launchWebAuthFlow` (`interactive:false`, `prompt=none`, `login_hint`)

| Case | Result |
|---|---|
| Google, silent, after the spike's own interactive auth whose code was **never exchanged** | **FAIL**, `interaction_required` |
| Google, silent, after a **real sign-in whose code the backend exchanged** | **PASS**, code returned with no UI |
| Google, silent, after a **full Chrome restart** | **PASS** |
| Google, with **two Google accounts signed in** and `login_hint` sent | **PASS**, no account-picker UI |
| Microsoft, silent, right after an interactive auth | **PASS** |
| Microsoft, silent, after a Chrome restart, with **consent revoked** before the restart | `consent_required`, **not** `login_required` |

**What this means:**
- **Google's silent flow only succeeds once a grant actually exists.** An authorize step whose
  code is never redeemed doesn't create the grant `prompt=none` needs. So **the first data
  grant per provider must be interactive and must complete a real code exchange.** For Google,
  that exchange goes through the backend. After that, silent works, across restarts and with
  several Google accounts signed in, as long as `login_hint` is sent.
- **Microsoft:** the error after the restart was `consent_required`, not `login_required`. That
  means the auth-flow sign-in session survived the restart, and the only thing missing was
  consent, which had been revoked deliberately. *Inferred, not directly observed:* with consent
  intact, silent Microsoft auth after a restart should succeed. The first live 3a test run
  should confirm it.
- **Not tested:** optional step (i)(c), signing out of Google in a normal tab and then trying
  silent auth. So **it's still unknown whether the `launchWebAuthFlow` window shares cookies
  with the browser profile.** The design doesn't depend on the answer: every silent failure
  falls back to an interactive flow inside the same user click.

### (ii) Google Sheets URL: `gid` and `range=`

- **`gid` updates live when you switch sheet tabs, in both the query string and the hash**, with
  no page reload. **PASS.**
- **"Get link to this range" puts `range=A3:G7` in the hash** (`#gid=…&range=A3:G7`). **PASS.**

**What this means:** the sheet the user is on can be read reliably from the tab URL. A `range=`
fragment, when present, is a supported way to pre-fill the range field.

### (iii) Personal OneDrive file resolution with `Files.Read` only

- Consent to `User.Read Files.Read` alone. The token response reported
  `granted_scope: "User.Read Files.Read"`.
- **The personal Excel-for-the-web edit URL has this shape:**
  `onedrive.live.com/personal/<cid>/_layouts/15/doc.aspx?sourcedoc={GUID}&action=edit`. There
  is no `resid`, and the file's GUID is in `sourcedoc`.
- **`/shares/u!<base64url(tab URL)>/driveItem`** (with `Prefer: redeemSharingLinkIfNecessary`)
  **→ 200**, the correct file, `driveType: "personal"`. **PASS with `Files.Read` alone**, even
  though the `shares-get` permission table lists `Files.ReadWrite` as the least-privileged
  permission.
- The `cid`/`resid` fallback: **n/a**. There's no `resid` in the URL. The harness didn't try
  resolving through `sourcedoc`.
- **The token response included a `refresh_token` even though `offline_access` wasn't
  requested.**

**What this means:**
- `/shares` is the only verified way to resolve the file, and there's **no verified fallback**
  if it fails for some future URL shape.
- **Work/school (OneDrive for Business/SharePoint) URLs weren't tested at all**, because there's
  no business tenant. Business resolution is unverified.
- Microsoft returns a refresh token that we didn't ask for. **Production must throw it away and
  never store it.** For SPA redirect URIs it's capped at 24h anyway (Microsoft identity
  platform, "Refresh tokens"), but there's no reason to keep one: silent re-auth covers the
  need.

### (iv) Graph Excel workbook API on a personal account, `Files.ReadWrite` token

**UNEXPECTED: it works.** `worksheets` returned both sheets. `worksheets('P&L')/usedRange`
returned `'P&L'!A1:G9`, and its `text` matched exactly what Excel displays, including
`3/31/2025` for the Short Date cells.

### (vii) Graph Excel workbook API on a personal account, `Files.Read`-only token

**Works too.** With `granted_scope: "User.Read Files.Read"`, the harness got both sheets,
`'P&L'!A1:G9`, and the exact display text.

**What (iv) + (vii) mean:** on this account, today, the workbook API works for reads with
`Files.Read` alone. That contradicts both Microsoft's reference note ("Support for workbooks
stored in OneDrive Consumer platform is still not available") and the per-endpoint permission
tables (least privilege `Files.ReadWrite`). **We still don't use it:**
- Microsoft's own documentation says consumer workbooks are officially unsupported. Microsoft
  Q&A answers from June 2024 and March 2026 describe it failing with `FileOpenUserUnauthorized`
  and say there's no roadmap. It could stop working at any time, for any account.
- (viii) below shows it has **no freshness advantage** over downloading the file.

The finding is recorded here so it isn't rediscovered as a surprise later.

### (v) Download + parse the `.xlsx` with SheetJS 0.20.3 (from `cdn.sheetjs.com`)

- Metadata (`select=…,@microsoft.graph.downloadUrl`) and the download both **work with
  `Files.Read`**. **The download host is `my.microsoftpersonalcontent.com`.** The extension's
  host permission makes the `fetch` work.
- **29 of 33 checks matched exactly.** Checks covered 25 rendered cells plus 8 formula-cell
  checks. The **4 misses are all the built-in Short Date cells (A4–A7): SheetJS renders
  `3/31/25`, while Excel displays `3/31/2025`.** Everything else rendered exactly:
  - currency: `$1,250,000`
  - the 1-decimal percent: `61.6%`
  - the parenthesized negative: `($45,000)`
  - custom `d-mmm-yy`: `28-Apr-25`
  - text cells
- **Formula cells D4:E7 have both the formula and a cached value.** Excel for the web saves
  cached results, so no recalculation is needed.
- **Freshness:**
  - On the first run, **the downloaded file lagged the on-screen workbook by several minutes.**
    It was stale at 11:50 and fresh by 11:54.
  - A sheet rename also took several minutes to show up.

### (viii) Freshness: workbook API vs download+parse, for one edited cell

`P&L!B4` was edited from `1250000` to `1250001`. Then both the workbook API (`range(address='B4')`)
and download+parse were read side by side, repeatedly:
- **The first read, right after the edit: both stale.**
- **At +90s: both fresh.**
- `lastModifiedDateTime` jumped to 12:17:57, about 24s after the edit, and `eTag` incremented.

**What this means:** the workbook API reads the **saved file**, not the live editing session,
so it has **no freshness advantage** over downloading. Both only reflect edits after Excel for
the web autosaves, and then only after some extra propagation delay. The product should **show
the file's `lastModifiedDateTime` ("data as of last save")** and tell users that very recent
edits may take a minute or two to appear.

**Not tested:** how fresh Google Sheets data is. The Sheets API reads the live document, and no
Google token was exchanged in the spike.

### (vi) `chrome.storage.session` from the side panel

- Write and read-back: works (see the harness bug below).
- The value is **not** visible in `chrome.storage.local`. **PASS.**
- It **survives closing and reopening the side panel.** **PASS.**
- It is **cleared by a full Chrome restart.** **PASS.**

**What this means:** `chrome.storage.session` is the right place for short-lived provider data
tokens. It never touches disk-backed `.local`, it's shared across open panels, and it's gone
when the browser closes.

## Harness-bug lesson

(vi)'s write check **falsely FAILed** on the first run. It compared
`JSON.stringify(readBack) === JSON.stringify(probe)`, and `chrome.storage` returned the object's
keys in a different order. The data was fine; the comparison was wrong. It was fixed in
`f372adf` to compare fields (`value` and `at`), and the retest passed.

**The lesson:** a spike harness is untested code too, so a FAIL has two candidate causes, the
system under test and the harness itself. Before recording a FAIL as a finding, check that the
harness's comparison is actually right for what it's measuring. Structural comparisons (fields,
not serialized strings) avoid this whole class of bug.

A related lesson from (i): the first Google silent FAIL *looked* like it could be a harness or
cookie problem. It turned out to be a real, meaningful result: no redeemed grant, so silent auth
can't work yet. That only became clear because the evidence line logged the provider's exact
`error` value (`interaction_required`), not just FAIL. Always log the raw provider signal next
to the verdict.

## Resulting decisions (final)

- **A3 confirmed.**
  - Sign-in requests identity scopes only.
  - The first data grant per provider is interactive and completes a real code exchange.
    Google's goes through a new backend endpoint, `POST /v1/google/data-token`.
  - After that, `launchWebAuthFlow` runs silently (`prompt=none` + `login_hint`) inside a user
    click, falling back to interactive if it fails.
  - Microsoft's unrequested `refresh_token` is discarded and never stored.
  - Data tokens live only in `chrome.storage.session`.
- **B2 final.**
  - `/shares` resolution, then download, then SheetJS 0.20.3 from `cdn.sheetjs.com`, all with
    **`Files.Read`** only. Microsoft sign-in no longer asks for `Files.ReadWrite` at all.
  - Production needs the host permission `https://my.microsoftpersonalcontent.com/*`.
  - The workbook API is not used.
  - The file's `lastModifiedDateTime` is shown to the user.
- **C confirmed.** Default to the live `gid`; pre-fill the range from a `range=` fragment when
  there is one; sheet selector plus range field.

## Open questions carried into 3b (not decided)

1. **Date normalization.** Should dates be converted to ISO-8601 on both platforms before
   `/v1/csv/parse`? Two reasons to consider it: the SheetJS Short Date gap (`3/31/25`), and the
   fact that Google's `FORMATTED_VALUE` date strings depend on the spreadsheet's locale.
2. **Numbers as underlying values or display strings.** The project's traceability principle is
   at stake. For example, E5's underlying value is `0.6145…` but it displays as `61.5%`.

Other items that remain unverified:
- work/school OneDrive and SharePoint resolution and download
- whether the auth-flow window shares cookies with the browser profile
- Microsoft silent auth after a restart with consent intact (inferred to work)
- how fresh Google Sheets data is

## References

- `docs/chrome-extension-design.md` (§2, §3, §4, §6 and §9 amended from this spike)
- `backend/EXTENSION_INTEGRATION.md` (the planned `POST /v1/google/data-token` and the data-scope
  split)
- [Working with Excel in Microsoft Graph](https://learn.microsoft.com/en-us/graph/api/resources/excel?view=graph-rest-1.0) — the consumer-OneDrive "not available" note
- [Microsoft Q&A, 2024](https://learn.microsoft.com/en-us/answers/questions/1691489/workbook-excel-api-for-personal-onedrive-accounts) and [2026](https://learn.microsoft.com/en-gb/answers/questions/5835926/microsoft-graph-api-workbook-paths-for-excel-store)
- [Access shared items (shares-get)](https://learn.microsoft.com/en-us/graph/api/shares-get?view=graph-rest-1.0) and [Download driveItem content](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0)
- [Refresh tokens in the Microsoft identity platform](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens) — the 24h SPA refresh-token cap
- [Google OAuth 2.0 for web server apps](https://developers.google.com/identity/protocols/oauth2/web-server) — `prompt=none`, `login_hint`, `include_granted_scopes`
- [chrome.identity](https://developer.chrome.com/docs/extensions/reference/api/identity) — `abortOnLoadForNonInteractive`, `timeoutMsForNonInteractive`
- [SheetJS installation](https://docs.sheetjs.com/docs/getting-started/installation/frameworks) — 0.20.3 from the CDN; the npm registry copy is stale
