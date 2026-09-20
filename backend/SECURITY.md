# Backend security posture — Phase B session 10 hardening pass

This document is the output of Phase B's final session: a real audit (not a
rubber-stamp) of the design doc's §4 ("Security posture") against what the
code actually does, plus the rotation runbooks §4/§6 step 10 call for. It's
organized as: the cross-install (now cross-account) isolation audit, this
project's highest-priority finding — open at the time this file was first
written, **resolved as of 2026-09-20, see §2** — the three rotation runbooks,
the log-content review, and what's checkable about Postgres network access
from this repo alone.

Findings here are honest by design — including status changes recorded
plainly rather than glossed over. Treat this file as a living record: update
it whenever a rotation actually happens (who, when, why) and whenever a
finding's status changes.

---

## 1. Cross-install isolation audit

Every query in `backend/app/main.py`, `gating.py`, `billing.py`, and
`history.py` touching `conversations`, `turns`, `csv_statements`,
`usage_events`, `byo_keys`, `installs`, or `subscriptions` was reviewed.

**Confirmed correct:**
- Every route that accepts a client-supplied ID routes through an ownership
  check before touching the row: `POST /v1/csv/{csv_context_id}/propose-mapping`
  and `.../confirm` (path parameter) call `_get_owned_csv_statement`;
  `POST /v1/ask`'s `conversation_id`/`csv_context_id` body fields call
  `_get_owned_conversation`/`_load_confirmed_csv_statement` before use.
  A row that doesn't exist and a row that exists but isn't owned by the
  caller both 404 identically (anti-enumeration).
- No request schema anywhere (`backend/app/schemas.py`) has an `install_id`
  field — it is always derived from the `X-Install-Id` header via
  `_get_or_create_install`, never accepted from the client body/query string.
- `gating.py` never reads a route parameter or request body directly —
  everything it evaluates comes from the `Install`/`ByoKey`/`Subscription`
  rows already scoped to the header-derived install.
- The Stripe webhook's `client_reference_id → install_id` mapping
  (`billing.py::_handle_checkout_session_completed`) is sourced only from
  Stripe's signature-verified payload, which itself only ever contains what
  our own server set at Checkout Session creation time from an authenticated
  `X-Install-Id`. A caller cannot inject an arbitrary `install_id` into a
  subscription via the webhook.

**Confirmed correct, added Phase C session 6:** `POST /v1/auth/logout` and `POST
/v1/auth/sessions/revoke-all` (`backend/app/main.py`) both accept no client-supplied account or
session identifier at all — the only inputs are the `Authorization: Bearer` token itself and
`get_current_account`'s resolution of it. `revoke-all` scopes its `UPDATE` solely to
`sessions.account_id == account.id`, where `account` comes only from `get_current_account`, so it
cannot be made to touch another account's sessions. `logout` scopes to
`sessions.id == request.state.session_id` (itself set only by `get_current_account`, from the
exact row the presented token validated against — never read from the request body, a header, or
a path parameter) with `account_id == account.id` as a redundant second check, so it can only ever
revoke the row belonging to the token that authenticated the request. Verified directly in
`backend/tests/test_auth_logout.py::test_revoke_all_does_not_touch_other_accounts` (cross-account
isolation) and `::test_logout_revokes_only_presented_session` (same-account, different-session
isolation).

**Worth a defensive tightening (not urgent — not exploitable today):**
- `main.py`'s `ask()` has two queries that rely on an ownership check
  performed earlier in the *same* function rather than re-verifying
  `install_id` at the point of use: the `Turn` query scoped only by
  `conversation_id` (immediately after `_get_owned_conversation` in the same
  function body), and a second-session `Conversation` re-fetch further down
  (which already carries a comment noting the invariant). Safe today because
  no other code path can set `conversation_uuid`, but a future refactor that
  reorders or duplicates this logic could silently reintroduce an IDOR.
  Recommend adding an explicit `if conversation.install_id != x_install_id`
  guard at the second-session re-fetch, and a comment at the `Turn` query
  tying it to the earlier check, next time this code is touched.

---

## 2. `X-Install-Id` had no real authentication

