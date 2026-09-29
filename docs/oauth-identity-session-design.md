# OAuth Identity + Session Authentication — Design (Phase C)

## Context

`backend/SECURITY.md`'s open finding (§2) is the reason this session exists: `X-Install-Id`
provides zero real authentication today. `_get_or_create_install` (`backend/app/main.py`)
accepts *any* UUID a caller presents and silently creates a row for it — anyone who learns an
install's UUID (a shared URL, a devtools screenshot, a pasted support-ticket body, a future log
line) can impersonate that install indefinitely: read its conversation history, spend its BYO
Anthropic key, and consume its paid Stripe subscription's monthly quota, with no rotation or
revocation path for the real owner. `SECURITY.md` explicitly blocks starting the Chrome
extension's frontend work until this is designed and built, since the extension's
credential-storage design depends on the answer here.

This design replaces the original `sheets-backend-design.md`'s two-tier identity model
(`identity_type: google_email|uuid`, §2 of that doc) with mandatory OAuth sign-in for every
tier, including free — not new friction, since the extension already requires an OAuth grant
from Google or Microsoft to read the person's Sheet/Excel file in the first place. Identity
reuses that same unavoidable consent step instead of asking twice, and becomes portable across
devices: signing in on a second computer resumes the same account, tier, and history, rather
than minting a second, disconnected install. No real users exist yet (everything in
`installs`/`byo_keys`/`subscriptions` today is Phase B test/seeded data), so this is a clean
redesign of the identity layer, not a live migration.

Confirmed against the real code this session (not assumed): `backend/db/models.py`,
`backend/app/main.py`, `backend/app/gating.py`, `backend/app/billing.py`,
`backend/app/crypto.py`, `backend/app/schemas.py`, and `backend/SECURITY.md` were all read in
full. Current Google/Microsoft OAuth-verification practice was confirmed via fresh web search
this session rather than assumed from training data — see Sources at the end.

---

## 1. OAuth verification flow

New endpoint: **`POST /v1/auth/exchange`** — `{provider: "google"|"microsoft", oauth_token: str}`
→ `{session_token: str, account_id: str, expires_at: str}`. This retires `POST /v1/install`
entirely (today it doesn't persist anything — it just mints an unverified UUID — so nothing of
value is lost).

`provider` is required explicitly, not auto-detected from token shape: a Microsoft Graph access
token is Microsoft's own proprietary/opaque format (Microsoft's own docs are explicit that
third parties should not attempt to parse or locally validate a token whose audience is a
Microsoft-owned API like Graph — "you can't validate tokens for Microsoft Graph according to
[normal validation] rules due to their proprietary format"), and a Google access token obtained
via `chrome.identity.getAuthToken` is likewise opaque from the client's perspective. Neither can
be reliably fingerprinted by shape, so the caller states which provider issued it.

