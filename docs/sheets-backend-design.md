Sheets Add-on Backend — Architecture Design (Phase A)

Context

The project currently has one front door: a local, single-user Streamlit app
(src/app/main.py) that calls run_agent and the CSV-upload pipeline directly
in-process. This session designs a second, separate front door: a hosted FastAPI
service that a Google Workspace Add-on for Sheets talks to over HTTP, so someone can
ask plain-English questions about their currently-open spreadsheet the same way the
Streamlit app already lets them ask about an uploaded CSV or a ticker.

This is Phase A — architecture and policy only, no implementation. The goal is to
validate the already-decided direction (thin FastAPI wrapper, Sheet data flowing
through the existing CSV pipeline, a 7-day free tier with a daily cap and BYO-key
fallback, persistent history, Render hosting with a paid DB from day one) against what
the real code actually does, and to name every place — explicitly, not by omission —
where the existing single-process/single-session code doesn't cleanly extend to a
real multi-tenant hosted service.

Two scope questions were resolved with the user before finalizing:
- run_agent may be changed to accept a prior_messages parameter, so stored
  conversation history can seed a follow-up question with raw prior turns (not a
  model-generated summary — see §3.2 for why that distinction is load-bearing, not
  stylistic). This is the one place this design touches core reasoning-loop code,
  not just new backend plumbing, and is accepted as in-scope.
- /v1/ask runs synchronously (FastAPI sync def, threadpool-dispatched) rather
  than as a background job with client-side polling. Simpler for Phase A; the
  response shape reserves a turn_id today so a 202+polling mode can be added later
  without a breaking change, if Render's real request-timeout behavior ever requires it.
  Because everything from history to rate-limiting to retention is designed on top of
  this assumption holding, the real-proxy load test that checks it is sequenced
  immediately after the FastAPI skeleton stands up (Phase B session 3), not at the
  end of the build — see §6. If it doesn't hold, the design pivots to the reserved
  202+polling shape before any of that later work is built on a broken assumption.

---

1. Backend API shape

All endpoints live under a new FastAPI service, versioned path prefix /v1, JSON
over HTTPS. Every endpoint except /v1/health requires an X-Install-Id header and,
once a BYO key exists, an Authorization: Bearer <session-token> (never the raw key
itself — see §4). One further exception, added by the monetization amendment (§7):
POST /v1/billing/webhook is called directly by Stripe and carries no X-Install-Id —
it authenticates via Stripe's own signature header instead (§7.4). Called out here
explicitly since it deliberately breaks the rule just stated, not an oversight.

Endpoints

- POST /v1/ask — {question, csv_context_id?, conversation_id?} →
  {conversation_id, turn_id, final_answer, hit_iteration_cap, figure_check, citations, tool_calls_summary}.
  citations is derived server-side from tool_calls' embedded provenance
  (tag/filed/source_row/source_column, already present in each tool's JSON
  result — see src/agent/tools.py) — run_agent's actual return dict has no
  citations field today, so this is new backend logic, not a passthrough.
  tool_calls_summary strips input/result payload bulk to {tool_name, is_error}
  for the sidebar's compact trace view; GET /v1/turns/{turn_id} returns the full
  trace on demand.
- POST /v1/csv/parse — Sheet range's cell data (+ synthetic filename) →
  {csv_context_id, columns, sample_rows, parse_error?}. Mirrors parse_csv's
  structural refusals (bad encoding, no header, zero data rows, duplicate headers).
- POST /v1/csv/{csv_context_id}/propose-mapping — calls propose_mapping →
  {proposal: [{csv_column, proposed_role, rationale}], note?}.
- POST /v1/csv/{csv_context_id}/confirm — {mapping, entity_name} → runs
  validate_mapping then normalize → {confirmed, cadence?, warnings, concepts_unavailable}
  or {confirmed: false, errors, warnings}.
- GET /v1/conversations/{conversation_id} — paginated turn history for the sidebar.
- GET /v1/usage — response shape superseded by the monetization amendment (§7.2):
  {install_id, tier, questions_today, daily_cap, questions_this_period, monthly_cap,
  period_ends_at, byo_key_required} — only the fields for the caller's actual tier
  are populated.
- POST /v1/byo-key — registers/replaces a user's Anthropic API key.
- POST /v1/billing/checkout-session, POST /v1/billing/webhook — added by the
  monetization amendment, see §7.1.

Why the mapping flow is 3+ endpoints, not 1

propose_mapping's own docstring is explicit that its output isn't authoritative —
a human reviews/overrides every proposed role before anything normalizes. Collapsing
propose+confirm into one call would remove the only point a human edits the mapping,
breaking the human-on-the-loop invariant, not just convenience. Since HTTP is
stateless per call, the backend hands back a csv_context_id after /parse that the
sidebar re-attaches to propose-mapping and confirm; the backend needs somewhere
to park the unconfirmed RawCsv between those calls (short TTL, ~1 hour,
purgeable — disposable working state, not part of the durable per-install archive).

Sheet-range → RawCsv adapter

