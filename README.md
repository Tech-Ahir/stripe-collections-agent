# Stripe Collections Agent

An agent that finds overdue Stripe invoices and drafts collection letters. **It cannot send
them.** A human reviews each letter and approves or rejects it, and only on approval does a
physically separate service perform the send.

Stripe **test mode only**. There is no live-key code path, and the default email adapter captures
letters locally, so cloning this repository and running the demo cannot email a real person.

---

## Ten minutes to a working demo

You need Docker. Nothing else — no Python, no Node, no database.

### 1. Configure (2 min)

```bash
cp .env.example .env
```

Fill in three values:

| Variable | Where to get it |
|---|---|
| `APPROVAL_SIGNING_SECRET` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `STRIPE_API_KEY_READ` | Stripe Dashboard → Developers → API keys → **Restricted keys**. Read-only on Invoices, Customers, Charges. A standard `sk_test_…` works, but the read-only split is the point. |
| `STRIPE_API_KEY_SEED` | Your standard `sk_test_…` key. Used only by the seeding script; no service ever reads it. |

`ANTHROPIC_API_KEY` is needed only to run the agent live. Everything else in this README works
without it.

### 2. Bring it up (2 min)

```bash
docker compose up -d --build --wait
```

Two containers start. `app` publishes port 8000; `gateway` publishes nothing.

### 3. Seed Stripe test mode (1 min)

Six customers and eight invoices, overdue from 3 to 95 days, including one customer with no email
address and one invoice already paid — so the agent's filtering is visible rather than assumed.

```bash
docker compose run --rm --entrypoint python tests scripts/seed_stripe_test_data.py --recreate
```

```
  invoice      $250.00  acme         3 days overdue
  invoice    $1,240.50  borealis     9 days overdue
  invoice    $4,800.00  corvus      18 days overdue
  invoice    $3,300.00  delta       27 days overdue   <- no email on file, must be skipped
  invoice      $875.00  eastwind    47 days overdue
  invoice   $23,400.00  ferrolux    62 days overdue
  invoice    $8,650.00  ferrolux    95 days overdue
```

### 4. Fill the queue (1 min)

**With an Anthropic key** — open <http://localhost:8000> and press **Start agent run**. Watch the
transcript stream: the agent chooses its own tools and its own order.

**Without one** — populate the queue by driving the real loop with a scripted model over the same
live Stripe data:

```bash
docker compose exec app python scripts/dev_seed_run.py
```

That run is badged **scripted fixture** in the UI and says so in its own transcript. It exists so
the approval flow is reviewable before a key is available, not to pass for a real agent run.

### 5. Run the demo (4 min)

Open <http://localhost:8000/proposals>. Then, in order:

```bash
# 1. The gateway is unreachable from the host. This must fail.
curl localhost:9000

# 2. The agent's complete toolset: five tools, READ and DRAFT only, no send capability.
docker compose exec app python scripts/show_agent_tools.py
```

3. In the queue, read a proposal: **rationale first**, then the invoice facts it was built from,
   then the letter.
4. Press **Try to send without approval** → `403 not_approved`, refused at check 4. The token was
   correctly signed; the gateway refused because it read the status from its own database.
