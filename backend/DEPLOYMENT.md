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

## Known risk / backlog: no separate dev/test database

The local backend (`backend/.env`'s `DATABASE_URL`) and the backend test suite both point at
the **production** database (`fin_agent_db`). As a result:

- Every local `pytest` run creates and deletes accounts, sessions, usage events and other rows
  in production. A run that crashes before its cleanup leaves test rows behind.
- Local manual testing, and any local script, reads and writes real production data.
- `NOTES.md` still describes this instance as the "dev/test Render Postgres instance".

A separate dev/test database, with its own `DATABASE_URL` and `BYO_KEY_ENCRYPTION_KEY` in
`backend/.env`, is needed **before real users**.
