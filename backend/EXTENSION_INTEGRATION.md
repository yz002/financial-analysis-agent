# Chrome Extension Integration Contract

This document is the reference for building the Chrome extension's frontend against this
backend. It describes the real, current HTTP contract — verified against `backend/app/main.py`,
`backend/app/schemas.py`, and `backend/app/oauth_providers.py` as they exist today, not against
either original design doc's stated intent. Where the implementation ended up differing from
`oauth-identity-session-design.md` or `sheets-backend-design.md`, this document follows the code
and says so explicitly.

**Why this document exists now:** `backend/SECURITY.md` §2 ("`X-Install-Id` had no real
authentication") named this contract as a hard prerequisite for starting extension frontend
work — whoever held an `install_id` could impersonate that install indefinitely, with no
rotation or revocation path. That finding is now resolved: every route below is protected by
real OAuth-verified sign-in and a revocable, opaque bearer session token. `X-Install-Id` is gone
completely — it is not read anywhere in this codebase anymore.

All endpoints are versioned under `/v1`, JSON over HTTPS.

---

## 1. Sign-in flow

1. **Obtain OAuth credentials from Google or Microsoft**, client-side, via
   `chrome.identity.launchWebAuthFlow` (Authorization Code + PKCE, for both providers —
   see the extension's own design doc for why `getAuthToken` isn't used for Google
   either). **What gets sent onward in step 2 differs by provider — this is new as of
   Phase D session 2, and the reason is load-bearing, not incidental:**

   - **Google:** send the raw authorization `code`, the PKCE `code_verifier`, and the
     `redirect_uri` used in the authorization request — **not** a pre-exchanged access
     token. Confirmed against Google's own current OAuth 2.0 documentation: the "Web
     application" client type — the only Google client type compatible with
     `launchWebAuthFlow`'s `https://<extension-id>.chromiumapp.org/` redirect
     requirement — is documented by Google itself as a **confidential client**
     ("a web server application does need a secret"). PKCE does not substitute for
     `client_secret` at its token endpoint. Since a client secret can't be kept
     confidential inside extension code (any unpacked/installed extension can be
     decompiled), **this backend performs the code-for-token exchange itself**,
     server-side, where `GOOGLE_CLIENT_SECRET` can stay confidential (see
     `app/oauth_providers.py`'s `exchange_google_code_for_token`).
   - **Microsoft:** obtain a genuine OAuth access token client-side and send it
     directly, as before. Azure's **"Single-page application"** platform type is a
     genuine no-secret public client — PKCE alone is sufficient there, so the extension
     completes the full exchange itself and never needs a Microsoft client secret
     anywhere.

   Required scopes, either way, at minimum:
   - **Google:** the `openid` and `email` scopes. This backend verifies the resulting
     access token by calling `GET https://openidconnect.googleapis.com/v1/userinfo`
     with it — that call only returns a usable `email`/`email_verified` pair when those
     two scopes were granted.
   - **Microsoft:** the `User.Read` scope. This backend verifies the token by calling
     `GET https://graph.microsoft.com/v1.0/me` with it — `User.Read` is required for
     that call to return the `mail` field this backend reads.

   **Not covered here, and not determined by anything in this codebase:** whatever additional
   scope is needed for the extension to actually read/write the person's Sheet or Excel file
   (e.g. a Sheets or Drive scope on the Google side, a Files/Graph scope beyond `User.Read` on
   the Microsoft side). No such scope string appears anywhere in this backend's code or design
   docs — `sheets-backend-design.md`'s identity discussion was written for a Google Workspace
   Add-on (Apps Script), which gets implicit access to its containing Sheet with no explicit
   OAuth scope at all, a different authorization model than a Chrome extension's
   `chrome.identity` flow. The extension team owns determining and requesting that data-access
   scope; this backend never sees or checks it — it only ever verifies the identity scopes above.

   (Amended Phase D session 3a (spike-verified): **the token or code sent to `/v1/auth/exchange` now carries identity scopes only**:
   Google `openid email`, Microsoft `User.Read`. Sign-in no longer asks for
   `spreadsheets.readonly` or `Files.ReadWrite`. Data access is a separate grant made later,
   when the user clicks to read a spreadsheet (`docs/chrome-extension-design.md` §2
   "Data-access tokens"):
   - Microsoft's data grant (`Files.Read`) is exchanged client-side and never reaches this
     backend.
   - Google's data grant (`openid email spreadsheets.readonly`) can't be exchanged client-side,
     for the same confidential-client reason as step 2 below. It goes through the one new
     route, `POST /v1/google/data-token` (§1a).

   Spike results: `docs/spikes/session3a-auth-file-access.md`.)

   (Amended Phase D session 3a (live-verified): **for Microsoft, "identity scopes only"
   describes the request, not the token this backend receives.** Microsoft adds every scope
   the account has already consented to for Graph. Once a person has made the `Files.Read`
   data grant, the `User.Read`-only sign-in token sent here carries `User.Read Files.Read`.
   For accounts that consented before 3a it may also carry `Files.ReadWrite`.
   - This backend uses that token once, for `/me`, and stores and logs nothing.
   - Google sign-in tokens carry only `openid email`.
   - Planned fix: verify Microsoft identity with a validated ID token, so no Graph token
     reaches this backend (`docs/chrome-extension-design.md` §10, open item 1).)

2. **Exchange for a session token:**

   `POST /v1/auth/exchange` — **no authentication required** (this is the one route a caller
   hits before having a session token at all).

   Request body — **shape depends on `provider`:**
   ```json
   // provider: "google"
   {
     "provider": "google",
     "code": "<the authorization code from launchWebAuthFlow's redirect>",
     "code_verifier": "<the PKCE code_verifier used to build the authorization URL>",
     "redirect_uri": "<the exact redirect_uri used in the authorization request>"
   }
   ```
   ```json
   // provider: "microsoft"
   {
     "provider": "microsoft",
     "oauth_token": "<the OAuth access token obtained in step 1>"
   }
   ```
   Sending `oauth_token` for `"google"`, or `code`/`code_verifier`/`redirect_uri` for
   `"microsoft"`, is a request-shape error — see the two distinct `422` cases below.

   Response body (200) — **identical for both providers:**
   ```json
   {
     "session_token": "<opaque bearer token, shown exactly once>",
     "account_id": "<uuid string>",
     "expires_at": "<ISO-8601 timestamp>"
   }
   ```

   `session_token` is a random opaque string (not a JWT, not decodable/inspectable). It is
   returned once, at exchange time, and never re-derivable afterward — the backend stores only
   its SHA-256 hash. There is no way to look it up or recover it later; if it's lost, the only
   remedy is to run this exchange again.

   Error responses — **two genuinely different situations both happen to return `422`; treat
   them as two distinct, named cases, not one:**

   - `401` — `{"detail": "OAuth token could not be verified."}` — the provider rejected the
     credential: an invalid/expired/wrong-audience Microsoft access token, an unverified Google
     account email, **or now also a failed Google code-for-token exchange** (invalid/expired
     `code`, a `code_verifier`/`redirect_uri` mismatch, etc.) — Google doesn't distinguish these
     causes to callers, so this backend doesn't either, same anti-enumeration convention as
     everywhere else in this contract. **Client handling: identical to any other 401 — discard
     any stored state and re-run sign-in from step 1 (§3 below applies the same way here).**
   - `422`, case **"missing email"** — `{"detail": "No usable email address is available for
     this account."}` — **Microsoft-only**, unchanged from before this session: the account has
     no `mail` and its `userPrincipalName` isn't syntactically an email address either. This is
     a real, expected account-state outcome, not a bug — **client handling: relay it to the
     person as-is (e.g. "this Microsoft account has no usable email"); retrying or re-running
     sign-in will not resolve it, since it's a property of the account itself.**
   - `422`, case **"wrong fields for provider"** — `{"detail": "Google sign-in requires code,
     code_verifier, and redirect_uri."}` or `{"detail": "Microsoft sign-in requires
     oauth_token."}` — the request body didn't carry the fields this provider's branch expects
     (new as of Phase D session 2). This should never happen from a correctly-implemented
     client — it means the caller's own code has a bug (sent the wrong shape for the
     `provider` value it also sent). **Client handling: this is a bug in the extension's own
     code to fix, not something a retry or re-sign-in would ever resolve, unlike a real 401 —
     do not show it to the person as an account problem, and do not retry the exchange
     automatically.**
   - `500` — `{"detail": "Google OAuth is not configured."}` — `GOOGLE_CLIENT_ID`/
     `GOOGLE_CLIENT_SECRET` aren't set server-side. A deployment misconfiguration, not
     something the extension can act on beyond surfacing a generic "sign-in is currently
     unavailable" message, same class as `/v1/billing/checkout-session`'s missing
     `STRIPE_PRICE_ID`.

   Cross-provider identity note (amended 2026-09-29, see `SECURITY.md` §7): each provider
   identity is its own account. Signing in with a different provider, or a different Microsoft
   account, gets a separate `account_id` **even when the email is the same**. Email is never
   used to select an account. The earlier automatic same-email merge was removed as an
   account-takeover risk. The same provider account always resolves to the same `account_id`
   across devices and sign-ins. The extension doesn't need to do anything for this, but it
   shouldn't tell people that signing in with "the same email" elsewhere reaches the same
   account.

3. **Store `session_token` in `chrome.storage.local`, never `chrome.storage.sync`.**
   `.sync` piggybacks on Chrome's own browser-account sync, which is a different (and
   inapplicable) guarantee from this backend's own cross-device account resolution — signing in
   again on a second device re-runs step 2 against that device's own OAuth grant and resolves
   back to the same `account_id` on its own; the token itself never needs to travel between
   devices.