Decision: build RawCsv directly from the Sheet's 2D array
(pd.DataFrame(rows[1:], columns=rows[0])), not round-tripped through CSV text via
parse_csv. parse_csv is fundamentally a bytes/text-in function (decodes
utf-8-sig, feeds csv.reader/pd.read_csv) built for arbitrary uploaded files;
re-serializing already-typed Apps Script values back to CSV text just to re-parse
them would risk lossy formatting round-trips and runs an encoding-detection codepath
against data that was never bytes on disk. RawCsv itself is a thin dataclass
(df, filename, uploaded_at) with no hidden state — propose_mapping/
validate_mapping/normalize only ever touch those three fields. So the adapter
(new module, src/data/sheet_ingest.py::rows_to_raw_csv) duplicates parse_csv's
three structural checks (duplicate headers via the same Counter approach, empty
header, zero data rows) directly against the Sheet's header/data rows, then
hand-builds RawCsv — mirroring parse_csv's (RawCsv, None) | (None, reason)
refusal contract exactly, so /v1/csv/parse is a thin wrapper regardless of which
ingestion path fed it.

One Sheets-specific concern with no CSV analog: Apps Script date cells serialize
either as ISO strings or as Sheets' internal date-serial numbers depending on how
getValues()/getDisplayValues() is called. The interface contract between Apps
Script and /v1/csv/parse must require display-string values, not raw typed
values, for the period-date column specifically — so pd.to_datetime in
normalize() sees the same kind of string it would from a real uploaded CSV, not a
numeric serial needing Sheets' 1899-12-30 epoch correction reproduced server-side.
(The Apps Script side of this is out of scope per §5; stated here as a contract the
backend's adapter documents and expects.)
(Amended Phase D session 3b: for the Chrome extension, this contract is refined in
`backend/EXTENSION_INTEGRATION.md` §6. Dates are sent as ISO-8601, numbers as their
underlying value, and an optional `source` lets cited figures name their cell. It also adds
size limits and an empty-header refusal. A date is still never sent as a serial.)

Running run_agent: sync def, FastAPI's threadpool

run_agent is fully synchronous (blocking anthropic.Anthropic().messages.create
in a loop up to 8 rounds, 30+ seconds typical). A sync def route handler is
automatically dispatched to Starlette's threadpool by FastAPI, so nothing in
agent.py changes and the event loop isn't blocked. Confirmed with the user as the
Phase A choice over background-task+polling (see Context above) — Render's default
proxy timeout is expected to comfortably exceed 30s, but this is an assumption
verified empirically, early, against Render's real deployed proxy (Phase B session 3
below, sequenced right after the FastAPI skeleton and before any other work is built
on top of it), not something this design merely asserts. max_iterations (currently 8, per
agent.py's DEFAULT_MAX_ITERATIONS) is a candidate to lower specifically for the
hosted service, to bound worst-case request latency more tightly than Streamlit's
no-timeout-pressure deployment needs.

Replacing csv_session.py's global registry — cannot carry over as-is

Flagging explicitly, as required. csv_session.py today is a literal
module-global pd.DataFrame | None, and get_csv_statement/get_csv_ratios take
no identifier argument at all — the single-active-CSV assumption is baked into
the tool signatures and the system prompt ("there is no ticker or other identifier
to pass"), not just the storage layer. Under concurrent multi-install requests this
is a straightforward correctness bug: install A's CSV could be read by install B's
concurrent tool call, or B's set_active_csv could stomp A's mid-flight request.
This is strictly worse than the existing module docstring's stated risk ("two people
hitting the same running process"), because a hosted backend guarantees that
concurrency, where Streamlit's local single-user deployment mostly doesn't.

Fix: reimplement csv_session.py internally with a contextvars.ContextVar,
keeping its public set_active_csv/get_active_csv signatures unchanged. A
ContextVar is coroutine/thread-safe by construction — each request's context is
isolated even when multiple requests run concurrently in the same process/threadpool
— so /v1/ask's handler does token = _active_csv_var.set(df) after loading that
install's confirmed statement from the DB, and _active_csv_var.reset(token) in a
finally. This is the smallest fix that actually closes the race: it requires zero
change to tools.py's dispatch contract or the model-facing tool schema (the
model's tool-call JSON still never carries a session id), only a few lines inside
csv_session.py itself. Rejected alternative: a {install_id: df} module-level
dict — tempting, but reintroduces the same class of bug (a worker restart loses it;
nothing guarantees perfectly reliable per-request cleanup the way ContextVar's
reset-in-finally does).

What the DB holds for the CSV path

Two lifetimes: (a) an unconfirmed RawCsv/proposal, short-TTL disposable state
between /parse and /confirm; (b) the confirmed, normalized statement, which
becomes durable per-install data once confirmed (matching the existing Streamlit UX
where a confirmed CSV stays active without re-uploading). Store the normalized
DataFrame as JSON (records-oriented) plus its df.attrs (needed for
_csv_citation's source_row/source_column) in a csv_statements table — simpler
and more portable on Postgres than pickling a DataFrame; reconstructing via
pd.DataFrame.from_records plus reattaching .attrs on load is cheap.

---

2. Free-tier / rate-limiting / identity design

Identity: Google email scope first, UUID fallback — both designed, not just the happy path

Primary: request the userinfo.email OAuth scope in the Add-on manifest; Apps
Script attempts Session.getActiveUser().getEmail() on first run and, if non-empty,
sends {"identity_type": "google_email", "identity_value": email} to /v1/install.

Documented failure mode that must be designed around: this reliably returns a
real address only when the Workspace domain's admin allows script-visible user
identity. A consumer @gmail.com user — likely this Add-on's largest audience,
individual analysts and small-business owners on personal accounts — gets an empty
string. This is a common outcome, not an edge case, and the fallback must be
concrete:

1. Empty email → Apps Script generates a UUID and stores it in
   PropertiesService.getUserProperties() (per-user, tied to the Google account for
   that script, travels across any Sheet the user opens the Add-on in — sturdier
   than browser-local storage, though still resettable on uninstall/explicit clear).
   Sends {"identity_type": "uuid", "identity_value": uuid}.
2. Backend stores identity_type alongside the value — an email-verified identity is
   a stronger anti-abuse signal than a UUID (harder to mint many of), leaving room to
   tune cap leniency later if abuse concentrates in UUID installs; not a Phase A requirement.
3. An empty-string email must never be silently treated as "no identity" / no
   limits. If both the email read and the UUID fallback somehow fail, /v1/ask
   refuses outright rather than proceeding unmetered.

Superseded by the monetization amendment (§7.2) — the model is no longer a 7-day
window followed by required BYO-key; it's three tiers (BYO-key, paid subscription,
recurring free daily cap), with BYO-key still skipping every check exactly as it did
here. See §7.2 for the current decision-order pseudocode and the current default
constants; nothing below this point in §2 describes a time-limited window anymore.

If an install never subscribes or adds a BYO key

History stops accumulating once the free tier's recurring daily cap is hit for the
day with no active paid subscription (§7.2) and resumes automatically at the next
UTC day — not a one-time expiry anymore — but existing history stays intact and
readable regardless. Deleting a user's own past answers as a consequence of a
billing prompt would be a surprising, punitive UX for what's framed as an upgrade
nudge, not an account purge. /v1/conversations/* and /v1/usage stay fully
functional read-only regardless of tier; only /v1/ask (anything that spends API
budget) is gated. This does mean a never-upgraded install's history sits in storage
indefinitely at zero revenue — addressed by the inactivity-based retention policy in
§3.4, not by tying deletion to free-tier status.

Usage-tracking schema

installs: install_id (PK, UUID) · identity_type (google_email|uuid) ·
identity_value (unique index on (identity_type, identity_value)) ·
byo_key_id (FK, nullable) · created_at · last_seen_at. (The monetization
amendment, §7.3, adds a separate subscriptions table rather than a column here —
see §7.3 for why. The free_window_started_at column from the original 7-day-window
design is dropped; nothing replaces it.)

usage_events: id (PK) · install_id (FK) · occurred_at (indexed — drives
the daily-cap query, and, per §7.2, the paid tier's monthly-cap query against
subscriptions.current_period_start/end) · turn_id (FK, nullable if rejected
pre-flight) · outcome (answered|hit_iteration_cap|error|rejected_daily_cap|
rejected_monthly_cap). Cap count excludes rejected_* outcomes (a rejection
shouldn't count against the cap it just enforced); those rows remain useful
abuse-pattern telemetry.

---

3. Conversation history design

Storage schema

conversations: id (PK) · install_id (FK) · title (nullable, first
question truncated) · csv_context_id (FK, nullable) · created_at · last_turn_at.

turns — maps directly onto run_agent's actual returned dict, adding nothing
it doesn't produce: id (PK, = the turn_id returned by /v1/ask) ·
conversation_id (FK) · question · final_answer · hit_iteration_cap ·
iterations_used · stop_reason · figure_check (jsonb, verbatim) · tool_calls
(jsonb, verbatim — each {iteration, tool_name, tool_input, tool_result, is_error})
· model · created_at. Storing tool_calls in full (not reduced) is deliberate:
it's the only place the "every number carries provenance" invariant's evidence
actually lives — collapsing it would make later citation review or figure_check
re-verification impossible.

How much prior history feeds a new question

None of "all of it," "a model-generated summary," or "last N turns as prose" is
safe to bolt on purely at the backend layer — run_agent has no concept of prior
turns today (messages is always hard-seeded as exactly one user turn), so
supporting this requires the accepted agent.py change: an additive
prior_messages: list[dict] | None = None parameter that, when given, seeds
messages = prior_messages + [{"role": "user", "content": question}] instead of
starting fresh. Backward-compatible — Streamlit's existing zero-argument calls are
unaffected.

The content fed back must be raw prior turns (question + final_answer + the
actual assistant tool_use/tool_result blocks), never a model-generated summary — a
direct, non-negotiable consequence of the no-arithmetic principle, not a style
choice. Summarizing necessarily asks the model to restate prior figures in its own
words to compress them; the moment a number leaves its original tool-call JSON and
becomes prose in a "here's what we discussed" digest, it's indistinguishable in the
next turn's context from a model-recalled fact rather than a tool-sourced one —
exactly what guardrails.check_figures exists to catch on the current turn's
answer, but with zero visibility into a number quietly reintroduced via a stale
summary from an earlier turn. So: reconstruct the actual Anthropic-API-shaped
message blocks from a stored turn's tool_calls field (rebuilding the assistant
tool_use content and user tool_result content exactly as run_agent's loop produced
them), not a natural-language digest.

Bound by recency, not by degrading fidelity. Recommend the last N=3 prior
turns, reconstructed verbatim, dropped once older — matching tools.py's own
existing bounding philosophy (MAX_PERIODS, MAX_DAILY_PRICE_ROWS: bound scope by
truncating how much is included, never by degrading the fidelity of what's kept).
N=3 is a starting tuning knob to revisit once real token costs from live multi-turn
use are observed, not a hard constant.

Sidebar's "review past conversations" view

GET /v1/conversations?install_id=... lists conversations (title = first question,
last_turn_at); GET /v1/conversations/{id} returns its turns in order
(question/final_answer plus a collapsed tool-call summary with expand-for-full-trace,
matching /v1/ask's own response shape). Read-only browsing — no editing a past
turn, only asking a new one, optionally continuing the same conversation_id.

Retention policy: capped, not unbounded — driven by the Render paid-DB cost constraint

Conversations (and their turns) for an install with no activity for 12 months are
hard-deleted — both hidden from the app and physically purged from Postgres.
Two independent reasons: storage cost scales with install count × time on a paid-tier
DB with a real per-GB cost, so unbounded retention is a direct, compounding
operating-cost liability at this budget; and the retained content is explicitly real
business detail (per the original brief's own framing), so bounded retention is also
the more conservative security/privacy posture, not just a cost one — less data at
rest is strictly less exposure. A 12-month inactivity window (not a fixed
delete-after-90-days-regardless-of-use window) means an actively-used install's
history is never pruned mid-use — only genuinely dormant installs lose their archive,
and only after a full year of silence. Stated as a deliberate trade-off: not every
product would choose deletion here, but it's the right one given the named cost
constraint and the sensitivity of what's stored.

---

4. Security posture

Scoped to a real threat model for a $7/mo Render Starter deployment, not an
enterprise multi-tenant platform: the realistic risks are a leaked connection string
or env var, a SQL-injection/IDOR bug exposing one install's rows to another, a
database backup being exfiltrated, or Render's own infrastructure being compromised —
not a nation-state adversary or a dedicated red team. The choices below are sized to
that.

- Server-side master Anthropic key — never touches the database. Lives only as a
  Render environment variable, injected at deploy time, never logged, never returned
  in any response — the same .env/python-dotenv convention the project already
  uses, just relocated from a local file to Render's env-var store. No new mechanism.
  Non-negotiable, separate from key-leak protection: a hard spend cap / budget
  alert must be set on the Anthropic account backing this key before it is ever used
  against a live deployment — the free-tier/daily-cap logic in §2 bounds a
  well-behaved client's usage, but it's an application-layer control the master
  key itself has no awareness of; a bug in that gating logic, a misconfigured
  install, or a leaked key would otherwise be bounded only by whatever limit the
  Anthropic account itself enforces. This is a named, checkable step in the build
  plan (§6, session 3), not an assumption folded into the free-tier design.
- BYO API key encryption at rest — Python's cryptography library's
  Fernet (AES-128-CBC + HMAC, symmetric, authenticated), with the Fernet key
  itself stored as a Render env var (BYO_KEY_ENCRYPTION_KEY), separate from the
  ciphertext in Postgres — a leaked DB backup alone doesn't expose plaintext keys,
  since the decryption key lives in a different secret store entirely. Chosen over a
  KMS-backed approach (AWS/GCP KMS): a KMS adds a second cloud vendor relationship,
  IAM configuration, and per-decrypt latency/cost not justified at this scale or
  budget — worth reconsidering only if this ever grows into a genuinely enterprise
  offering with compliance requirements that specifically mandate it, out of scope
  for Phase A. Schema: byo_keys(id, install_id, encrypted_key bytea, created_at, last_used_at, is_active) — is_active rather than hard-delete on rotation, so a
  compromised-key incident has an audit trail.
- Conversation history at rest — TLS in transit (Render's default, not a new
  decision) plus Render's provider-level Postgres encryption at rest, rather than
  application-layer field encryption on every question/final_answer/tool_calls
  column. Field-level encryption would break server-side use of that data (the §3.4
  retention-expiry query, any future search feature) without decrypting row-by-row
  first — disproportionate complexity for this deployment's actual risk, since a
  DB-dump scenario is already mitigated by provider-level encryption plus access
  controls. Stated plainly as a real trade-off: provider-level encryption protects
  against physical/storage-layer compromise, not an attacker holding valid DB
  credentials — for that, the mitigation is credential hygiene (unique, rotated DB
  password; Postgres network access restricted to the app service only), which is
  the proportionate answer here, not a gap being glossed over.
- Cross-install data isolation — the actual highest-value control. Every query
  touching conversations/turns/csv_statements/usage_events/byo_keys filters
  by install_id from the authenticated request context (the X-Install-Id +
  session-token pairing), never a client-supplied install_id trusted on its own —
  this is an IDOR risk, and is a code-review-time discipline (every query function
  takes an authenticated install_id) more than a cryptographic one.

---

5. Explicit out-of-scope

Stated plainly, not by omission:
- The actual Apps Script sidebar UI implementation and its manifest
  (appsscript.json, OAuth scope declarations, HTML/CSS/JS) — this design specifies
  the HTTP contract and identity/data contracts it must honor, not its implementation.
- Google Workspace Marketplace publishing, listing, and review-process requirements.
- Any Excel/Office equivalent adapter — a distinct future integration.
- Any change to the existing Streamlit app (src/app/main.py) — it continues to run
  exactly as it does today, unmodified, as a separate, parallel front door. The two
  share src/agent/src/analysis/src/data but have no runtime dependency on each
  other.

---

6. Phase B session breakdown

1. Data models + migrations. Stand up Postgres on a paid tier from day one
   (never Render's free tier, including for this session's own testing — per the
   hard constraint). Write the schema from §2/§3/§4 (installs, usage_events,
   conversations, turns, csv_statements, byo_keys) via Alembic. Smoke-test
   the connection only — no API code yet.
2. FastAPI skeleton. Add fastapi, uvicorn, a Postgres driver/ORM to
   requirements.txt. Stand up /v1/health and the route/Pydantic-model shape
   (stubbed responses) for /v1/ask, /v1/csv/parse, /v1/csv/{id}/propose-mapping,
   /v1/csv/{id}/confirm, /v1/install, /v1/usage. Deploy the skeleton to
   Render's free app-service tier for dev iteration (fine, since it holds no
   persistent state itself — only the DB is excluded from the free tier).
3. Spend-cap gate, minimal run_agent wiring, and Render timeout validation —
   deliberately narrow-scoped and run before any other backend work. In order:
   (a) Set a hard spend cap / budget alert on the Anthropic account backing the
   master key — non-negotiable (§4), done first, before this session's own load
   test generates real spend against that key; (b) wire /v1/ask to call
   run_agent(question) synchronously against ticker-only questions using the
   master key, and persist the resulting turns row — no CSV, no history, no
   free-tier logic yet; (c) deploy to Render and run a synthetic worst-case load
   test against the real deployed proxy, forcing max_iterations=8 (the
   longest a single request can legitimately run), confirming no timeout
   truncation; (d) explicit decision gate: if the synchronous approach holds
   under (c), proceed with the rest of this session list as designed; if it
   doesn't, pivot immediately to activating the reserved 202+polling response
   shape (§1) before building anything further on top of it. This is sequenced
   immediately after the skeleton and before history/CSV/free-tier/retention work
   specifically so a broken foundational assumption is caught before 7+ more
   sessions are built on top of it, not after.
4. Sheet-range → RawCsv adapter + CSV pipeline endpoints. Build
   src/data/sheet_ingest.py::rows_to_raw_csv; wire /v1/csv/parse →
   /propose-mapping → /confirm against the existing csv_ingest/csv_statement
   functions unchanged. Store confirmed statements in csv_statements.
5. csv_session.py concurrency fix + CSV integration in /v1/ask. Implement
   the ContextVar reimplementation; wire /v1/ask's csv_context_id to load a
   confirmed statement into it before calling run_agent. Test explicitly for the
   race this fixes — two concurrent /v1/ask calls for two different installs'
   CSVs, asserting no cross-contamination.
6. run_agent multi-turn parameter + history seeding. Add prior_messages to
   agent.py as its own reviewable change with its own tests (confirming
   Streamlit's zero-argument call sites are unaffected). Build the backend-side
   reconstruction of prior turns' raw tool-call blocks from turns.tool_calls,
   capped at N=3, wired into /v1/ask.
7. Identity + tier-gating + Stripe billing — split into two sessions per the
   monetization amendment (§7.5), given the added real-payment scope:
   7a. Identity + tier-gating logic + schema. /v1/install (accepting/normalizing
       either identity type — the Apps Script side of the email/UUID logic is out
       of scope here, but the backend's acceptance of either is not), the
       subscriptions/stripe_webhook_events migrations, the three-tier gating
       logic in §7.2 in front of /v1/ask, and /v1/usage. Fully testable offline
       by seeding a subscriptions row directly — no live Stripe account needed.
   7b. Stripe integration. /v1/billing/checkout-session, /v1/billing/webhook
       with signature verification (§7.4), the stripe dependency, and an
       end-to-end test against a Stripe test-mode account. Sequenced after 7a
       since it depends on the schema and gating logic already being in place.
   Both sequenced after core ask/history/CSV work since they gate already-working
   functionality.
8. BYO key registration + encryption. Add cryptography to requirements.txt;
   implement /v1/byo-key, the encrypt-on-write/decrypt-on-read byo_keys path, and
   switch /v1/ask's client construction to the decrypted BYO key when set.
9. Retention job. A scheduled job (Render Cron Job or in-process periodic task)
   implementing the 12-month-inactivity purge (§3.4) and the short-TTL cleanup of
   unconfirmed RawCsv/proposal rows (§1).
10. Security hardening pass. Cross-install isolation audit (every query path
    reviewed for install_id scoping), a rotation runbook for the Fernet key and DB
    password, confirmation that Postgres network access is restricted to the app
    service, and a log-content review (no plaintext BYO keys or full Q&A bodies
    landing in application logs).

---

7. Monetization — three-tier pricing + Stripe billing (amendment, added 2026-09-16)

This section replaces the original 7-day-unlimited-then-BYO-key model with three
tiers: a small recurring free daily cap, a $2/month paid tier with a
generous-but-capped usage limit, and BYO-key (unchanged) as a fourth, genuinely
unlimited option for power users. It introduces real payment processing, which the
original design never addressed. Confirmed against current Stripe best practice via
web search rather than assumed — see Sources at the end of this section.

7.1 Payment processor choice and integration shape

Stripe, confirmed as still the standard default. Key current-practice points that
shape this design:

Use Stripe Checkout (Stripe's own hosted payment page), not Stripe Elements
embedded in our own UI. This is the right call for this project specifically
because it means card data never touches our server or database at all —
satisfies the "never store raw card details" requirement by construction, not by
policy, and keeps this service out of most PCI scope (SAQ A). It also avoids
embedding a payment iframe inside a Google Sheets Add-on sidebar, which has its own
CSP/iframe constraints (Apps Script sidebar implementation itself stays out of
scope per §5 — this design only specifies the contract).

Subscription creation flow:
1. Sidebar's "Upgrade" action calls POST /v1/billing/checkout-session
   (authenticated the normal way, via X-Install-Id) → backend creates a Stripe
   Checkout Session in mode=subscription, with client_reference_id = install_id,
   line_items=[{price: STRIPE_PRICE_ID, quantity: 1}], and (when
   identity_type=google_email) customer_email prefilled. Returns {checkout_url}.
2. Sidebar opens checkout_url in a new browser tab (a plain link — no Apps
   Script–specific mechanics designed here, per §5). Stripe creates the Customer
   automatically since none was passed.
3. User pays on Stripe's hosted page. success_url/cancel_url point at a plain
   static confirmation page this backend serves — not a webhook trigger point,
   just user-facing text ("Subscription active — return to your Sheet"). The
   webhook, not the redirect, is the source of truth for actually granting access
   (see below) — a deliberate trade-off: the user could in principle return to the
   Sheet a few seconds before the webhook lands and see stale free-tier status on
   their very next request. Not solved this session; noted explicitly rather than
   engineering around a multi-second race for a $2/mo product.

Mapping a Stripe customer/subscription to an install_id: the client_reference_id
set at Checkout Session creation is Stripe's documented mechanism for exactly
this; the checkout.session.completed webhook payload's client_reference_id field
recovers it. No new identity concept — reuses the same install_id used everywhere
else in this design.

Webhook events handled (POST /v1/billing/webhook), each upserting the new
subscriptions row (§7.3) keyed by stripe_subscription_id:
- checkout.session.completed — first-time subscribe. Insert the row: install_id
  (from client_reference_id), stripe_customer_id, stripe_subscription_id, status,
  current_period_start/end (from the session's expanded subscription object).
- customer.subscription.updated — covers renewals (extends current_period_end)
  and status transitions (e.g. active → past_due on a failed renewal charge, or
  recovery back to active after Stripe's own automatic retry succeeds). Upsert
  status + period fields verbatim.
- customer.subscription.deleted — cancellation (including retries exhausted). Set
  status='canceled' — a soft state update, not a row delete, matching this
  design's existing byo_keys.is_active pattern (audit trail over destructive
  deletes).
- invoice.payment_failed — persisted for visibility only (status itself is
  already handled via customer.subscription.updated); no new notification/dunning
  machinery is built this session — that would be scope this design wasn't asked
  to cover.

Stripe Customer Portal (self-service cancel/update-card) is not designed this
session — noted here explicitly, matching this doc's own practice of naming
exclusions rather than leaving them implicit, per §5's style.

7.2 Updated /v1/ask gating logic (replaces §2's old decision list)

Evaluated in this order on every /v1/ask:

1. install.byo_key_id IS NOT NULL → skip all other checks, unlimited. (unchanged
   from the original design.)
2. An active subscription exists for this install (subscriptions.status IN
   ('active', 'trialing')) → count this install's usage_events where
   occurred_at >= subscription.current_period_start AND
   occurred_at < subscription.current_period_end; if >= PAID_MONTHLY_CAP → reject
   {"error": "monthly_cap_reached", "prompt_byo_key": true, "resets_at":
   current_period_end}; else allow.
   past_due/canceled/unpaid subscriptions fall through to step 3 rather than
   being hard-blocked — consistent with this design's existing ethos (never
   punitively cut off access as a side effect of a billing state) and with
   Stripe's own automatic payment retries meaning past_due is often self-healing
   within days. Stated as the Phase A default policy, easy to tighten later.
3. Otherwise, free tier (recurring forever, no window expiry): count today's
   (UTC calendar day) usage_events for this install; if >= FREE_DAILY_CAP →
   reject {"error": "daily_cap_reached", "prompt_upgrade": true, "resets_at":
   <next UTC midnight>}; else allow.
4. On allow (step 2 or 3), insert the usage_events row before calling run_agent,
   exactly as today — same "counts against the cap even on a mid-run crash"
   rationale already in §2, unchanged.

Named, easily-adjustable constants (both placeholders to tune from real usage
data once live, not guessed definitively):
- FREE_DAILY_CAP = 5 — small deliberately; the old daily_cap=20 was sized for a
  7-day trial, not a forever allotment.
- PAID_MONTHLY_CAP = 300 — sized to comfortably cover a genuinely engaged single
  user's monthly usage while still bounding one subscriber's API cost against
  $2/mo of revenue.

Updated /v1/usage response (replaces the free_window_ends_at shape):
{install_id, tier: "byo_key"|"paid"|"free", questions_today, daily_cap,
questions_this_period, monthly_cap, period_ends_at, byo_key_required} — only the
fields relevant to the caller's actual tier are populated (e.g. a free-tier
install gets questions_today/daily_cap, not the paid fields).

7.3 Schema

New table, not overloading installs, matching this design's own existing
precedent of a dedicated byo_keys table for payment-adjacent state:

subscriptions: id (PK, UUID) · install_id (FK -> installs.install_id, UNIQUE —
one active subscription per install in Phase A) · stripe_customer_id (text,
indexed) · stripe_subscription_id (text, UNIQUE — webhooks arrive keyed on this)
· status (text — Stripe's own enum stored verbatim: trialing|active|past_due|
canceled|unpaid|incomplete|incomplete_expired — rather than remapped to a
simplified app-level enum, so a Stripe status this design didn't anticipate
doesn't silently fall through an incomplete mapping) · current_period_start
(timestamptz) · current_period_end (timestamptz — drives the monthly-cap window
in §7.2, the same role usage_events.occurred_at plays for the free tier's daily
window) · cancel_at_period_end (bool) · created_at · updated_at.

stripe_webhook_events: stripe_event_id (PK, text — Stripe's own evt_... id) ·
event_type (text) · processed_at (timestamptz). Exists purely as an idempotency
guard (§7.4) — Stripe retries webhooks and delivery isn't guaranteed
exactly-once, so every handler checks/inserts this row before mutating
subscriptions, short-circuiting a duplicate delivery.

installs.free_window_started_at is dropped (§2) — nothing replaces it.

7.4 Security considerations specific to payment data

- Webhook signature verification — critical, non-negotiable, matching this doc's
  existing "non-negotiable" callouts (e.g. §4's spend cap). An unverified
  /v1/billing/webhook is a real attack surface: anyone who learns the URL could
  POST a fake checkout.session.completed and grant themselves a subscription for
  free. Verified via stripe.Webhook.construct_event(payload, sig_header,
  STRIPE_WEBHOOK_SECRET) against the raw request body — this route must read
  Request.body() directly rather than accepting a parsed Pydantic model, since
  re-serializing a parsed body breaks signature verification (confirmed current
  Stripe/FastAPI guidance, see Sources).
- Idempotent processing — insert into stripe_webhook_events keyed on Stripe's
  event.id before mutating subscriptions; skip (return 200 immediately) if that
  id was already processed. Handles Stripe's documented retry/out-of-order
  delivery behavior.
- Never storing raw card details — satisfied by construction (§7.1: Stripe
  Checkout hosted page, never Elements), stated here explicitly as a requirement,
  not left implicit.
- New secrets, same convention as today's ANTHROPIC_API_KEY/SEC_USER_AGENT
  (.env.example locally, Render env var in production — no new secret-storage
  mechanism): STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID. Separate
  values for Stripe test mode vs live mode (Render preview vs production env var
  groups), so local/staging testing can never touch live subscriptions.
- Threat model fit — extends §4's existing framing ($7–10/mo-scale deployment,
  not enterprise) rather than replacing it: card-data compromise and payment
  fraud are Stripe's problem, absorbed by Stripe Checkout/Radar, not something
  this design needs its own tooling for. The residual risk this design does own
  is the webhook endpoint and the subscriptions table — covered above and by
  §4's existing cross-install-isolation discipline (every subscriptions query
  still filters by authenticated install_id, same as every other table). §4's
  session-10 log-content review should be extended to also cover Stripe webhook
  payloads (may carry customer email) — a one-line addition to that existing
  bullet, not new scope.
- requirements.txt gains the official stripe Python SDK — no other new runtime
  dependency.

7.5 Session 7 split

Split into 7a and 7b (reflected in §6's session 7 above). This mirrors the doc's
own existing precedent (see Verification below): the prior_messages and
csv_session.py ContextVar changes were each deliberately isolated into their own
session specifically so a change touching a real risk surface gets its own review
and tests rather than being buried inside broader work. Payment processing is a
bigger version of that same argument — it's real money and a new
externally-reachable, differently-authenticated endpoint (§7.4's webhook).
7a (tier-gating logic + schema) is fully testable offline by seeding a
subscriptions row directly, consistent with this project's existing
offline-once-cache-is-warm test philosophy (root CLAUDE.md's Commands section).
7b (Stripe integration proper) needs an actual Stripe test-mode account and a
publicly reachable URL or the Stripe CLI's `stripe listen --forward-to` for local
webhook delivery — a genuinely different testing shape than the rest of this
codebase, worth its own session so that difference doesn't get glossed over
inside a bigger session.

7.6 Known limitation: webhook processing latency under event bursts (discovered
during 7b's manual `stripe listen` verification, not fixed this session)

A single test-mode checkout fires far more than just `checkout.session.completed`
— charge/invoice/customer/payment_intent events too, ~13 in total for one
checkout. `stripe listen` forwards all of them (nothing filters event types), and
this backend's single local `uvicorn` worker processed all 13 correctly but
serially — each one, including a real round-trip to the remote Render Postgres,
took roughly 3 seconds, compounded for `checkout.session.completed` specifically
by its own extra outbound `stripe.Subscription.retrieve` call (§7.1/§7.3 — the
webhook payload doesn't embed the full subscription object, so this handler makes
a second live Stripe API call to fetch it). Three deliveries near the end of the
burst — including `checkout.session.completed` itself — exceeded the Stripe CLI's
client-side timeout and logged `context deadline exceeded`, even though the
server went on to finish and commit every one of the 13 correctly seconds later
(confirmed via `stripe_webhook_events.processed_at` timestamps, not assumed).

Confirmed not a correctness bug: the atomic marker-plus-mutation idempotency
design (§7's webhook section above) means a delivery that times out from the
caller's side but succeeds server-side is safely absorbed by Stripe's own
automatic retry — the retry's `stripe_webhook_events` check finds the marker
already committed and short-circuits to 200 without reprocessing. No data was
lost or duplicated in this run. But it's a real, observed latency characteristic
of serial single-worker processing under a burst, not a hypothetical — worth
revisiting before real production webhook volume, most likely via multiple
`uvicorn` workers or moving webhook processing off the request path entirely
(e.g. queue-and-process-async), rather than assumed to be fine because Phase A's
threat model is small-scale. Flagged for awareness and a future session's
judgment call, not addressed here — this session's scope was correctness and the
signature/idempotency design, not throughput under bursty load.

Sources (Stripe current-practice confirmation)
- Stripe Webhooks: Complete Implementation Guide (2026) — https://www.hooklistener.com/learn/stripe-webhooks-implementation
- Stripe Webhook Best Practices: Raw Body, Signatures & Retries — https://hookray.com/blog/stripe-webhook-best-practices-2026
- Checkout Sessions | Stripe API Reference — https://docs.stripe.com/api/checkout/sessions
- How Checkout works | Stripe Documentation — https://docs.stripe.com/payments/checkout/how-checkout-works
- FastAPI + Stripe Integration: Webhooks, Checkout & Subscriptions — https://fastro.ai/blog/fastapi-stripe-integration

---

Verification

This session produces a design document, not code — "verification" here means
confirming the design is internally consistent and validated against the real
codebase before Phase B begins, not running tests:
- Every schema/endpoint/flow above was checked against the actual current signatures
  of run_agent, tools.py's tool functions, csv_ingest.parse_csv,
  csv_statement.normalize/validate_mapping, and csv_session.py (all read in
  full this session) — not assumed.
- The two deviations from "no changes to core financial logic" (the agent.py
  prior_messages parameter, and the csv_session.py ContextVar reimplementation)
  are called out explicitly above, confirmed acceptable with the user, and scoped as
  their own Phase B sessions (6 and 5) specifically so they get isolated review and
  tests rather than being buried inside broader backend work.
- Before Phase B session 1 starts, confirm Render's current published Postgres-tier
  pricing/limits are still consistent with what this design assumes (an
  externally-controlled fact that can drift between this design session and actual
  implementation). Render's request-timeout assumption specifically is not just
  spot-checked against documentation — it's load-tested against the real deployed
  proxy in session 3, before any later session builds on it, with an explicit
  pivot-to-polling fallback if it doesn't hold (see §1 and §6).
- The master-key spend cap (§4) is a named, checkable prerequisite of session 3, not
  an assumption folded silently into the free-tier design — it must be set and
  confirmed active before that session's own load test (or any other real API call
  against the master key) runs.
