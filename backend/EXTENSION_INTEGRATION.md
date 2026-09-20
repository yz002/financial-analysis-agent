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

1. **Obtain an OAuth access token from Google or Microsoft**, client-side, via whatever
   mechanism the extension uses (`chrome.identity.getAuthToken` for Google; the equivalent
   Microsoft identity flow). The token must carry, at minimum:
   - **Google:** the `openid` and `email` scopes. This backend verifies the token by calling
     `GET https://openidconnect.googleapis.com/v1/userinfo` with it — that call only returns a
     usable `email`/`email_verified` pair when those two scopes were granted.
   - **Microsoft:** the `User.Read` scope. This backend verifies the token by calling
     `GET https://graph.microsoft.com/v1.0/me` with it — `User.Read` is required for that call
     to return the `mail` field this backend reads.

   **Not covered here, and not determined by anything in this codebase:** whatever additional
   scope is needed for the extension to actually read/write the person's Sheet or Excel file
   (e.g. a Sheets or Drive scope on the Google side, a Files/Graph scope beyond `User.Read` on
   the Microsoft side). No such scope string appears anywhere in this backend's code or design
   docs — `sheets-backend-design.md`'s identity discussion was written for a Google Workspace
   Add-on (Apps Script), which gets implicit access to its containing Sheet with no explicit
   OAuth scope at all, a different authorization model than a Chrome extension's
   `chrome.identity` flow. The extension team owns determining and requesting that data-access
   scope; this backend never sees or checks it — it only ever verifies the identity scopes above.

2. **Exchange that token for a session token:**

   `POST /v1/auth/exchange` — **no authentication required** (this is the one route a caller
   hits before having a session token at all).

   Request body:
   ```json
   {
     "provider": "google",       // or "microsoft" -- exactly one of these two strings
     "oauth_token": "<the OAuth access token obtained in step 1>"
   }
   ```

   Response body (200):
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

   Error responses:
   - `401` — `{"detail": "OAuth token could not be verified."}` — the provider rejected the
     token (invalid, expired, wrong audience), or, for Google specifically, the account's email
     isn't verified.
   - `422` — `{"detail": "No usable email address is available for this account."}` —
     Microsoft-only case: the account has no `mail` and its `userPrincipalName` isn't
     syntactically an email address either.

   Cross-provider identity note: signing in with the same verified email from a different
   provider (e.g. Google today, Microsoft tomorrow) resolves to the *same* `account_id` — this
   is automatic server-side behavior the extension doesn't need to do anything for.

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
token and re-run the sign-in flow from §1.** Do not attempt to distinguish "expired" from
"revoked" from "never valid" — there is no signal available to do so, and no future version of
this backend is expected to add one.

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

### `POST /v1/csv/parse`
Auth required. Parses spreadsheet cell data into a structured CSV context for later mapping.

Request:
```json
{
  "rows": [["header1", "header2", ...], ["cell", "cell", ...], ...],  // list of list of strings
  "filename": "some-name.csv"
}
```
Every cell must be sent as a **display string**, not a raw typed value — this matters
specifically for date cells, which the backend expects to parse as ordinary date strings, not
as a spreadsheet's internal numeric date-serial format.

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
- The Sheets/Drive data-access OAuth scope needed to actually read/write a Sheet — not named
  anywhere in this codebase (§1); the extension team owns determining it.
- Conversation history listing/browsing and full tool-call-trace-on-demand endpoints — described
  in the original design doc but not implemented (see `/v1/ask`'s note in §6).
- A `/v1/byo-key` removal endpoint — only registration/rotation exists today (see §6).
- Manual account-linking for a person whose Google and Microsoft accounts use different email
  addresses — cross-provider resolution only merges accounts on a matching verified email; there
  is no self-service linking flow for the differing-email case.
