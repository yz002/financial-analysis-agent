# Backend deployment reference

The backend runs as a Render web service. **A push to `main` auto-deploys it**, so pushing is
deploying. No secret values belong in this file. `backend/.env.example` documents what each
variable is and how to generate or obtain it.

## Production environment variables

Set these on the **Render web service → Environment**, not on the retention cron job. The cron
job (`scripts/retention_cleanup.py`) is a separate Render resource with its own environment,
and it needs only `DATABASE_URL`.

| Variable | Needed for | Production status (2026-09-29) |
|---|---|---|
| `DATABASE_URL` | Everything | Set |
| `ANTHROPIC_API_KEY` | `/v1/ask` (master key) | Set |
| `SEC_USER_AGENT` | EDGAR tool calls | Set |
| `GOOGLE_CLIENT_ID` | Google sign-in (server-side code exchange) | Added 2026-09-29 |
| `GOOGLE_CLIENT_SECRET` | Google sign-in (server-side code exchange) | Added 2026-09-29 |
| `BYO_KEY_ENCRYPTION_KEY` | Encrypting/decrypting stored BYO Anthropic keys | Added 2026-09-29 |
| `STRIPE_SECRET_KEY` | Checkout Session creation, subscription lookups | Set, **test mode** (`sk_test_`), Phase D session 6; in use 2026-10-20 |
| `STRIPE_WEBHOOK_SECRET` | Verifying Stripe webhook signatures | Set from the dashboard endpoint's signing secret, Phase D session 6 |
| `STRIPE_PRICE_ID` | `/v1/billing/checkout-session` | Set, the test-mode $2/month price, Phase D session 6 |

**Finding, 2026-09-29:** production had only `DATABASE_URL`, `ANTHROPIC_API_KEY` and
`SEC_USER_AGENT`. Google sign-in and BYO-key registration could not have worked in production
before then. `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `BYO_KEY_ENCRYPTION_KEY` were added
that day.

**`STRIPE_*` stayed unset until Phase D session 6** (set since, in test mode; see below). They need a production webhook endpoint
registered in the Stripe dashboard (in test mode), whose signing secret becomes
`STRIPE_WEBHOOK_SECRET`. The `whsec_…` secret printed by the local `stripe listen` CLI only
works for local forwarding and must not be used on Render. Until then, billing endpoints fail
at request time in production.

**Phase D session 6: Stripe is test-mode only.**
- The billing code refuses any `STRIPE_SECRET_KEY` that doesn't start with `sk_test_` (or
  `rk_test_`). The billing routes then return `503 billing_unavailable`, and the rest of the
  service runs normally. *Verified locally* (a dummy `sk_live_` key gave 503 and opened no
  Stripe tab), once an orphan uvicorn process had been stopped (see "Local development").
  Not exercised in production, which runs a test key.
- **Production webhook endpoint (verified 2026-10-20, commit `c5b4474`):**
  - registered in the test-mode sandbox ("Fintel sandbox"), API version `2026-08-26.dahlia`;
  - events `checkout.session.completed`, `customer.subscription.updated`,
    `customer.subscription.deleted` and `invoice.payment_failed`;
  - an unsigned POST returns 400.
- Set the three `STRIPE_*` values on Render only after the session 6 backend is deployed:
  1. Stripe dashboard, **test mode** → Developers → Webhooks → **Add endpoint**
     `https://<render-service-host>/v1/billing/webhook`, with events
     `checkout.session.completed`, `customer.subscription.updated`,
     `customer.subscription.deleted` and `invoice.payment_failed`.
  2. Copy that endpoint's signing secret into `STRIPE_WEBHOOK_SECRET`.
  3. Set `STRIPE_SECRET_KEY` (`sk_test_…`) and `STRIPE_PRICE_ID` (the test-mode $2/month
     `price_…`).
  4. Render redeploys on an env change. Check `/v1/health`.
- The Customer portal must have a saved configuration in test mode (Settings → Billing →
  Customer portal), or `POST /v1/billing/portal-session` fails with `502`.

**Backend test network guard (session 6).** `backend/.env` holds an `sk_test_` key and a
`STRIPE_PRICE_ID` (presence checked 2026-10-08; values not yet checked against the Stripe
dashboard). `backend/tests/conftest.py` therefore fails any test that makes a real Stripe or
Anthropic API call (`backend/tests_support/network_guard.py`). The only exemption is
`test_checkout_session_creation_hits_real_sandbox`, marked `real_stripe`. Session 6's targeted
DB-backed run excludes it:
```
.venv/Scripts/python.exe -m pytest tests/test_billing.py tests/test_tier_gating.py tests/test_byo_key.py -k "not hits_real_sandbox"
```
It runs on its own at live-test step L0, after the key and price are checked in the dashboard.