4. **Send it as `Authorization: Bearer <session_token>` on every other request below.**
   `X-Install-Id` no longer does anything server-side — it is not read by any route in this
   codebase. Do not send it; it has been fully replaced by the bearer token.

---

## 1a. Google data-access token — `POST /v1/google/data-token` (Amended Phase D session 3a (spike-verified))

**Status: implemented** (Phase D session 3a), in `backend/app/main.py`'s `google_data_token`
and `app/oauth_providers.py`'s `exchange_google_data_code`. It's covered by
`backend/tests/test_google_data_token.py` and was live-tested end to end with the extension
against a local backend. This section now follows the code, like the rest of this document.
It's the single contract addition that `docs/chrome-extension-design.md` §8 explicitly allows.

**Purpose:** exchange a Google data-access authorization code for a short-lived access token
that the extension uses to call the Google Sheets API itself. Google's "Web application" client
is confidential (see §1 step 1), so this exchange needs `GOOGLE_CLIENT_SECRET` and has to happen
here. **This backend never reads spreadsheet data with the token.** It only exchanges the code
and hands the token back.

**Auth required:** `Authorization: Bearer <session_token>`, the same as every §6 route.

Request body:
```json
{
  "code": "<authorization code from the data-grant launchWebAuthFlow redirect>",
  "code_verifier": "<the PKCE code_verifier for that authorization request>",
  "redirect_uri": "<the exact redirect_uri used in that authorization request>"
}
```
The authorization request that produced `code` must ask for
`openid email https://www.googleapis.com/auth/spreadsheets.readonly` with
`include_granted_scopes=true`. `openid email` lets this backend return the data account's
`email`, which the extension uses as `login_hint` for later silent re-auth.