**Status: RESOLVED (2026-09-20, Phase C sessions 3–6).** The rest of this section is left as
written at the time the finding was opened, since this file is a living record, not a rewritten
history — see the resolution note at the end of this section for what actually closed it.

The design doc (§1) describes the auth mechanism as "`X-Install-Id` + ...
session-token pairing." No session-token mechanism exists anywhere in this
codebase. `_get_or_create_install` (`backend/app/main.py`) accepts *any*
UUID a caller presents at face value and silently auto-creates a row for it
— its own docstring calls this a "TEMPORARY shim," and `POST /v1/install`
doesn't persist anything; it just mints a UUID.

Every ownership check audited in §1 above is correctly implemented, but each
one only proves *internal consistency* (row X belongs to install Y) — not
*real authorization* (this caller actually is install Y). Whoever holds an
install's UUID can impersonate it completely: read its conversation history,
spend its BYO Anthropic key, and consume its paid Stripe subscription's
monthly quota, indefinitely, with no rotation or revocation path available to
the real owner.

The only thing standing between this and active abuse today is that
`install_id` is a 128-bit random UUIDv4 — practically unguessable by brute
force. But that means the *client* (the Sheets Add-on sidebar / the planned
Chrome extension) is doing 100% of the real security work by never leaking
its own `install_id`, not the server. Any leak of an install_id — a shared
URL, a browser devtools screenshot, a support ticket pasting a request body,
a future log line — grants indefinite full impersonation with no way for
the real owner to detect or revoke it.

**This has grown more severe since §4 was originally written.** At design
time this was a theoretical identity-model gap over an app with no real
assets behind it. Today, a compromised `install_id` can steal a live paid
subscription's usage or a live BYO Anthropic key — real money and a real,
spendable credential, not just conversation history.

**Decision (made explicitly this session, not deferred by omission):**
leaving this open for the remainder of Phase B is acceptable *only* because
no real users exist yet. It must be designed and built as its own dedicated
session — real session-token issuance at `/v1/install`, storage, and a
rotation/revocation path — **before** the Chrome extension's frontend work
begins, since that frontend's credential-storage design (where the token
lives client-side, how it survives a browser restart, what happens on
logout) depends on this decision. Do not schedule that frontend work ahead
of this session.

**Resolution (2026-09-20, Phase C sessions 3–6):** real session-token authentication now exists
end to end. `POST /v1/auth/exchange` (session 3) verifies the caller's OAuth token directly
against Google/Microsoft and issues a fresh opaque bearer token (`secrets.token_urlsafe(32)`),
persisted only as its SHA-256 hash in a new `sessions` table — the plaintext token is never
stored and is returned to the caller exactly once. `get_current_account` (session 4, retrofitted
onto every authenticated route: `/v1/ask`, `/v1/csv/parse`, `/v1/csv/{id}/propose-mapping`,
`/v1/csv/{id}/confirm`, `/v1/usage`, `/v1/byo-key`, `/v1/billing/checkout-session`) validates that
hash against a live, non-expired, non-revoked `sessions` row on every single request and 401s
identically on any miss (unknown, expired, or revoked token) — `_get_or_create_install`'s
auto-create-any-UUID shim, the actual root cause of this finding, is deleted outright, not
hardened. A working revocation path — the piece this finding always said was required before it
could be called closed — now exists too (session 6): `POST /v1/auth/logout` revokes the exact
session the presented token belongs to (other devices untouched), and `POST
/v1/auth/sessions/revoke-all` revokes every session on the caller's account at once, including
the one making the request, for the compromised-token case where the affected device is unknown.
Both are covered end to end in `backend/tests/test_auth_logout.py` and in
`backend/app/smoke_test.py`'s live walkthrough (issue a token, confirm it works, log out, confirm
the same token now 401s). The original vulnerability this finding described — whoever holds an
install's UUID can impersonate it indefinitely, with no rotation or revocation path available to
the real owner — is closed: an account identifier alone is no longer sufficient to act as that
account, and a leaked token can now be revoked by the real owner instead of remaining valid
indefinitely.

---

## 3. Rotation runbooks