**Duplicate subscription runbook (session 6).**
- Trigger: a WARNING in the Render logs, `duplicate subscription ignored existing=sub_… new=sub_…`.
  It means two checkouts for one account both completed. The backend kept the existing
  subscription and ignored the new one, but Stripe is still billing both.
- In the Stripe dashboard, open the **new** subscription id, cancel it **immediately**, and
  refund its payment.
- The account's own row is already correct; nothing to change in the database.

Whenever a new variable is added to `.env.example`, add it here and to Render in the same
change.

## Verifying Google OAuth config after a deploy

POST a deliberately fake authorization code to `/v1/auth/exchange`. The backend checks its
Google config before calling Google, so the status code tells you which failed:

```powershell
curl.exe -s -w "`n%{http_code}`n" -X POST "https://<render-service-host>/v1/auth/exchange" -H "Content-Type: application/json" -d '{\"provider\":\"google\",\"code\":\"fake\",\"code_verifier\":\"fake\",\"redirect_uri\":\"https://fake.chromiumapp.org/\"}'
```

- **401** (`"OAuth token could not be verified."`): **configured.** Google received the request
  and rejected the fake code, as expected.
- **500** with `"Google OAuth is not configured."`: `GOOGLE_CLIENT_ID` and/or
  `GOOGLE_CLIENT_SECRET` are missing on the web service.
- **500 with any other body**: something else failed, for example Google was unreachable.
  Check the Render logs.
- **422**: the request body was malformed. Fix the command, not the config.

The fake request fails before account resolution, so it creates no rows.

## `BYO_KEY_ENCRYPTION_KEY`

- **Must be identical locally and on Render while they share a database** (see the risk
  below). Stored BYO keys are Fernet-encrypted with it. A row encrypted by one side with a
  different key can't be decrypted by the other. `.env.example`'s "never reuse the same key
  across environments" assumes separate databases. While one database serves both, local and
  production are a single environment for this key's purposes. Once a separate dev/test
  database exists, give it its own key.
- **Back it up** somewhere outside Render and outside this repo, such as a password manager.
  If it's lost, every stored `byo_keys.encrypted_key` becomes permanently unreadable, and
  every affected user has to re-register their key.
- To rotate it, follow `SECURITY.md` §3.1 (hard cutover).
- **Locally, copy it from Render; never generate a new one** (Phase D session 6). It was
  missing from local `backend/.env` during the session 6 live test.
  - Missing or malformed, the backend now answers BYO requests with `503 byo_unavailable` and
    touches no key.
  - A *valid but different* key is worse: stored keys fail to decrypt and get deactivated
    (`409 byo_key_needs_readd`). The ciphertext is kept, so restoring the right key and
    re-adding the key on the panel recovers, but don't rely on that.

## Local development (Phase D session 6 findings)

- **One uvicorn at a time on port 8000.**
  - The repo lives in OneDrive. WatchFiles (`--reload`) restarts the server whenever OneDrive
    touches a file.
  - Closing a terminal with uvicorn still running leaves an **orphan process listening on
    8000**. Windows lets a second process bind the same port, so a new uvicorn also reports
    startup complete. In session 6, a week-old orphan answered requests with old code.
  - **Stop uvicorn with Ctrl+C only**, never by closing the terminal. Before a live test, check
    for exactly one listener:
    ```powershell
    Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object LocalAddress, OwningProcess
    ```
    Stop any extra process (`Stop-Process -Id <pid>`) before testing.
- **Helper scripts saved inside `backend/` trigger `--reload`**, which restarts the server
  mid-test. Keep one-off scripts outside the repo, for example in a temp folder.
- **The Stripe CLI listener is unreliable on this network.** `stripe listen`'s websocket closes
  (`websocket.Client.writePump … close sent`), and `stripe events resend` doesn't reach a
  restarted listener. Test webhooks against the production endpoint registered in the Stripe
  dashboard (test mode) instead: session 6 live-test steps L4–L8 moved to production P4–P7. As
  of the session 6 local test, the sandbox had no registered webhook endpoints. Session 6's
  webhook verification was done this way, in production, on 2026-10-20.
- **Sandbox cleanup (session 6):** four orphan test-mode subscriptions were canceled in the
  dashboard. One was from Phase B (2026-09-16); three came from local checkouts whose
  webhooks were never delivered, so they had no `subscriptions` row.
- **Leftover test account with a BYO key:** account `51b10836…` (`sub-*@example.com`,
  created 2026-09-19) holds an active *fake* BYO key. It goes with the leftover-test-account
  cleanup (`docs/chrome-extension-design.md` §10 item 13).