**The data account may differ from the sign-in identity, on purpose.** The Google account this
code was granted for doesn't have to be one of the caller's linked identities. A
Microsoft-signed-in session can read a Google Sheet, and a Google-signed-in person can read a
Sheet owned by a different Google account. This route doesn't compare them
(`docs/chrome-extension-design.md` §2, "Data account vs sign-in identity"). Such a check would
add almost no security: the code is PKCE-bound to this extension and was obtained through
Google's own consent, so even a stolen session can only mint tokens for Google accounts the
holder already controls.

Server behavior, all of which is required:
1. Exchange the code at Google's token endpoint with `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`.
   **Never send `access_type=offline`.** If a `refresh_token` appears in the response anyway,
   discard it.
2. Call `userinfo` with the resulting access token and read `email`, the data account's email.
   Don't compare the account against the caller's `LinkedIdentity` rows (see above).
3. Return the token. **Store nothing**: no DB row, no cache. **Log nothing** about it, not the
   token, not the code. Send `Cache-Control: no-store`.

Response (200):
```json
{
  "access_token": "<Google access token, ~1h lifetime>",
  "expires_in": 3599,
  "scope": "<space-separated scopes Google actually granted>",
  "email": "<the data account's email, from userinfo>"
}
```
The extension keeps `access_token` only in `chrome.storage.session`. It keeps `email`, the data
account, in `chrome.storage.local`, so silent re-auth works after a browser restart; it's not a
credential. Both are cleared on sign-out, revoke-all, any `401`, and every successful
sign-in. (Amended Phase D session 3a: the email was originally specified as session-only.)
Google's granular consent lets the user untick the Sheets scope, so the extension must check
`scope` for `https://www.googleapis.com/auth/spreadsheets.readonly` and show a named "Sheets
access wasn't granted" message if it's missing. In that case it stores neither the token nor
the email.

Errors:
- `401` — `{"detail": "Invalid or expired session token."}`: the *session token* failed. This
  is §3's rule, handled exactly as §3 says: discard the session and show the signed-out UI.
- `400` — `{"detail": "Google authorization could not be exchanged."}`: Google rejected the
  code exchange (invalid or expired `code`, a `code_verifier`/`redirect_uri` mismatch) or the
  `userinfo` call. **This is deliberately not a `401`.** On this authenticated route a `401`
  would trigger §3's discard-the-session handling for what is only a failed data grant. Client
  handling: keep the session and offer the read action again.
- `422` — `{"detail": "code, code_verifier, and redirect_uri are required."}`: a request-shape
  bug in the extension, the same class as §1's "wrong fields for provider". Don't retry.
- `500` — `{"detail": "Google OAuth is not configured."}`: the same server misconfiguration as
  `/v1/auth/exchange`'s.

The session requirement stays, for authentication and abuse control: without it, this
backend's confidential client would be a free code-exchange service. *Future consideration, not
built:* per-account rate limiting on this route.

There's **no Microsoft equivalent**. Microsoft data tokens are exchanged client-side (a public
SPA client) and never reach this backend.

---

## 2. Authenticated request contract

Every route in §6 below (all of them except the ones in §1 and §4, which have their own rules)
requires this exact header on every request:

```
Authorization: Bearer <session_token>
```

There is no other way to authenticate. No cookies are set or read anywhere in this backend — the
token is purely a request header in, and a response body field out (at exchange time only).

---

## 3. What a 401 means

Any authenticated route returns `401` with the identical body:
```json
{"detail": "Invalid or expired session token."}
```
for **all** of the following, indistinguishably by design: a missing/malformed
`Authorization` header, a token that was never issued, a token that has expired (sessions are
valid 90 days from last use, sliding forward on each successful request), and a token that was
explicitly revoked (via logout or revoke-all, §4).

This is intentional — this backend's anti-enumeration convention never lets a caller learn *why*
a credential failed. **The correct client behavior is the same in every case: discard the stored
token and show the signed-out UI (the same sign-in buttons a fresh load shows).** Do not attempt
to distinguish "expired" from "revoked" from "never valid" — there is no signal available to do
so, and no future version of this backend is expected to add one.

