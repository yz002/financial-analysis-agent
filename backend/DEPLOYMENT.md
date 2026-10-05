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
| `STRIPE_SECRET_KEY` | Checkout Session creation, subscription lookups | **Deliberately unset** |
| `STRIPE_WEBHOOK_SECRET` | Verifying Stripe webhook signatures | **Deliberately unset** |
| `STRIPE_PRICE_ID` | `/v1/billing/checkout-session` | **Deliberately unset** |

**Finding, 2026-09-29:** production had only `DATABASE_URL`, `ANTHROPIC_API_KEY` and
`SEC_USER_AGENT`. Google sign-in and BYO-key registration could not have worked in production
before then. `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `BYO_KEY_ENCRYPTION_KEY` were added
that day.

**`STRIPE_*` stay unset until Phase D session 6.** They need a production webhook endpoint
registered in the Stripe dashboard (in test mode), whose signing secret becomes
`STRIPE_WEBHOOK_SECRET`. The `whsec_…` secret printed by the local `stripe listen` CLI only
works for local forwarding and must not be used on Render. Until then, billing endpoints fail
at request time in production.

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

## `/v1/ask` timeouts and the per-call log (Phase D session 5, pending live verification)

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
  lines carry metadata only, never question or answer text (`SECURITY.md` §4).

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

Revisions 0001–0003 were applied by hand earlier and weren't recorded here.

## Known risk / backlog: no separate dev/test database

The local backend (`backend/.env`'s `DATABASE_URL`) and the backend test suite both point at
the **production** database (`fin_agent_db`). As a result:

- Every local `pytest` run creates and deletes accounts, sessions, usage events and other rows
  in production. A run that crashes before its cleanup leaves test rows behind.
- Local manual testing, and any local script, reads and writes real production data.

A separate dev/test database, with its own `DATABASE_URL` and `BYO_KEY_ENCRYPTION_KEY` in
`backend/.env`, is needed **before real users**.

## Backlog: CI coverage and dependency audit (review in Phase D session 7)

- **CI runs only the root `pytest` suite.** `.github/workflows/tests.yml` installs
  `requirements-lock.txt`, seeds the EDGAR cache and runs `pytest` from the repo root. It
  doesn't run these, so a push to `main` (which deploys) isn't gated on them:
  - the backend suite (`backend/tests/`), which also needs a database (see the risk above);
  - the extension's `npm run compile` and `npm test`.
  Today they're run by hand before each push.
- **`npm audit` in `extension/` reports 5 high-severity findings, all in dev tooling.** They
  all come through `web-ext` (via `addons-linter`/`image-size` and
  `@devicefarmer/adbkit`/`node-forge`), which is used only for running and packaging builds. None
  is in code the extension ships. SheetJS (`xlsx` 0.20.3, from the `cdn.sheetjs.com` tarball)
  has no findings. Re-check, and update `web-ext`, in session 7.