## `/v1/ask` timeouts and the per-call log (Phase D session 5, verified locally and in production, commit `aea7b33`)

- **Anthropic client timeout: 120 s per model call**, set explicitly for both the master key and
  BYO keys. The SDK's own default is 600 s, with 2 retries.
- **Run budget: 45 minutes.** `run_agent` checks it before each model call and each tool call.
  When it's exceeded, `/v1/ask` returns
  `504 {"detail": {"error": "answer_time_budget_exceeded"}}`.
- **Stale cutoff: 60 minutes.** A `request_id` still marked in progress after that is reported
  as `answer_lost`. The budget sits below the cutoff so a normal run can't be mistaken for a
  lost one. Render itself allows HTTP requests up to 100 minutes.
- **Per-call duration log.** Each model call logs one INFO line from `src.agent.agent` to
  stderr: iteration, `duration_ms`, stop reason and model. These appear in the Render service's
  logs. Use them to check the 120 s timeout against real call times before changing it. The
  lines carry metadata only, never question or answer text (`SECURITY.md` §4). In the
  session 5 production run the largest model call was 12.1 s.

## Production deploys

- **`46fe4a5`, pushed 2026-10-25 (Phase D session 7 fix round, `2a1e28f..46fe4a5`).**
  Extension-only: no backend file changed after `aaab7bc`, so the backend is the same code. CI
  green on `46fe4a5`, both jobs (pytest and extension). The production re-check is recorded in
  `docs/chrome-extension-design.md` §9 item 7.
- **`aaab7bc`, deployed to Render and confirmed via `/v1/health` on 2026-10-20 (Phase D
  session 7).** The production code for the session 7 QA on 2026-10-25.
  The one backend change since `c5b4474` is `/confirm`'s additive `period_count` (`20aab70`;
  `EXTENSION_INTEGRATION.md` §6). No migration and no env change. `aaab7bc` itself adds the
  extension job to CI.