**"Show the signed-out UI" is not "start a new sign-in."** The extension must never call its
`launchWebAuthFlow`-based auth flow in response to a `401` (or from `/v1/auth/logout`/
`/v1/auth/sessions/revoke-all`, §4) — a 401 clears local state and waits for an explicit sign-in
button click, exactly like a person opening the panel for the first time. Phase D session 2's
implementation briefly had `revoke-all` auto-relaunch sign-in; live testing showed this produces
a surprise OAuth popup with no user action, so it was removed (Phase D session 3) — sign-in
begins only from a direct click, with no exception for any error-handling path.

(Amended Phase D session 3a (spike-verified): the same rule covers the data grants in §1a and `docs/chrome-extension-design.md` §2
"Data-access tokens". No `launchWebAuthFlow` call, silent (`prompt=none`) or interactive, starts
except inside the handler for a direct user click. Silent data-token attempts are part of a
user-initiated read, never a background refresh. A `401` also clears any data tokens held in
`chrome.storage.session`.)

(Amended Phase D session 3a (live-verified): a `401` also clears the data-account emails in
`chrome.storage.local`. **Chrome itself doesn't enforce the click rule:**
`launchWebAuthFlow({interactive: true})` opened a window with no user gesture in the live test.
The extension's `isTrusted` check on the click event, in `getDataToken`, is the only
enforcement (`docs/chrome-extension-design.md` §6 step 5).)

---

## 4. Logout and revoke-all

Both require `Authorization: Bearer <session_token>` and take **no request body**.

### `POST /v1/auth/logout`
Call this on an explicit "sign out" action in the extension. Revokes only the one session
belonging to the token that was presented — other devices/sessions on the same account are
untouched.

Response (200): `{"revoked": true}` — unconditional; there is no failure mode once the token
itself passed authentication.

### `POST /v1/auth/sessions/revoke-all`
Call this for the "sign out everywhere" / suspected-compromised-credential case. Revokes every
session on the caller's account, **including the one making this call** — the extension should
expect its own current token to stop working immediately after this call and must re-run §1 to
get a new one, on this device too.

Response (200): `{"revoked": true}` — unconditional, same as logout.

---

## 5. Error response shape, generally

Every error below is a standard FastAPI `HTTPException` response: `{"detail": <string or
object>}`, with the stated HTTP status. A request body that fails basic schema validation
(wrong type, missing required field) returns FastAPI's own `422` with a `detail` array
describing the validation failure — not covered field-by-field here since it's generic across
every route.

---

## 6. Full route reference

(Amended Phase D session 3a: `POST /v1/google/data-token` is specified in §1a and implemented.)

### `POST /v1/csv/parse`
Auth required. Parses spreadsheet cell data into a structured CSV context for later mapping.

Request:
```json
{
  "rows": [["header1", "header2", ...], ["cell", "cell", ...], ...],  // list of list of strings
  "filename": "some-name.csv",
  "source": {                          // optional (Amended Phase D session 3b)
    "platform": "google_sheets",       // or "excel"
    "sheet_name": "P&L",
    "range": "A3:G7",                  // the exact A1 rectangle sent; its first row is the header
    "file_name": "FA Spike Test",
    "modified_at": null                // ISO-8601 when known (Excel lastModifiedDateTime), else null
  }
}
```
Every cell must be sent as a **display string**, not a raw typed value — this matters
specifically for date cells, which the backend expects to parse as ordinary date strings, not
as a spreadsheet's internal numeric date-serial format.

(Amended Phase D session 3b: **cell encoding.** "Display string" above is refined to these
rules. Every cell is still a JSON string, and a date is still never sent as a serial.)
- **Every cell is a JSON string.** A number, `null` or boolean fails request validation (`422`).
  An empty cell is `""`.
- **A date cell is sent as ISO-8601**: `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM:SS` when it has a
  time. Locale-formatted strings like `01/02/2025` are read month-first by the backend, so a
  non-US date would silently become the wrong date.
- **A numeric cell is sent as its underlying value**, in JavaScript `String(value)` form:
  `1250000`, `-45000`, `0.6145038167938931`, and exponent form at the extremes (`1e+21`,
  `1e-7`), which the backend parses. A display string can hide scale (`#,##0,` shows
  1,250,000 as `1,250`) or round.
- **Everything else is sent as displayed**: text, booleans (`TRUE`), and formula errors as
  their literal (`#DIV/0!`). A non-numeric cell in a column later mapped to a concept becomes
  "no value" for that period.

(Amended Phase D session 3b: **`source` and cell provenance.**)
- `source` is optional. When it's present:
  - `range` must be exactly `len(rows)` rows by `len(rows[0])` columns, or the request is
    refused in-band.
  - A malformed `range` (not `A1:B2` form) is a `422`.
- The backend stores `source` with the rows. Once the mapping is confirmed, every cited
  figure carries `source_cell`, for example `'P&L'!B4`, alongside `source_row`/`source_column`.
- The extension therefore sends one contiguous rectangle, and trims only trailing empty rows
  and columns.

(Amended Phase D session 3b: **size limits and new refusals**, all in-band unless noted.)
- At most 200 columns, 2,000 data rows, 50,000 cells in total (header included), and 1,000
  characters per cell.
- At most a **2 MB request body**. It's checked after authentication and before the JSON is
  parsed, and it applies to chunked bodies too. An oversize body gets `200` with
  `"parse_error": "This selection is too large to send (over 2 MB)…"`.
- An **empty or whitespace-only header cell** is refused, naming its position: the cell
  (`cell C3`) when `source` is given, otherwise the column number. Two blank headers used to
  surface as an empty "duplicate column headers ()" message.