5. Press **Approve & send** → the seven checks are listed, and the letter appears in
   [the outbox](http://localhost:8000/outbox).
6. **Reject** another with a note, then press **Try to send without approval** on it →
   `403 not_approved` again.
7. Open [the audit log](http://localhost:8000/audit) and press **Verify chain** → intact.

Or all of it from a terminal, against the live gateway:

```bash
docker compose exec app python scripts/demo_boundary.py
```

```
9/9 steps as expected  (one send, from the one properly approved proposal)
```

---

## The shape of it

```
browser ──HTTP :8000──▶  app  (agent service)  ──POST /internal/actions/execute──▶  gateway
                         STRIPE_API_KEY_READ       + X-Approval-Token  (HMAC)         STRIPE_API_KEY_WRITE
                         ANTHROPIC_API_KEY         + X-Idempotency-Key                EMAIL_ADAPTER
                         no SMTP, no write key                                        NO PUBLISHED PORT
```

Two processes, two credential sets, one crossing point. The agent can read from Stripe and think;
it holds no capability to act. The gateway can act, and cannot be persuaded — it executes only
against a signed approval it verifies with **seven checks, in order**, the fourth of which reads
the proposal's status from its own database rather than from anything the caller sent.

`docs/boundary-guide.md` explains it properly. `knowledge-base/` is an Obsidian vault covering
architecture, decisions, standards and how to extend it.

---

## Screens

| | |
|---|---|
| <http://localhost:8000/> | Dashboard: connection status, counters, start a run, recent runs |
| `/runs/{id}` | Live transcript over SSE, with a READ or DRAFT chip on every tool call |
| `/proposals` | The approval queue: review, edit, approve, reject, try without approval |
| `/audit` | The hash-chained log, filterable, with a **Verify chain** control |
| `/outbox` | Letters the gateway captured. Nothing left the machine. |
| `/docs` | Generated OpenAPI — 15 operations |

---

## Tests, lint and the import contracts

Everything runs in the container. No host Python required.

```bash
docker compose run --rm tests                                    # the whole suite
docker compose run --rm tests pytest tests/test_boundary_refusals.py -v
docker compose run --rm tests lint-imports                       # the 7 import contracts
docker compose run --rm tests ruff check .
```

`tests/test_boundary_refusals.py` must stay green at all times. If a change makes a refusal test
fail, **the change is wrong, not the test.**

---

## What holds the boundary in place

Four independent layers. Any one survives losing the others.

| Layer | Mechanism | Catches |
|---|---|---|
| **Static** | `.importlinter`, 7 contracts, in CI | an import that exists in the source, on any path |
| **Type** | `AppSettings` declares no action credential | code that tries to read a write key — it cannot name the field |
| **Runtime** | `app/guards.py` **refuses to boot** | a compose file that hands the agent a key it should not have |
| **Filesystem** | the agent image contains no `gateway/` directory | code that is not there at all |

Plus tests that read the deployment configuration itself, because *"publish the gateway port to
make testing easier"* and *"merge the gateway into the app"* are changes no functional test would
notice.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `STRIPE_API_KEY_READ` | — | **Required.** Restricted, read-only. Agent service only. |
| `APPROVAL_SIGNING_SECRET` | — | **Required**, 32+ bytes. Shared by both services and nothing else. |
| `ANTHROPIC_API_KEY` | — | Needed only for a live agent run. |
| `STRIPE_API_KEY_SEED` | — | The seeding script only. Never read by a service. |
| `STRIPE_API_KEY_WRITE` | empty | Gateway only. Needed only if invoice send is enabled. |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | |
| `EMAIL_ADAPTER` | `outbox` | `outbox` \| `smtp` \| `resend` |
| `ENABLE_STRIPE_INVOICE_SEND` | `false` | |
| `PROPOSAL_TTL_HOURS` | `72` | |
| `MAX_TOOL_CALLS_PER_RUN` | `25` | |
| `MAX_PROPOSALS_PER_RUN` | `10` | |
| `STRIPE_INCLUDE_TEST_CLOCK_INVOICES` | `true` | Makes the test-mode fixture visible to the agent. See below. |
| `ENABLE_UNAPPROVED_ATTEMPT_DEMO` | `true` | The "try to send without approval" button. |

Compose refuses to start if either required value is missing, rather than booting into a state
where tokens cannot be verified.

### Seeing a real email arrive

The default adapter captures letters to the database and `/data/outbox`. For a real SMTP delivery
into a real inbox, with still no external delivery:

```bash
docker compose --profile smtp up -d          # Mailpit
# set EMAIL_ADAPTER=smtp in .env
docker compose up -d --force-recreate gateway
# approve a proposal, then open http://localhost:8025
```

`EMAIL_ADAPTER=resend` sends for real. It is off by default and requires an explicit key.

---

## Two things worth knowing

**Stripe will not create a back-dated invoice.** `due_date` must be in the future, at creation
*and* on update, so a genuinely 95-days-overdue invoice cannot be made directly. The fixture is
built on **test clocks** frozen in the past. Objects on a test clock are omitted from unfiltered
list calls and `invoices.list` has no `test_clock` filter, so the overdue query is additionally
run scoped per test-clock customer with the *identical* server-side filters — only a `customer`
scope is added, and nothing is paged and filtered in Python. Set
`STRIPE_INCLUDE_TEST_CLOCK_INVOICES=false` to switch that off; it is inert in live mode.

**Stripe reissues `hosted_invoice_url` on every read.** So the guardrail that stops the model
inventing a payment link compares a measured 120-character identity prefix rather than the whole
string: two reads of the same invoice share 140 of 159 characters, while two different invoices
diverge at 92. Strict enough to reject another invoice, another account or another host; tolerant
of a reissue. The letter is never rewritten to the newest link, because substituting text after
review would mean the operator reads one thing and the customer receives another.

Both are recorded in `knowledge-base/08 — Decisions.md` with what was rejected and why.

---

## Starting clean

```bash
docker compose down -v                # removes the volume, so the database goes too
docker compose up -d --build --wait
```

The Stripe fixture lives in your Stripe test account, not in that volume:

```bash
docker compose run --rm --entrypoint python tests scripts/seed_stripe_test_data.py --destroy
```

---

## Out of scope for this trial

Live Stripe keys or real money movement. Real outbound email to real customers. Multi-tenancy,
billing, user registration — a single operator identity is sufficient. The mobile app: the API is
designed so a mobile client is a later addition rather than a rewrite, but no mobile work happens
here. Production hardening: rate limiting, secrets management beyond environment variables, HA.

---

## Layout

```
CLAUDE.md                  the seven non-negotiable rules, for contributors and AI assistants
docker-compose.yml         gateway has NO published port
app/                       AGENT SERVICE — no write credentials
  agent/                   loop, prompts, tools (READ and DRAFT only), guardrails
  api/                     the /v1 surface and its read models
  approval/                token minting, and the unapproved-send probe
  services/                the approval path
  stripe_client/read.py    restricted key
  store/                   runs, transcripts, proposals
  web/                     four screens, Jinja + HTMX
gateway/                   ACTION GATEWAY — isolated, no published port
  verify.py                the seven checks
  executor.py              execution, and its transaction boundaries
  email_adapter/           outbox (default), smtp, resend
  stripe_client/write.py   write-capable key
shared/                    schemas, hashing, audit chain, money, the data model
scripts/                   seeding, the boundary demo, the tool-schema inspector
tests/                     refusals, boundary, guardrails, money, audit chain, API
docs/                      the boundary guide and the generated OpenAPI
knowledge-base/            Obsidian vault, notes 00–10
```