**Google verification:** call `GET https://openidconnect.googleapis.com/v1/userinfo` with
`Authorization: Bearer <oauth_token>` (the current OIDC UserInfo endpoint — confirmed via web
search this session, not `oauth2.googleapis.com/tokeninfo`, which Google's own docs explicitly
say is unsuitable for production: "requests may be throttled or otherwise subject to
intermittent errors... intended solely for development and testing"). A 200 response with
`email_verified: true` yields the verified email; `sub` is the stable per-Google-account
identifier. A non-200 (expired/invalid/wrong-audience token) or `email_verified: false` refuses
the exchange with a 401. **Contract this places on the extension's OAuth request:** the token
must be obtained with `openid` and `email` scopes in addition to whatever Sheets/Drive scope it
already requests — an extension-manifest change, out of scope here (§6), stated as a contract
this backend depends on, matching this project's existing convention of naming a
client-side contract explicitly rather than assuming it (`sheets-backend-design.md` §1 does the
same for the Sheet-date-serialization contract).

**Microsoft verification:** call `GET https://graph.microsoft.com/v1.0/me` with
`Authorization: Bearer <oauth_token>`. Since Graph tokens can't be validated locally (see above),
calling Graph's own endpoint and letting it reject an invalid/expired/wrong-audience token with
401 *is* the verification — the same shape as Google's userinfo call, not a lesser substitute.
A 200 response's `id` field is the stable per-account identifier. For email: prefer `mail`; if
`mail` is null (a real, common case for personal Microsoft accounts and some work/school
tenants — confirmed via web search this session), fall back to `userPrincipalName` only if it is
syntactically an email address (contains `@`, passes a basic format check); otherwise **refuse
the exchange** with a 422 rather than accepting a non-email UPN (a phone number or Skype-style
alias) as someone's "email" — this project's existing "refuse rather than guess" convention
(`statements.py`'s Q4-tiling refusal, `total_liabilities`'s fallback refusal) applied to a new
kind of input. **Contract on the extension's OAuth request:** the token must carry at least
`User.Read` (Microsoft's default profile-read permission, needed for `/me` to return `mail`)
alongside the Excel/OneDrive Graph scope.

Neither call is cached or trusted twice — every `/v1/auth/exchange` call re-verifies with the
provider, so a revoked provider grant is caught at the next sign-in even though it can't be
proactively pushed to us.

## 2. Session token design

**Opaque, server-stored token — not a JWT.** Reasoning: a JWT's main advantage is statelessness
(no DB lookup to validate it), but this design's own requirement — a real, immediate
logout/revoke path for a compromised token (§2's whole reason for existing) — needs a
server-side revocation check on every request regardless of token format. A JWT without a
revocation list is unrevocable before its expiry, which fails that requirement outright; a JWT
*with* a revocation list needs the same DB row a plain opaque token needs, so the "stateless"
benefit evaporates and a JWT would only add signing-key management (yet another secret with its
own rotation runbook, alongside Fernet/Stripe in `SECURITY.md` §3) for no real gain. Postgres is
already this backend's one and only stateful dependency for everything else (`byo_keys`'
`is_active` audit-trail pattern is the direct precedent) — an opaque token backed by a `sessions`
row is the smaller, more consistent design, not a compromise.

**Schema — new `sessions` table:**
`id (PK, UUID)` · `account_id (FK → accounts.id, ondelete=CASCADE)` ·
`token_hash (text, unique, indexed — SHA-256 of the token, never the plaintext, mirroring
why byo_keys.encrypted_key isn't stored as plaintext: a DB dump alone shouldn't hand out live
sessions)` · `created_via_provider (text — "google"|"microsoft", audit/display only)` ·
`created_at` · `last_used_at` · `expires_at` · `revoked_at (nullable)`.
One account can have many concurrent session rows — required for multi-device portability
(signing in on a second computer must not invalidate the first).

**Token format:** `secrets.token_urlsafe(32)` (256 bits), returned to the caller once at
exchange time and never again — same "shown once" pattern `byo-key` registration already
follows for the raw key. The client sends it as `Authorization: Bearer <session_token>` on every
request, replacing `X-Install-Id` everywhere except `/v1/health` and `/v1/billing/webhook`
(unchanged exemptions).

**Validation on every request:** hash the presented token, look up `sessions` where
`token_hash` matches, `revoked_at IS NULL`, and `expires_at > now()`; 401 on any miss (expired,
revoked, or simply unknown — indistinguishable to the caller, matching this codebase's existing
anti-enumeration convention for `csv_context_id`/`conversation_id` lookups). On a hit, update
`last_used_at` and load the associated `Account` row — this becomes the new
`get_current_account` FastAPI dependency, replacing `_get_or_create_install`.

**Lifetime and refresh — a single rolling-idle-timeout token, not an access+refresh pair.**
`expires_at` is set to `now + 90 days` at issuance, and *extended* to `now + 90 days` again on
every successful validation (a sliding window, not an absolute cap) — so a person who opens
their Sheet at least once every ~3 months never has to re-authenticate, matching the "just works
long-term" expectation, while someone who genuinely stops using it for a season lands back at a
real sign-in (which, since the browser typically still holds a valid Google/Microsoft grant, is
often a silent re-consent rather than a full login screen — an extension-UX detail, out of
scope). **Tradeoff stated plainly:** a stolen token that's used periodically stays valid
indefinitely under a pure sliding window, unlike a short-lived access token backed by a
separate refresh token (where a stolen *access* token self-expires in minutes even if never
revoked). A dual-token design is the more defensible pattern for a system with real attackers to
budget for; it's not adopted here because it roughly doubles the client-side state machine (an
extension now has to catch 401s, hold a refresh token, retry) for a $2–10/mo-scale product whose
threat model (per the existing design doc §4 and `SECURITY.md`) is a leaked credential, not a
persistent adversary replaying a live token — and the mitigation for that leaked-credential case
is exactly the revoke path below, not a shorter clock.

**Revocation:**
- `POST /v1/auth/logout` (authenticated) — sets `revoked_at = now()` on the *presented* token's
  session row only. Other devices' sessions are untouched — logging out on one computer must
  not sign the person out everywhere, per the portability requirement.
- `POST /v1/auth/sessions/revoke-all` (authenticated) — sets `revoked_at = now()` on every
  session row for the caller's `account_id`. This is the compromised-token path: if a token
  leaks and the owner doesn't know which device holds it, this invalidates all of them at once,
  forcing every device to re-run `/v1/auth/exchange` (which each can do silently if its
  underlying provider grant is still valid).
- A stale (expired-by-inactivity) token gets a 401 with a clear "session expired, please sign in
  again" body — the extension's job to catch and re-run the exchange, not a distinct error
  class from "revoked" (both are just "this token is no longer good," matching the anti-fishing
  posture used elsewhere in this codebase).

**Client-side storage: `chrome.storage.local`, never `chrome.storage.sync`.** This is a
deliberate distinction, not an oversight: `.sync` piggybacks on Chrome's own account-sync
feature, so a token stored there would ride along to any Chrome profile signed into the same
Google *browser* account — a different and narrower guarantee than the cross-provider,
backend-verified portability this design is actually responsible for (a person could easily use
Chrome signed in with one Google identity while granting the extension a *different* Google or a
Microsoft identity — `.sync`'s guarantee doesn't track that at all). `.sync` also caps item size
and pushes the token through Google's sync infrastructure, an exposure surface this design gets
no benefit from taking on. Real cross-device continuity is achieved the correct way: the second
device re-runs `/v1/auth/exchange` against its own OAuth grant, which resolves back to the same
`account_id` via §3 below — not by syncing the session token itself. `.local` is cleared on
extension uninstall (matching `sessions` rows simply going stale/unused server-side — no
explicit uninstall webhook exists or is needed).

## 3. Cross-provider identity resolution

> **Amended 2026-09-29 (security fix, `backend/SECURITY.md` §7): step 2 below is removed, and
> email never selects an account.** Resolution is now: (1) an exact `(provider,
> provider_subject)` match resolves to its existing account; otherwise (3) a new account is
> created. Each provider identity is its own account, even when two identities (from different
> providers, or two different Microsoft accounts) share an email. Reason: step 2 was an
> nOAuth-class account takeover. The Azure app accepts any Entra tenant, and Microsoft's `mail`
> and `userPrincipalName` are tenant-controlled attributes with no proof of ownership. An
> attacker could create their own tenant, set a user's `mail` to a victim's address, sign in
> with Microsoft, and be attached to the victim's account. It also worked in reverse, with the
> attacker creating the account first so the real owner's later sign-in landed in it. Google's
> `email_verified` check didn't help, since it only covered the Google side of a match.
> Microsoft's own guidance is to identify users by `sub`/`oid`, never by email or UPN. Email is
> still stored (`accounts.primary_email`, `linked_identities.provider_email`) for display and
> Stripe prefill only. Deliberate linking of a second provider is future work (§6). The
> original policy text is kept below for the record.

**Default policy (confirmed with the user, not re-litigated here): same verified email across
providers = same account; different emails = separate accounts, full stop, for Phase C.** No
manual "link my Google and Microsoft accounts" flow is built this session — deferred explicitly
as future work (§6).

Resolution order inside `/v1/auth/exchange`, after §1's provider verification yields
`(provider, provider_subject, provider_email)`:
1. Look up `linked_identities` by `(provider, provider_subject)` — the stable, non-guessable key
   for "have we seen this exact provider account before." A hit resolves directly to that row's
   `account_id`.
2. Else, look up `linked_identities` by `provider_email` alone, across *any* provider. A hit
   means this is a new provider identity for an email we already have an account for (the
   Google-then-Microsoft-with-the-same-email case) — attach a new `linked_identities` row to
   that existing `account_id` rather than creating a second account. This is the entire
   cross-provider merge policy; nothing more sophisticated is built.
3. Else, create a new `accounts` row and a new `linked_identities` row — first time this email
   has been seen from any provider.

`provider_subject` (not email) is the per-provider anti-duplicate key, since a provider account's
email is technically mutable (rare, but Google/Microsoft both allow it) while the subject/`sub`/
Graph `id` is the provider's own stable identifier — using email as the *sole* key in step 1
would risk silently merging two different physical accounts if a provider ever recycled an email
string, which subject-based lookup avoids.

## 4. Schema redesign

`installs` is replaced by three tables. Since no real data exists, this is a clean DDL rewrite,
not a migration with backfill — confirmed against `backend/db/models.py`'s current shape.

**`accounts`** (replaces `installs`):
`id (PK, UUID)` · `primary_email (text, nullable — set once at account creation from whichever
identity created it; informational/Stripe-prefill convenience only, never a security boundary,
so it deliberately isn't kept in sync if a person's provider email later changes)` ·
`byo_key_id (FK → byo_keys.id, use_alter=True — same circular-FK handling as today's
installs.byo_key_id)` · `created_at` · `last_seen_at`.
`identity_type`/`identity_value` and their unique index are dropped outright — every account is
now provider-verified by construction, so the type/value split that existed only to distinguish
a verified email from an unverified UUID has nothing left to distinguish.

**`linked_identities`** (new):
`id (PK, UUID)` · `account_id (FK → accounts.id, ondelete=CASCADE)` ·
`provider (text: "google"|"microsoft")` · `provider_subject (text)` ·
`provider_email (text, indexed — drove §3 step 2's lookup, which was removed 2026-09-29; the
now-unused, non-unique index is left in place since dropping it needs a migration and it's
harmless)` · `created_at`.
`UNIQUE(provider, provider_subject)` — a given provider account can only ever resolve to one
`account_id`.

**`sessions`** — as specified in §2.

**FK rename, mechanical only:** `byo_keys.install_id`, `subscriptions.install_id`,
`csv_statements.install_id`, `conversations.install_id`, `usage_events.install_id` all become
`account_id`, FK'd to `accounts.id` instead of `installs.install_id`. Every column's type,
nullability, and index stays identical — this is a rename-and-repoint, not a redesign of those
tables, confirmed by re-reading each one in `backend/db/models.py` this session.

**Confirmed, not assumed: `gating.py`'s tier logic and the Stripe `client_reference_id` wiring
need only an identifier swap.** `evaluate_ask_gate` (`backend/app/gating.py`) takes an ORM row
and reads `.byo_key_id`/`.install_id` off it — swapping to an `Account` row with `.byo_key_id`/
`.id` is a rename, not a logic change; the BYO-key → paid → free decision order and both cap
constants are untouched. `billing.create_checkout_session` (`backend/app/billing.py`) takes
plain `install_id`/`identity_type`/`identity_value` values, not an ORM object — this actually
*simplifies* slightly: since every account is now OAuth-verified, `identity_type == "google_email"`'s
conditional `customer_email` prefill is no longer conditional — `kwargs["customer_email"] =
account.primary_email` unconditionally (a real account always has one now). `client_reference_id`
is set to `str(account_id)` in place of `str(install_id)`; `_handle_checkout_session_completed`'s
webhook-side read of it is an identical rename. Nothing else in `billing.py` changes.

## 5. Consequences for existing gating logic

Confirmed: **this is purely a swap of "how we know who's asking," not a redesign of the
tiers.** The three-tier decision order (BYO-key unlimited → active-subscription monthly cap →
recurring free daily cap), `FREE_DAILY_CAP`/`PAID_MONTHLY_CAP`, the "past_due/canceled falls
through to free rather than hard-blocking" policy, and `usage_events`' cap-counting rules in
`gating.py` are all unchanged — every one of those functions receives an `Account` instead of an
`Install` and changes nothing about what it computes. The only real behavior change touches
`main.py`'s route layer: `_get_or_create_install`'s auto-create-any-UUID shim is deleted outright
(not "hardened" — deleted), replaced by `get_current_account`, which 401s rather than
autovivifying a row for an unrecognized token. `_get_owned_csv_statement`/`_get_owned_conversation`
keep their exact logic, renamed to compare against `account_id`.

## 6. Explicit out of scope

Named plainly, matching this project's existing design-doc convention:
- The Chrome extension's actual OAuth consent/sign-in UI, its manifest scope declarations, and
  where in its own UI a "sign in with Google/Microsoft" affordance lives.
- Manual account-linking for the differing-email case (§3) — someone who wants their
  Google-identity account and Microsoft-identity account merged despite different emails has no
  self-service path this design builds. (Amended 2026-09-29: since automatic same-email linking
  was removed, this now applies to *every* pair of identities, whatever their emails. The
  future-work shape is an explicit "link another provider" action taken while already signed
  in: it needs an authenticated session plus a fresh sign-in with the second provider, and
  never keys on email. Not built.)
- Whether the Microsoft no-usable-email 422 (§1) should be relaxed — open question, added
  2026-09-29. It was designed while email still fed account resolution (§3 step 2). Now that
  email no longer selects an account, it's only needed for display and Stripe prefill, and
  Stripe Checkout can collect an email itself. Left unchanged for now.
- Any Chrome Web Store submission/review process content.
- Automated compromised-session *detection* (anomalous IP/device alerts, etc.) — only the manual
  revoke path (§2) is built; nothing watches for suspicious use on its own.
- Refreshing the underlying Google/Microsoft OAuth grant itself (silent re-consent, provider
  token expiry handling) — that's `chrome.identity`'s job on the extension side; this backend
  only ever sees a provider token at the single moment it calls `/v1/auth/exchange`.
- A "manage my sessions" UI (list/name/revoke individual devices) — the `revoke-all` and
  single-session `logout` endpoints exist; a listing endpoint is easy to add later on the same
  `sessions` table but isn't built this session.

## 7. Session breakdown

1. **Schema migration.** Hand-written Alembic migration (matching this repo's existing
   convention of a deliberate, non-autogenerated initial migration): drop `installs`, create
   `accounts`/`linked_identities`/`sessions`, rename+repoint the five `install_id` FK columns to
   `account_id`. Update `backend/db/models.py` to match. Smoke-test the connection/schema only.
2. **Provider verification module.** New `backend/app/oauth_providers.py`:
   `verify_google_token`/`verify_microsoft_token`, each returning a shared `ProviderIdentity`
   dataclass (`provider`, `subject`, `email`) or raising a typed refusal (bad token, unverified/
   unusable email). Pure functions wrapping one outbound HTTP call each — testable by mocking
   that call, no DB/FastAPI dependency, mirroring `billing.py`'s existing isolation of pure logic
   from route glue.
3. **`POST /v1/auth/exchange` + session issuance.** The §3 account-resolution order, `sessions`
   row creation, opaque token generation/hashing. Fully testable offline once step 2's provider
   calls are mocked, consistent with this project's existing offline-once-mocked test
   philosophy.
4. **`get_current_account` dependency + retrofit every authenticated route.** Replace
   `x_install_id: uuid.UUID = Header(alias="X-Install-Id")` with the new dependency across
   `/v1/ask`, `/v1/csv/parse`, `/v1/csv/{id}/propose-mapping`, `/v1/csv/{id}/confirm`,
   `/v1/usage`, `/v1/byo-key`, `/v1/billing/checkout-session` in `backend/app/main.py`.
   `_get_owned_csv_statement`/`_get_owned_conversation` get the mechanical `account_id` rename.
   `/v1/health` and `/v1/billing/webhook` are untouched (already exempt). Delete
   `_get_or_create_install` and `POST /v1/install` entirely.
5. **`gating.py`/`billing.py` identifier swap.** Rename `install_id` references to `account_id`
   throughout `evaluate_ask_gate`, `create_checkout_session`, and
   `_handle_checkout_session_completed`; drop the now-unconditional `identity_type` check around
   Stripe's `customer_email` prefill (§4). No change to cap constants or decision order.
6. **Logout/revoke endpoints.** `POST /v1/auth/logout`, `POST /v1/auth/sessions/revoke-all`.
   Update `backend/SECURITY.md`: close out §2's open finding with a dated resolution note (this
   project's own stated practice — "update whenever a rotation actually happens" / whenever a
   finding's status changes), and confirm the cross-install (now cross-account) isolation audit
   in §1 still holds against the renamed columns.
7. **Extension integration contract writeup.** No extension code — a short contract document
   (request/response shapes for `/v1/auth/exchange`, the `Authorization: Bearer` header
   replacing `X-Install-Id`, the required OAuth scopes from §1) so the now-unblocked Chrome
   extension frontend session has exactly what `SECURITY.md` said it was waiting on.

---

## Verification

This produces a design document, not code. Before treating it as final:
- Every schema/endpoint claim above was checked against the real current code this session —
  `backend/db/models.py`, `backend/app/main.py`, `backend/app/gating.py`,
  `backend/app/billing.py`, `backend/app/crypto.py`, `backend/app/schemas.py`,
  `backend/SECURITY.md` — not assumed from the original design doc alone.
- Google's and Microsoft's current OAuth-token-verification practice was confirmed via web
  search this session (Google's `openidconnect.googleapis.com/v1/userinfo` vs. the
  production-unsuitable `tokeninfo` endpoint; Microsoft's stated position that Graph-audience
  tokens can't be locally validated by a third party and must be verified by calling Graph
  itself) rather than assumed from training data, per the brief's explicit instruction.
- §4/§5's claim that `gating.py`/`billing.py` need only an identifier rename, not new logic, was
  checked by reading both files in full rather than assumed from the original design doc.

### Sources
- [Authenticate with a backend server | Google for Developers](https://developers.google.com/identity/sign-in/web/backend-auth) — tokeninfo endpoint explicitly not recommended for production.
- [OpenID Connect | Sign in with Google](https://developers.google.com/identity/openid-connect/openid-connect) — current UserInfo endpoint, required scopes.
- [Access tokens in the Microsoft identity platform | Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens) — Graph-audience tokens are proprietary/opaque to third parties; resource server (Graph itself) must be the one to validate.