- A missing or invalid `Authorization` header is a `401` before any of the body is read.

Response (200 — always 200; structural failures are reported in-band, not via HTTP error status):
```json
{
  "csv_context_id": "<uuid string>",   // null if parse_error is set
  "columns": ["col1", "col2", ...],
  "sample_rows": [["...", "..."], ...],
  "parse_error": null                   // or a string describing why parsing failed
}
```
`csv_context_id` is short-lived (expires in 1 hour) until confirmed via `/confirm` below.
(Amended Phase D session 4: the expiry is now enforced by propose-mapping and confirm
themselves, as a `404`, not only by the cleanup job that later deletes the row.)

### `POST /v1/csv/{csv_context_id}/propose-mapping`
Auth required. `csv_context_id` is a path parameter (from `/csv/parse`'s response). **No request
body.**

Response (200):
```json
{
  "proposal": [
    {"csv_column": "Revenue", "proposed_role": "revenue", "rationale": "..."},
    ...
  ],
  "note": null   // or a string with additional context
}
```
This proposal is not authoritative — the extension must let the person review/edit it before
calling `/confirm`.

Errors: `502` (`{"detail": "Anthropic API error."}`) or `500`
(`{"detail": "propose_mapping failed unexpectedly."}`) if the underlying mapping call fails —
both generic, with no exception detail included, same hardening rule `/v1/ask` already applies
below.

(Amended Phase D session 4:
- **Idempotent per context.** Once a usable proposal exists for a `csv_context_id`, calling
  again returns it without a second model call. A proposal whose `note` is set (the model's
  output couldn't be used, so every column is `unmapped`) isn't kept, so calling again makes a
  fresh attempt.
- **A daily cap on fresh proposals:** 30 per account in a rolling 24 hours, whatever the tier.
  Each fresh model call, including one that then fails, is recorded as a usage event of its
  own kind. Proposals **never count toward the question caps** in `/v1/usage` or `/v1/ask`'s
  429, and always run on the master key, never a BYO key. Over the cap:
  `429 {"detail": {"error": "mapping_cap_reached", "resets_at": "<ISO-8601>"}}`. A proposal
  isn't required to confirm, so the extension offers manual mapping instead.
- `404` (`"csv context not found"`) also covers an **expired** context: an unconfirmed one
  past its 1-hour lifetime is refused even before the cleanup job deletes it.
- `409` (`{"detail": "csv context already confirmed"}`) for a confirmed context.
- Only the prompt is bounded: headers are cut to 200 characters and sample cells to 100 when
  sent to the model. Proposed `csv_column` values are always the real, full header names.)

### `POST /v1/csv/{csv_context_id}/confirm`
Auth required. `csv_context_id` is a path parameter.

Request:
```json
{
  "mapping": {"Revenue": "revenue", "Date": "period_end", ...},  // csv column -> role
  "entity_name": "Acme Corp"
}
```

Response (200) — one of two shapes, both under the same schema:
```json
// success
{"confirmed": true, "cadence": "quarterly", "warnings": [], "concepts_unavailable": [], "errors": []}
// failure (still HTTP 200 -- this is a validation result, not a request error)
{"confirmed": false, "cadence": null, "warnings": [...], "concepts_unavailable": [], "errors": ["..."]}
```
`warnings`, `concepts_unavailable`, and `errors` default to `[]` when not otherwise populated.

Once confirmed, the CSV context becomes durable (no longer expires) and can be referenced by
`csv_context_id` from `/v1/ask` below.

Error: `404` (`{"detail": "csv context not found"}`) if `csv_context_id` doesn't exist or belongs
to a different account — these two cases are deliberately indistinguishable, same
anti-enumeration posture as §3's 401s.

(Amended Phase D session 4: **units, unparseable cells, and immutability.** Every new field is
optional, so older callers keep working.)
- **Request additions:**
  ```json
  {
    "mapping": {...},
    "entity_name": "Acme Corp",          // 1-200 characters after trimming; blank is a 422
    "scale": "thousands",                // "ones" | "thousands" | "millions" | "billions"
    "currency": null,                    // ISO-4217 code like "USD", or null = not specified
    "accept_unparsed_cells": false,
    "ack_fingerprint": null              // echo of a previous response's ack_fingerprint
  }
  ```
  - **`scale`** is the unit the sheet's numbers were typed in. The backend converts every mapped
    value to ones, exactly, before anything else sees it: the agent and its figure check only
    ever work in ones. It defaults to `"ones"` **only for callers that predate it. The
    extension always sends it, and its selector has no default:** a sheet typed in thousands
    and confirmed as ones would make every answer wrong by 1000x.
  - **`currency`** is a label, never a conversion. `null` means not specified, and the agent then
    reports the business's figures with no currency symbol or code.
- **Unparseable cells need an acknowledgement.** When a column mapped to a concept has non-blank
  cells that don't parse as numbers (`61.5%` typed as text, `#DIV/0!`, `TRUE`, `€1,250`), the
  response is `confirmed: false`, `requires_acknowledgement: true`, with:
  ```json
  "unparsed_cells": [{"cell": "'P&L'!C4", "source_row": 0, "column": "Net Income",
                      "role": "net_income", "period_end": "2024-01-01", "value": "#DIV/0!"}],
  "ack_fingerprint": "<hex>"
  ```
  `cell` is `null` without a `source`, and `value` is cut to 50 characters. To go ahead, confirm
  again with `accept_unparsed_cells: true` **and** that `ack_fingerprint`. The fingerprint
  covers the mapping, the scale and the cell list. If any of them changed, the response is
  `requires_acknowledgement` again, with the new list and a new fingerprint. On success, those
  periods have no value for that concept, and `unparsed_cells` is returned again for the
  record. Blank cells aren't listed (a blank means "not reported"); each mapped concept with
  blanks gets one line in `warnings`.
- **Other response additions:** `requires_acknowledgement` (default `false`),
  `unparsed_cells` (default `[]`), `ack_fingerprint` (default `null`), and `scale`/`currency`
  echoed back.
- **Better refusals.** A `mapping` key that isn't a column, or a role that isn't a concept,
  `period_end` or `unmapped`, is now an in-band `errors[]` entry. It used to be a 500, or for
  roles, silently ignored. A dropped bad-date row's warning names its cell when there's a
  `source`.
- **Confirmed contexts are immutable:** `409` (`{"detail": "csv context already
  confirmed"}`). To re-map, send the rows to `/v1/csv/parse` again and confirm the new
  `csv_context_id`. Conversations that used the old statement keep pointing at it.
- **`404` also covers an expired context** (an unconfirmed one past its 1-hour lifetime), as
  for propose-mapping.
- Tool results the agent sees for a confirmed statement now carry `units`, and each value's
  citation carries `sheet_scale`. That's internal, not part of this HTTP contract.

### `POST /v1/ask`
Auth required. The main question-answering endpoint.

Request:
```json
{
  "question": "What was revenue last quarter?",
  "csv_context_id": null,       // optional -- a confirmed csv_context_id to ground the question in
  "conversation_id": null       // optional -- continue an existing conversation
}
```

Response (200):
```json
{
  "conversation_id": "<uuid string>",
  "turn_id": "<uuid string>",
  "final_answer": "...",
  "hit_iteration_cap": false,
  "figure_check": { ... },
  "citations": [],              // currently always empty -- not yet implemented, reserved for future use
  "tool_calls_summary": [{"tool_name": "get_financial_statement", "is_error": false}, ...]
}
```
**`citations` is currently always an empty array** regardless of the answer — provenance-derived
citation extraction from `tool_calls` is not implemented yet, despite being described in the
original design doc. Don't build extension UI that assumes it will be populated today.
*(Superseded by the session 5 amendment below, pending live verification: `citations` is now
populated.)*

Errors:
- `404` — `{"detail": "conversation not found"}` or `{"detail": "csv context not found"}` — a
  supplied `conversation_id`/`csv_context_id` is malformed, nonexistent, or belongs to a
  different account (indistinguishable, same convention as above). Note specifically:
  `csv_context_id` must refer to an already-**confirmed** context (via `/confirm` above) — an
  unconfirmed or still-in-progress one gets this same 404.
- `429` — usage cap reached:
  ```json
  {"detail": {"error": "daily_cap_reached", "prompt_byo_key": false, "prompt_upgrade": true, "resets_at": "<ISO-8601 or null>"}}
  ```
  or, for a paid account past its monthly cap:
  ```json
  {"detail": {"error": "monthly_cap_reached", "prompt_byo_key": true, "prompt_upgrade": false, "resets_at": "<ISO-8601 or null>"}}
  ```
  Use `prompt_byo_key`/`prompt_upgrade` to decide which upsell to show; `resets_at` is when the
  cap next clears.
- `502` — an Anthropic API failure. Body is a generic message (`"Anthropic API error."`, or, for
  an authentication/key-rejection failure specifically, `"Anthropic API authentication failed."`
  or, if the account has a BYO key registered, `"Your Anthropic API key was rejected. Please
  re-register a valid key."`) — deliberately generic; the underlying exception text is never
  included in the response.
- `500` — `"run_agent failed unexpectedly."`, or, in the specific case of a BYO key that can no
  longer be decrypted (e.g. after a server-side encryption key rotation), `"Stored BYO key could
  not be decrypted and has been deactivated; please re-register it."` — in that case the
  account's BYO key has been deactivated server-side and the account has silently fallen through
  to the free/paid tier's normal caps; the extension should prompt the person to re-register
  their key.

**Not currently available:** there is no endpoint to list past conversations or read a
conversation's full turn history — `conversation_id` can be used to *continue* a thread via
`/v1/ask`, but nothing currently exposes reading it back. There is also no endpoint to fetch a
turn's full tool-call trace beyond the `tool_calls_summary` (name + error flag only) included
above. Both were described in the original backend design doc but were never built — do not
build extension UI that assumes either exists.

(Amended Phase D session 5 (pending live verification): **citations, statement binding, and
replayable requests.** Every new request field is optional, so older callers keep working.)

**Request additions:**
```json
{
  "question": "What was revenue last quarter?",   // now 1-4000 characters, else 422
  "csv_context_id": null,
  "conversation_id": null,
  "request_id": "<uuid>"   // optional; generated by the caller once per question. The extension always sends it.
}
```

**A conversation is bound to one statement when it's created.** The first turn's
`csv_context_id` (or none, for a ticker-only conversation) is fixed for the life of the
conversation. On later turns:

| Conversation | `csv_context_id` sent | Result |
|---|---|---|
| Bound to X, X still usable | omitted, or X | Answered against X. Omitting it is fine: the backend uses the bound statement. |
| Bound to X | Y, a different id | `409 {"detail": {"error": "statement_mismatch"}}` |
| Bound to X, X deleted or no longer usable | anything | `409 {"detail": {"error": "statement_needs_reconfirm"}}` |
| Not bound (ticker-only) | omitted | Answered with no statement |
| Not bound (ticker-only) | Y | `409 {"detail": {"error": "statement_mismatch"}}` |

Conversations created before session 5 have no recorded binding. Such a conversation is treated
as bound to a deleted statement (`statement_needs_reconfirm`) only if one of its turns, any of
them, actually got statement data back from a statement tool. A ticker-only conversation where
the model looked for a statement and found none stays unbound and keeps working.

The statement is never switched silently. On `statement_mismatch`, start a new conversation
(omit `conversation_id`) for the other statement. Ownership of both the conversation and the
statement is checked on every turn, and a statement you don't own is never used, whatever id
was bound or sent.

**Statements that need re-confirming.** A confirmed statement is usable only if it carries
its stored data, a known `scale`, a `currency` entry (which may be `null`) and a business
name. Statements confirmed before scale and currency existed don't, and get
`409 {"detail": {"error": "statement_needs_reconfirm"}}` instead of an answer. They are never
backfilled. Units are never assumed, so the only fix is to send the rows to `/v1/csv/parse`
again and confirm them.

**Citations.** `citations` now has one entry per figure in `figure_check.figures`, in the same
order:
```json
{"figure_index": 0, "raw_text": "1.25 million", "start": 120, "end": 132,
 "status": "traced",   // "traced" | "ambiguous" | "weak" | "untraced"
 "matches": [
   {"kind": "cell", "value": 1250000, "turn_id": null,
    "source": {"cell": "'P&L'!B4", "concept": "revenue", "column": "Revenue",
               "period_end": "2025-03-31", "read_value": "1250", "sheet_scale": "thousands"}},
   {"kind": "derived", "value": 0.4, "turn_id": null,
    "computation": {"name": "gross_margin", "formula": "gross_profit / revenue",
      "inputs": [{"role": "gross_profit", "value": 500000,
                  "source": {"cell": "'P&L'!C4", "read_value": "500", "...": "..."}},
                 {"role": "revenue", "value": 1250000,
                  "source": {"cell": "'P&L'!B4", "read_value": "1250", "...": "..."}}]}},
   {"kind": "filing", "value": 91000000000, "turn_id": "<uuid>",
    "source": {"concept": "revenue", "tag": "us-gaap:Revenues", "filed": "2025-01-31",
               "period_end": "2024-12-28", "is_derived": false}},
   {"kind": "tool", "value": 1300000, "turn_id": null, "source": {"tool_name": "forecast_metric"}}
 ]}
```
- **`start`/`end`** are character offsets of `raw_text` in `final_answer`.
- **`status`:**
  - `traced`: exactly one distinct source.
  - `ambiguous`: the figure matches two or more different sources, for example the same value
    in two cells. All of them are listed, and none is picked as "the" source.
  - `weak`: only a coincidence-prone whole-number match exists.
  - `untraced`: no match.
  - For `weak` and `untraced`, `matches` is `[]`.
- **`kind`:**
  - `cell`: one spreadsheet cell.
  - `derived`: a ratio computed in Python. `computation.inputs` cites each input's own cell,
    including the prior period's cell for a growth rate and every quarter of a trailing
    twelve-month window. A derived value is never cited as a single cell.
  - `filing`: an SEC EDGAR fact.
  - `tool`: another tool's output with no cell or filing behind it, such as a forecast.
- **`value`** is the number the tool returned, in ones.
- **`read_value`** is the cell's content exactly as it was read and sent to `/v1/csv/parse`
  (the underlying value, not the sheet's formatting), never recomputed. With
  `sheet_scale: "thousands"`, `read_value` `"1250"` and `value` `1250000` are the same cell.
  `read_value`, `column` and `cell` come from the person's spreadsheet, so treat them as
  untrusted text.
- **`turn_id`** is non-null when the matched value came from an earlier turn's tool result.
  Those turns are part of the model's context, up to the last 3.
- **`figure_check`** gains `figures_skipped`: tokens such as a bare `1.25M` that couldn't be
  checked.

**Replayable requests (`request_id`).** A question that takes a long time to answer can outlive
the panel that asked it, so each question carries a `request_id`. Sending the same
`request_id` again **never runs or charges the question twice**:

| State of that `request_id` | Response |
|---|---|
| Never seen | The question runs and is counted once. |
| Still running | `409 {"detail": {"error": "answer_in_progress"}}` |
| Finished | `200` with exactly the stored response (same `turn_id`, `final_answer`, `figure_check`, `citations`). Free, and returned even when the account is at its cap. |
| Failed | `409 {"detail": {"error": "answer_failed"}}`. Ask again with a new `request_id`. |
| Running for over 60 minutes (e.g. a server restart killed it) | `409 {"detail": {"error": "answer_lost"}}`. Ask again with a new `request_id`. |
| Sent before with a different question, `csv_context_id` or `conversation_id` | `422 {"detail": {"error": "request_id_reused"}}`. A bug in the caller; don't retry. |

A `429` doesn't use up the `request_id`. A cap rejection is recorded without the `request_id`
or its fingerprint, because a `429` isn't an attempt that can be replayed. The same id
therefore works again once the cap resets. And if a request with the same `request_id` is
already running when a resend arrives, the resend gets the replay response
(`answer_in_progress`), never a `429`.

**Long answers and recovery.**
- An answer can take several minutes. The extension waits up to 5 minutes on one request.
- **When the wait ends, or the panel reopens with a question pending,** the extension re-sends
  the same body, with the same `request_id`, every 20 seconds while the response is
  `answer_in_progress`.
- **The backend stops a run after a 45-minute budget** with
  `504 {"detail": {"error": "answer_time_budget_exceeded"}}`. Replaying that `request_id` then
  returns `answer_failed`.
- **Only that exact JSON body means a budget stop.** A `502` or `504` with no body, a body that
  isn't JSON, or any other shape comes from a gateway or a server problem (for example Render's
  proxy), not from this backend's decision. The run may still be going, or may have finished,
  so keep the `request_id` and re-send it.

**What counts toward the question caps.**
- **Counted:** every attempt that reaches the model, whether it's answered, stops at the
  iteration cap, fails (`502`/`500`), or is stopped by the time budget. It counts once, when
  it starts, and it counts while it's still running.
- **Not counted:** requests refused before the model runs: `404`, the `409`s above, `422`, and
  `429`. Neither are replays of a finished request.

**New error, generally:** every new error above uses `{"detail": {"error": "<code>"}}`. Branch
on `detail.error`, never on the message text.

### `GET /v1/usage`
Auth required. No request body.

Response (200) — only the fields relevant to the caller's tier are populated, the rest are
`null`:
```json
{
  "account_id": "<uuid string>",
  "tier": "free",                  // "byo_key" | "paid" | "free"
  "questions_today": 2,            // free tier only
  "daily_cap": 5,                  // free tier only
  "questions_this_period": null,   // paid tier only
  "monthly_cap": null,             // paid tier only
  "period_ends_at": null,          // paid tier only
  "byo_key_required": false
}
```
(Amended Phase D session 5 (pending live verification): `questions_today` and
`questions_this_period` now include questions that are still running. A question counts from
the moment it starts, so it's counted while it runs, not only once it's answered. A question
that's later answered, stopped or failed still counts once. Refused requests and replays of a
finished `request_id` never count (see `/v1/ask` above).)

### `POST /v1/byo-key`
Auth required. Registers or rotates the caller's own Anthropic API key. **There is only this one
route** — no GET/status route, no DELETE. Registering a new key always replaces (soft-deactivates)
any currently active one; there is no separate removal endpoint.

Request:
```json
{"api_key": "sk-ant-..."}
```

Response (200): `{"registered": true}`

Error: `422` — `{"detail": "That doesn't look like a valid Anthropic API key."}` if the key
fails a basic format check.

### `POST /v1/billing/checkout-session`
Auth required. **No request body.**

Response (200): `{"checkout_url": "https://checkout.stripe.com/..."}` — open this URL in a new
tab; it's a Stripe-hosted checkout page. Success/cancel redirects land on this backend's own
static confirmation pages (`/v1/billing/success`, `/v1/billing/cancel`), not on anything the
extension needs to handle — actual subscription activation happens asynchronously via Stripe's
webhook, not the redirect, so the extension should not assume the account's tier has updated the
instant the person returns from checkout.

Error: `500` — `{"detail": "STRIPE_PRICE_ID is not configured"}` — a server misconfiguration, not
something the extension can act on beyond surfacing a generic error.

---

## 7. Explicitly out of scope

Named plainly, matching this project's convention of stating exclusions rather than leaving them
implicit:

- The extension's own OAuth consent/sign-in UI, its manifest and scope declarations (beyond the
  identity scopes confirmed in §1), and where in its UI a "sign in with Google/Microsoft"
  affordance lives.
- Chrome Web Store submission, listing, and review-process requirements.
- The not-yet-built Excel/OneDrive Graph API data adapter — nothing beyond the `User.Read`
  identity scope confirmed in §1 is specified anywhere in this codebase for that integration.
  (Amended Phase D session 3a (spike-verified): now specified client-side in `docs/chrome-extension-design.md` §4. It uses `Files.Read`,
  `/shares` resolution, and a file download parsed in the extension. None of it touches this
  backend except the resulting rows posted to `/v1/csv/parse`.)
- The Sheets/Drive data-access OAuth scope needed to actually read/write a Sheet — not named
  anywhere in this codebase (§1); the extension team owns determining it.
  (Amended Phase D session 3a (spike-verified): now named, `spreadsheets.readonly`, requested only by the separate data grant. This
  backend's only involvement is §1a's code exchange.)
- Conversation history listing/browsing and full tool-call-trace-on-demand endpoints — described
  in the original design doc but not implemented (see `/v1/ask`'s note in §6).
- A `/v1/byo-key` removal endpoint — only registration/rotation exists today (see §6).
- Account linking of any kind — a person's Google and Microsoft identities are always separate
  accounts, whatever their emails (§1's cross-provider note). An explicit "link another
  provider" action while signed in is future work, not built.