### 3.1 `BYO_KEY_ENCRYPTION_KEY` (Fernet) — hard cutover

`backend/app/crypto.py`'s `_get_fernet()` builds exactly one `Fernet`
instance from exactly one env var value — there is no `MultiFernet`/dual-key
support anywhere in this codebase. **Rotating this key makes every existing
`byo_keys.encrypted_key` row permanently undecryptable under the new key.**
This is a deliberate, accepted trade-off for Phase A (session 10): adding
`MultiFernet`-based dual-key rollover was considered and explicitly not
built this session, in favor of documenting a correct hard-cutover procedure
instead. Revisit this if BYO-key adoption grows enough that forcing mass
re-registration on every rotation becomes unacceptable.

As of this session, a decrypt failure is self-healing rather than a
repeated dead end: `POST /v1/ask`'s BYO-key branch (`main.py`) now
deactivates the affected `byo_keys` row (`is_active = False`) the first time
its ciphertext fails to decrypt, so the *next* request for that install
falls straight through `gating.evaluate_ask_gate`'s existing
`byo_key.is_active` check to the paid/free tier instead of hitting the same
decrypt failure on every call.

**Rotation steps:**
1. Generate a new key:
   ```
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. **Before** flipping the env var, proactively deactivate every currently
   active BYO key row, so affected installs see `byo_key_required` on their
   very next `/v1/usage`/`/v1/ask` call rather than getting one guaranteed
   failed request first:
   ```sql
   UPDATE byo_keys SET is_active = false WHERE is_active = true;
   ```
   Run this against the production database (e.g. via `psql "$DATABASE_URL"`).
3. Set the new value as the `BYO_KEY_ENCRYPTION_KEY` env var on the backend's
   Render service and redeploy.
4. Verify: hit `GET /v1/health` (confirms the service started and can reach
   the DB), then run `backend/app/smoke_test.py` (or a manual
   `POST /v1/byo-key` + `POST /v1/ask` round-trip) against a fresh test
   install to confirm new registrations encrypt/decrypt correctly under the
   new key.
5. Communicate to every affected user (outside this codebase — e.g. an
   in-product banner or email) that their BYO key needs re-registering.
6. **Old ciphertext is not recoverable after this point.** This is the
   accepted consequence of the hard-cutover choice above, not an oversight —
   don't attempt to "fix" a stuck row after the fact by anything other than
   the user re-registering a fresh key.

### 3.2 Database password

1. Rotate the Postgres user's password. If Render's dashboard offers a
   self-service rotate/reset control on the Postgres instance, use it;
   otherwise connect with the current credentials and run:
   ```sql
   ALTER USER <user> WITH PASSWORD '<new-strong-password>';
   ```
2. Update the backend service's `DATABASE_URL` env var on Render with the
   new password embedded in the connection string, and redeploy.
3. Verify via `GET /v1/health`'s `db` field reporting `"ok"`.
4. Confirm no other service or script (e.g. a locally-run
   `backend/db/smoke_test.py`, `backend/scripts/retention_cleanup.py`'s
   Render Cron Job config) still references the old password in its own env
   configuration — update each one before considering the rotation complete.

### 3.3 Stripe `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET`

Stripe supports "rolling" both a secret key and a webhook signing secret
with an overlapping grace period (the old value keeps working briefly),
so this can be done without a hard cutover, unlike the Fernet case above.

**`STRIPE_SECRET_KEY`:**
1. In the Stripe dashboard: Developers → API keys → roll the secret key.
2. Update `STRIPE_SECRET_KEY` on the backend's Render service and redeploy.
3. Verify with a real `POST /v1/billing/checkout-session` call against a
   test install, confirming a valid `checkout_url` comes back.
4. Once confirmed, expire the old key immediately in the Stripe dashboard
   rather than waiting out the grace period.

**`STRIPE_WEBHOOK_SECRET`:**
1. In the Stripe dashboard: Developers → Webhooks → select this backend's
   endpoint → roll the signing secret.
2. Update `STRIPE_WEBHOOK_SECRET` on Render and redeploy.
3. Verify with a test delivery — either `stripe trigger
   checkout.session.completed` (Stripe CLI) or the dashboard's "send test
   webhook" — and confirm the request returns 200 and a new row lands in
   `stripe_webhook_events`.
4. Once confirmed, the old signing secret can be left to expire per Stripe's
   own grace period (no manual step required — Stripe stops offering it as
   valid after the rollover window).

---

## 4. Log-content review

Grepped every `logger.*`/`print(` call across all of `backend/` (all files
added across sessions 3-9, not just this session's) and `src/` (imported by
`main.py`).

- **No raw Anthropic API key (master or BYO), Stripe secret, or database
  connection string/password appears in any log or print statement
  anywhere in this codebase.** `billing.py`, `gating.py`, `history.py`,
  `crypto.py`, `schemas.py`, `db/models.py`, and all of `src/` have zero
  logging calls at all — in particular, the BYO-key encrypt/decrypt path
  and the Stripe webhook handling (§5 below) never log anything.
- `main.py`'s three `logger.exception(...)` calls (in `POST /v1/ask`'s
  `AuthenticationError`/`APIError`/catch-all handlers) log a static message
  plus a traceback. Verified against the installed Anthropic SDK
  (`anthropic/_exceptions.py`) that `APIError`/`AuthenticationError`'s
  `str()` is built purely from the API's own JSON error response body, never
  an echo of the request or its headers — so the traceback can't surface a
  key even though the exception object technically retains the `httpx2.Request`
  (Python's default traceback formatting doesn't dump object attributes).
- As of this session, the two `HTTPException` responses that used to include
  `str(e)` (`anthropic.APIError`, and the generic `run_agent` catch-all) no
  longer do — the caller now gets a static message; the full exception is
  still captured server-side via the existing `logger.exception` call, so
  debuggability isn't lost.
- `backend/app/smoke_test.py` (a manual, dev-only script — not imported by
  the running app, not part of `backend/tests/`) prints full response
  bodies to stdout, including `final_answer` text and error detail. This is
  fine as long as it is never run inside a CI job whose console output is
  retained or shared — **confirm this stays true** if CI is ever set up to
  exercise it.
- `db/base.py`'s engine now sets `hide_parameters=True` (this session) —
  forward-looking: nothing currently configures a real logging handler
  (the app relies on Python's default stderr/WARNING sink), so no active
  exposure exists today, but an uncaught DB error's traceback would
  otherwise have included SQLAlchemy's default compiled-SQL-plus-bind-params
  rendering — for a `Turn` insert, that's raw `question`/`final_answer`
  text — the moment real log aggregation is wired up on Render. Fixed now
  rather than only once that changes.

---

## 5. Stripe webhook payloads specifically

`checkout.session.completed` webhook payloads can carry `customer_email`
per §4's own note. Confirmed: `billing.py::_handle_checkout_session_completed`
never reads or logs `customer_email` at all (only `customer`,
`client_reference_id`, and subscription fields are read off the payload),
and `billing.py` has zero logging calls of any kind. No customer email from
any Stripe webhook is logged anywhere in this codebase today.

---

## 6. Postgres network access

**Not fully checkable from this repository alone** — no `render.yaml` or
Render Blueprint file exists anywhere in this repo, and `db/base.py` reads
`DATABASE_URL` generically with no code-level preference for Render's
Internal vs. External Postgres connection string. Whatever is actually
configured in production lives only in Render's dashboard.

**What to check manually in the Render dashboard:**
1. Open the backend service's environment variables and confirm
   `DATABASE_URL` is set to the Postgres instance's **Internal Database
   URL** (reachable only from within Render's private network), not the
   External Database URL (publicly reachable, secured only by
   password + TLS).
2. Open the Postgres instance's own settings / Access Control page and
   confirm public network access is either disabled entirely, or — if it
   must stay enabled for some external tool (e.g. a local `psql` session
   during development) — restricted to a known, minimal IP allow-list
   rather than left open to `0.0.0.0/0`.
3. Note the Postgres plan tier in use for production (separately from the
   dev/test instance, which `NOTES.md` already documents as free/starter
   tier) and confirm it matches the "paid tier from day one" requirement
   stated in the design doc's §6 session 1.