- **`c5b4474`, verified 2026-10-20 (Phase D session 6).** Stripe in test mode (sandbox "Fintel
  sandbox"), with the webhook endpoint above. No new migrations beyond `0006_usage_event_tier`,
  which was applied before the push (2026-10-08). The production results are recorded in
  `docs/chrome-extension-design.md` §9 item 6.
- **`aea7b33`, 2026-10-06 (Phase D session 5).** `/v1/health` returned
  `{"status":"ok","db":"ok","commit":"aea7b33c7aef255dfc5b852e2e6c0a53fef536ce"}`. No new
  migrations beyond `0005_ask_request_state`, which was applied before the push.
  - **Before the session 6 Stripe work:** delete the `manual_test` `subscriptions` row for
    account `36f87f76-1ff4-4a13-809d-2d54d23b5745`. It raised the owner's question cap for
    the session 5 live tests. Deleting it is a production write, so it needs its own approval.
    (Phase D session 6: 1 row deleted during session 6 L0, by the owner. The date and the
    printed row were not recorded at the time.)
  - **Old BYO ciphertext (session 6, live-test step L0):** before session 6, a replaced BYO key's
    row kept its `encrypted_key`. The new code erases it on removal or replacement (never on a
    decrypt failure), but older rows needed a one-time wipe. The count before, the rows wiped and
    the date were not recorded at the time.

## Database migrations

**Render never runs migrations.** The web service runs `pip install -r requirements.txt` to
build, has no Pre-Deploy command (that setting isn't available on the current plan), and its
Start Command is uvicorn. Nothing in a deploy runs `alembic`. Every migration is applied by
hand.

**`backend/.env`'s `DATABASE_URL` is the production database.** So any `alembic` command run
locally, including `upgrade`, `downgrade` and `stamp`, changes production **immediately**,
before any code is pushed or deployed. See "Known risk / backlog" below.

Procedure for a change that needs a migration:

1. **Only additive migrations run ahead of code**: a new table, column, index or enum value
   that the currently deployed code ignores. Check that the deployed code keeps working once it
   has run. A migration that removes or renames something the deployed code uses needs its own
   plan: deploy code that no longer depends on it first, then migrate.
2. **Get explicit approval**, then run it once, from `backend/`, **before pushing** the code
   that depends on it:
   ```
   .venv/Scripts/alembic.exe current
   .venv/Scripts/alembic.exe upgrade head
   .venv/Scripts/alembic.exe current
   ```
3. **Verify the change itself**, not only the revision. For an enum value, for example:
   `SELECT unnest(enum_range(NULL::usage_event_outcome))`.
4. **Record the applied revision** in the log below.
5. Then run the backend tests and push.

**Pre-push checklist:** Does this push need a migration? Has it been applied?

### Applied revisions

| Revision | Applied to production | Notes |
|---|---|---|
| `0004_mapping_proposal_outcome` | 2026-10-04 | Adds `usage_event_outcome` value `mapping_proposal` (Phase D session 4). Additive; applied before the code that writes it. Verified with `enum_range`. |
| `0005_ask_request_state` | 2026-10-04 | Phase D session 5. Additive. Applied manually from a single head (`0004_mapping_proposal_outcome -> 0005_ask_request_state`), before the code that uses it. Verified read-only on Postgres 18.6. `enum_range` now ends in `in_progress`. New nullable columns: `conversations.bound_csv_context_id` (uuid), `turns.citations` (jsonb), `usage_events.request_fingerprint` (text), `usage_events.request_id` (uuid). New index: `uq_usage_events_account_request` UNIQUE on `(account_id, request_id) WHERE request_id IS NOT NULL`. Post-migration production check, with the old code still deployed: passed. A mapping proposal loaded normally: `'P&L'!A3:G7` read, Send to analysis, and the proposal appeared. |

| `0006_usage_event_tier` | 2026-10-08 | Phase D session 6. Additive. Applied manually from a single head (`0005_ask_request_state -> 0006_usage_event_tier`), before the code that uses it. Verified read-only: `alembic_version` is `0006_usage_event_tier`; `usage_events.tier` is `text`, nullable; `ck_usage_events_tier` = `CHECK ((tier = ANY (ARRAY['free'::text, 'paid'::text, 'byo_key'::text])))`; 0 rows with `tier IS NOT NULL`. Post-migration production check, with the old code (`795ec9f`) still deployed: passed. A question in the production extension was answered normally. |

Revisions 0001–0003 were applied by hand earlier and weren't recorded here.

## Known risk / backlog: no separate dev/test database

The local backend (`backend/.env`'s `DATABASE_URL`) and the backend test suite both point at
the **production** database (`fin_agent_db`). As a result:

- Every local `pytest` run creates and deletes accounts, sessions, usage events and other rows
  in production. A run that crashes before its cleanup leaves test rows behind.
- Local manual testing, and any local script, reads and writes real production data.

A separate dev/test database, with its own `DATABASE_URL` and `BYO_KEY_ENCRYPTION_KEY` in
`backend/.env`, is needed **before real users**.

## Backlog: CI coverage and dependency audit (reviewed in Phase D session 7)

- **CI coverage.** `.github/workflows/tests.yml` has two jobs:
  - the root `pytest` suite (installs `requirements-lock.txt` and seeds the EDGAR cache);
  - since `aaab7bc` (session 7), the extension: `npm ci`, `npm run compile` and `npm test` in
    `extension/`.
  - **Still not in CI:** the backend suite (`backend/tests/`). It needs a database, and today
    that's production (see the risk above), so it's run by hand before each backend push.
- **`npm audit` in `extension/`: fixed in `641de9c` (session 8), except the node-forge chain.**
  - 2026-10-20 there were 8 findings. By session 8 (2026-10-25) there were 13 (3 critical,
    7 high, 3 moderate), all in dev tooling.
  - **Cleared by plain `npm audit fix`** (never `--force`), lockfile only: shell-quote
    (critical), vite, source-map-js, undici, image-size/addons-linter, fx-runner,
    `@vitest/mocker` and fast-uri. vite 8.3.0 → 8.3.5, vitest 5.0.1 → 5.0.5 and web-ext
    10.6.0 → 10.8.0, all within their existing ranges. Transitively, `addons-scanner-utils`
    went 15 → 16 (semver-major). `npm run compile`, `npm test`, `npm run build:local` and
    `npm run build` all passed.
  - **Accepted risk: 4 high findings remain**, all one chain: node-forge →
    `@devicefarmer/adbkit` → web-ext → wxt.
    - node-forge has no fixed version (every version is affected). npm's only offer is
      `--force` to web-ext 5.1.0, which wxt (needs web-ext >=9.2.0) can't use.
    - It's dev-only. adbkit is web-ext's Firefox-for-Android runner, which `wxt build` doesn't
      use. None of it is in code the extension ships.
    - **Re-check each session** with `npm audit` in `extension/`, and clear it once node-forge
      or adbkit ships a fix.
  - **Runtime dependencies are clean:** `npm audit --omit=dev` = 0 vulnerabilities. That covers
    SheetJS (`xlsx` 0.20.3, from the `cdn.sheetjs.com` tarball).
