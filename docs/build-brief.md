# Stripe Collections Agent — Build Brief

An AI agent that finds overdue invoices in Stripe and drafts collection letters, with every
outbound action gated behind human approval enforced at an internal API boundary.

| | |
|---|---|
| **To** | Manoj Ahir — build lead |
| **From** | Eric Beser — spec owner, client-facing |
| **Client** | Servicia AI (Salomon) — paid trial, $650, leads to platform work |
| **Build window** | Mon 24 – Wed 26 August 2026. Handoff Wednesday EOD. |
| **Source of truth** | This document. Upwork contract scope plus kickoff call, 21 Aug 2026. |

> **Read this before anything else**
>
> The client is evaluating the **approval architecture**, not the quality of the letters. No
> external action may execute until approved through the internal API boundary, and the agent
> must be structurally incapable of sending anything on its own.
>
> If you run short of time, cut UI polish, cut the payment-history tool, cut proposal editing.
> Do not cut the boundary, its refusal tests, or the audit chain. Those three are the delivery.

---

## Erratum — 25 August 2026

**Section 7 originally specified the wrong Stripe permissions for `STRIPE_API_KEY_READ`.**

It asked for read access to *Invoices, Customers and Charges*. That was wrong in both
directions, and the error propagated into `README.md`, `.env.example`, `CLAUDE.md` and
`knowledge-base/04 — Stripe Integration.md` before it was caught.

- **Charges** is never called. The read client touches invoices, customers and test clocks only.
- **Test clocks** *is* called, via `test_helpers.test_clocks.list`, and was omitted.

Test clocks is not optional. Stripe refuses a back-dated `due_date`, so every invoice more than
roughly thirty days overdue exists only on a test clock. A key built to the original wording
silently loses the 47-, 62- and 95-day invoices — including the $23,400 letter that carries the
`final` tone — while the run still reports success.

Section 7 below carries the corrected list. The original PDF is kept alongside this file as
`Stripe-Collections-Agent-Design-Specification.pdf` for provenance; where the two disagree,
this document wins.

---

## Section 1 · Scope & Commitments

### What the system does

An operator opens a web UI and starts an agent run. The agent connects to Stripe, finds invoices
that are overdue, decides which ones warrant a collection letter, drafts a letter for each, and
places each letter into a proposal queue. Nothing leaves the system. A human then reviews each
proposal and approves or rejects it. Only on approval does the letter get sent, and the send is
performed by a separate component that the agent cannot reach directly.

### Committed deliverables

| Deliverable | Definition of done |
|---|---|
| GitHub repository | Created in the client's organisation, populated from the first commit, full history retained. |
| Docker image | Web test UI and Python backend. `docker compose up` brings the whole system up with no host dependencies beyond Docker. |
| Web test UI | Start a run, watch the agent work, review proposals, approve or reject, inspect the audit log. |
| Python backend | FastAPI. Agent orchestration, Stripe integration, proposal store, approval gateway. |
| Human approval step | Enforced at the internal API boundary. Demonstrable in both directions: approved action executes, unapproved action is refused. |
| Local testing instructions | A reader with Docker and a Stripe test key can run the system and reproduce the full demo unaided. |
| API documentation | Generated OpenAPI plus a written guide to the boundary and the agent's tool contract. |
| Obsidian knowledge base | Linked vault covering architecture, decisions, rules and standards. Ships in the repo. |

### Explicitly out of scope for the trial

- Live Stripe keys or real money movement. Test mode only, throughout.
- Real outbound email to real customers. The send adapter is pluggable and defaults to a
  captured-outbox implementation. See Section 8.
- Multi-tenancy, billing, user registration. A single operator identity is sufficient.
- The mobile app and the wider Servicia platform. The API is designed so that a mobile client is
  a later addition rather than a rewrite, but no mobile work happens here.
- Production hardening: rate limiting, secrets management beyond environment variables, HA.

> **Why this must be a real agent loop**
>
> Salomon asked repeatedly for agents. A script that calls an LLM once at the end will not pass.
> Build a genuine tool-use loop: the agent gets a goal and a toolset, and decides which tools to
> call and in what order. The run transcript in the UI is how that becomes visible, so the
> transcript is a deliverable, not a debug view.
>
> Practical consequence: do not hardcode the sequence "list invoices, then draft letter for
> each". Let the model choose. It will sometimes pull payment history first, sometimes skip it.
> That variability is the proof.

---

## Section 2 · Architecture

### The central rule

Two processes, two credential sets, one crossing point. The **Agent Service** can read from
Stripe and think. The **Action Gateway** can act on the outside world. The agent cannot act, and
the gateway cannot be persuaded — it only executes on a signed approval it can verify.

```
BROWSER — Web Test UI
  Runs · Live transcript · Proposal queue · Approve/Reject · Audit
                     │
                     │  HTTP (public API, port 8000)
                     ▼
AGENT SERVICE                      container: app
  FastAPI public router      /v1/runs  /v1/proposals  /v1/audit
  Agent loop (Claude tool-use)
  Stripe client — READ-ONLY restricted key
  Letter drafting
  Proposal store (SQLite)
  ✗ no write-capable Stripe key    ✗ no SMTP credentials
                     │
                     │  ONLY crossing point.
                     │  POST /internal/actions/execute
                     │  + HMAC-signed approval token
                     ▼
ACTION GATEWAY                     container: gateway (no published port)
  Verify signature → verify status → verify idempotency → execute
  Stripe client — write-capable key
  Email adapter — SMTP / outbox
  Append-only audit log
```

> **Implementation rules — non-negotiable**
>
> - The `gateway` service has **no `ports:` mapping** in `docker-compose.yml`. It is reachable
>   only on the internal Docker network. This is the boundary made physical, and it is the first
>   thing to show the client.
> - The agent's Stripe key is a **restricted read-only key**. Supply it as `STRIPE_API_KEY_READ`.
>   The write key exists only in the gateway's environment as `STRIPE_API_KEY_WRITE`.
> - No module imported by the agent may import the email adapter or the write-capable Stripe
>   client. Enforce with an import-linter contract in CI so a future contributor cannot
>   accidentally erase the boundary.
> - The gateway trusts nothing in the request body except what it can verify: the HMAC signature,
>   its own database record of the proposal, and the idempotency key.

### Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn | Client asked for a Python backend. FastAPI gives OpenAPI documentation for free, which is a named deliverable. |
| Agent | Anthropic Messages API, tool use, Claude Sonnet | Native tool-use loop. The tool schema is the agent's contract. |
| Stripe | `stripe` Python SDK | Official, test-mode native. |
| Persistence | SQLite via SQLAlchemy, Docker volume | Zero setup for the reviewer. The SQLAlchemy layer means Postgres is a URL change, not a rewrite. |
| UI | Jinja2 + HTMX + Tailwind (CDN) | Served by the same FastAPI app: one image, no Node build. HTMX polling gives a live transcript without a websocket layer. |
| Transport | Server-Sent Events for run transcript | Simpler than websockets, sufficient for one-directional streaming. |
| Packaging | Docker, docker compose, uv | Reproducible, fast installs. |

---

## Section 3 · Data Model

| Table | Fields |
|---|---|
| `runs` | id (uuid) · goal (text) · status (queued\|running\|awaiting_approval\|completed\|failed) · started_at · ended_at · operator_id · error |
| `run_steps` | id · run_id · seq (int) · type (thought\|tool_call\|tool_result\|message) · tool_name · payload (json) · created_at |
| `proposals` | id (uuid) · run_id · action_type (send_collection_letter) · status (pending\|approved\|rejected\|executed\|failed\|expired) · payload (json) · payload_hash (sha256) · rationale (text) · stripe_invoice_id · customer_email · amount_due · currency · days_overdue · created_at · expires_at |
| `approvals` | id · proposal_id · decision (approve\|reject) · actor · note (text) · edited_body (text, nullable) · decided_at · token_nonce |
| `executions` | id · proposal_id · idempotency_key (unique) · result (json) · status (succeeded\|failed) · executed_at · error |
| `audit_events` | id · ts · actor (agent\|operator\|gateway\|system) · event · subject_type · subject_id · detail (json) · prev_hash · hash |

> **Two details worth getting right**
>
> **`payload_hash`** is what makes approval meaningful. The operator approves a specific letter,
> and the gateway re-hashes the payload it receives and compares. If they differ it refuses.
> Approving one letter cannot be used to send a different one.
>
> **`audit_events`** is hash-chained: each row stores the hash of the previous row. It is
> append-only, with no update or delete path anywhere in the codebase. It makes tampering
> visible, and it demonstrates the audit thinking that the client's enterprise product will
> eventually need.

### Proposal lifecycle

```
agent drafts        operator reviews      gateway executes
     │                     │                     │
[pending] ──approve──▶ [approved] ──execute──▶ [executed]
     │                     │                     │
     ├──reject──▶ [rejected]                     └──error──▶ [failed]
     │
     └──ttl elapsed──▶ [expired]

Terminal states: rejected, executed, failed, expired.
Only [approved] is executable, and only once.
```

---

## Section 4 · Agent Design

### The loop

1. Operator starts a run with a goal, either the default or free text.
2. System prompt establishes role, constraints, and the rule that it cannot send anything.
3. Agent calls read tools to gather facts. It decides which and how many.
4. For each invoice it judges worth pursuing, it drafts a letter and calls
   `propose_collection_letter`.
5. Every tool call and result is persisted as a `run_step` and streamed to the UI.
6. Agent produces a closing summary. Run status becomes `awaiting_approval`.
7. The run is over. Approval happens on the operator's schedule, not inside the loop.

### Tool classification

Every tool is one of three kinds. The classification is declared in code and is the thing a
reviewer checks.

| Class | Effect | Tools |
|---|---|---|
| **READ** | No external effect. Auto-executes. | `list_overdue_invoices` · `get_invoice` · `get_customer` · `get_payment_history` |
| **DRAFT** | Writes internal records only. Auto-executes. | `propose_collection_letter` |
| **ACTION** | Reaches the outside world. **Not exposed to the agent at all.** | `send_collection_letter` — lives in the gateway, invocable only by an approved proposal |

> **Read this twice**
>
> `send_collection_letter` is not a tool the agent has and is refused. It is **absent from the
> agent's tool schema entirely.** The agent's only route to the outside world is to create a
> proposal that a human may later approve. Refusal at runtime is a weaker design than absence at
> definition time, and the client is evaluating exactly this.

### Tool schemas

```
list_overdue_invoices(min_days_overdue: int = 1,
                      limit: int = 25,
                      min_amount_cents: int | None = None) -> Invoice[]
    Returns id, number, customer_id, customer_name, customer_email,
            amount_due, currency, due_date, days_overdue,
            hosted_invoice_url, attempt_count

get_invoice(invoice_id: str) -> Invoice
    Full detail including line items and payment attempts.

get_customer(customer_id: str) -> Customer
    name, email, created, delinquent, currency, metadata.

get_payment_history(customer_id: str, limit: int = 10) -> Payment[]
    Prior invoices and their outcomes. Lets the agent distinguish a
    first-time late payer from a repeat offender and set tone accordingly.

propose_collection_letter(invoice_id: str,
                          subject: str,
                          body: str,
                          tone: "friendly" | "firm" | "final",
                          rationale: str) -> ProposalRef
    Persists proposal in status=pending. Returns proposal_id.
    NO EXTERNAL EFFECT. This is the agent's terminal capability.
```

### System prompt requirements

The prompt must establish, at minimum:

- Role: a collections assistant working on behalf of the account holder.
- That it cannot send anything, and that every letter it drafts will be reviewed by a human
  before it goes anywhere. State this plainly — it changes how the model writes.
- Escalation guidance keyed to `days_overdue`: courteous reminder in the first fortnight, firmer
  past thirty days, final notice past sixty.
- Tone calibrated by payment history. A reliable customer who is nine days late is not addressed
  like a habitual non-payer.
- A required `rationale` on every proposal explaining why this invoice, this tone. The rationale
  is shown to the operator and is what makes the queue reviewable at a glance.
- Never invent amounts, dates, invoice numbers or payment links. Every figure in a letter must
  come from a tool result.

### Guardrails

- Maximum 25 tool calls per run. Exceeding it fails the run cleanly with a partial transcript.
- Maximum 10 proposals per run unless the operator raises it at start.
- One pending proposal per invoice. A duplicate is rejected by the store, not by the prompt.
- Proposals expire after 72 hours (`PROPOSAL_TTL_HOURS`). Stale approvals are worse than no
  approvals.

---

## Section 5 · The Internal API Boundary

This is the component the client is buying. Build it Monday, before the agent and before the UI.
It must be demonstrable from a terminal in under a minute, because that is how Eric will open the
handoff call.

### Endpoint

```
POST http://gateway:9000/internal/actions/execute

Headers
  X-Approval-Token: <base64 HMAC-SHA256 signed token>
  X-Idempotency-Key: <uuid>
  Content-Type: application/json

Body
  { "proposal_id": "...", "payload_hash": "sha256:..." }
```

### Approval token

Minted only by the approval endpoint of the public API, at the moment a human approves, and
signed with `APPROVAL_SIGNING_SECRET` — a secret shared by the two services and by nothing else.

```json
{
  "proposal_id":  "uuid",
  "payload_hash": "sha256:...",   // binds the token to exact content
  "action_type":  "send_collection_letter",
  "approver":     "operator@servicia.ai",
  "nonce":        "uuid",         // single use
  "iat":          1756...,
  "exp":          1756...         // +15 minutes
}
```

### Verification sequence

The gateway performs all seven checks, in order, and refuses on the first failure.

| # | Check | Refusal |
|---|---|---|
| 1 | HMAC signature valid | `401 invalid_signature` |
| 2 | Token not expired | `401 token_expired` |
| 3 | Nonce not seen before | `409 token_replayed` |
| 4 | Proposal exists and status is approved | `403 not_approved` |
| 5 | Approval record exists, is an approve, and matches the token's approver | `403 approval_mismatch` |
| 6 | Recomputed payload hash equals the token's `payload_hash` | `409 payload_modified` |
| 7 | Idempotency key unused | `200` with the original result, no re-send |

> **Check 4 is the one that matters**
>
> The gateway reads the proposal status from the database itself. It does not accept a status
> asserted in the request body. This is why a forged or replayed request cannot cause a send: the
> gateway's answer comes from state the caller does not control.

### Execution, once all checks pass

1. Write an `executions` row with the idempotency key, status `pending`.
2. Emit `action.execution.started` to the audit log.
3. Call the email adapter with the approved subject and body.
4. Optionally call `stripe.Invoice.send_invoice` if `ENABLE_STRIPE_INVOICE_SEND` is on. Off by
   default.
5. Update the execution row, set proposal status to `executed`.
6. Emit `action.execution.succeeded` or `.failed` with the provider response.

### Required negative-path tests

These are deliverables, not incidental unit tests. Each maps to a demo step in Section 11.

- Execute a `pending` proposal → `403 not_approved`, nothing sent.
- Execute a `rejected` proposal → `403 not_approved`.
- Execute with a forged token → `401 invalid_signature`.
- Approve, then alter the letter body, then execute → `409 payload_modified`.
- Replay a valid token → `409 token_replayed`.
- Repeat the same idempotency key → single send, original result returned.
- Attempt to reach the gateway from the host → connection refused, because no port is published.

---

## Section 6 · Public API

Versioned under `/v1` and published as OpenAPI at `/docs`. The UI is one client of this API and
holds no privileges of its own, which is what allows the client's future mobile app to be an
additional client rather than a second implementation.

| Verb | Path | Purpose |
|---|---|---|
| POST | `/v1/runs` | Start a run. Body: `goal`, `min_days_overdue`, `max_proposals`. Returns `run_id`, executes asynchronously. |
| GET | `/v1/runs` | List runs with status and counts. |
| GET | `/v1/runs/{id}` | Run detail with full ordered transcript. |
| GET | `/v1/runs/{id}/stream` | SSE stream of steps as they occur. |
| GET | `/v1/proposals` | Filter by `status` and `run_id`. Default pending. |
| GET | `/v1/proposals/{id}` | Full proposal: letter, rationale, invoice context, customer history. |
| POST | `/v1/proposals/{id}/approve` | Body: `actor`, optional `note`, optional `edited_body`. Records approval, mints token, calls the gateway, returns the outcome. |
| POST | `/v1/proposals/{id}/reject` | Body: `actor`, `note`. Terminal. |
| GET | `/v1/invoices/overdue` | Read-only passthrough so the UI can show ground truth beside the agent's output. |
| GET | `/v1/audit` | Paged audit log, newest first, with chain-verification status. |
| GET | `/healthz` | Liveness plus reachability of Stripe, Anthropic and the gateway. |

> **On the edit-then-approve path**
>
> If the operator edits the body before approving, the edited text is stored on the approval
> record and the payload hash is recomputed from the edited content before the token is minted.
> The operator approves what they actually read. Do not mint the token from the original draft.

### Error contract

```json
{ "error": { "code": "not_approved",
             "message": "Proposal is in status 'pending'.",
             "proposal_id": "..." } }
```

Gateway refusal codes surface to the UI unchanged. When a refusal happens the UI must show the
code, because the refusal is the feature.

---

## Section 7 · Stripe Integration

### Identifying overdue invoices

An invoice is overdue when its status is `open` and its `due_date` is in the past. Filter
server-side; do not page the whole invoice list and filter in Python.

```python
stripe.Invoice.list(
    status="open",                       # finalized, still owed
    due_date={"lt": int(time.time())},   # past due
    limit=100,
    expand=["data.customer"],
)
```

`status="open"` means finalised with a balance remaining. Stripe's dashboard shows these as Past
due once the due date passes, but past-due is a display badge rather than a distinct API status,
so the `due_date` filter is what does the work. Invoices with
`collection_method="charge_automatically"` may be mid-retry; expose `attempt_count` so the agent
can reason about that rather than dunning someone whose card is about to be retried.

### Fields to project into the tool result

| Stripe field | Use |
|---|---|
| `id`, `number` | Identity; the number appears in the letter. |
| `customer` (expanded) | Name and email for addressing. |
| `customer_email` | Recipient. If null, the agent must not propose a letter. |
| `amount_due`, `amount_remaining`, `currency` | Amount owed. Minor units — format for display, never let the model do the arithmetic. |
| `due_date` | Unix timestamp. Derive `days_overdue` in Python and pass it in. |
| `hosted_invoice_url` | The payment link in the letter. Never fabricated. |
| `collection_method`, `attempt_count` | Whether automatic collection is still retrying. |
| `status_transitions.finalized_at` | Age of the receivable. |

> **Currency handling**
>
> Stripe returns minor units — `2500` is $25.00. Format once, in Python, using the currency's
> exponent, and pass the agent a preformatted `amount_display` string alongside the integer.
> Never ask the model to divide by 100. This is the single most likely source of an embarrassing
> error in a demo.

### Keys

| Variable | Scope |
|---|---|
| `STRIPE_API_KEY_READ` | Agent service. Restricted key, read-only on **Invoices**, **Customers** and **Test clocks**. |
| `STRIPE_API_KEY_WRITE` | Gateway only. Needed solely if `ENABLE_STRIPE_INVOICE_SEND` is enabled. |
| `STRIPE_API_KEY_SEED` | The seeding script only. Run by a human, never by a service. Standard test key. |

> **Test clocks is required, not optional**
>
> Corrected 25 Aug 2026 — this table previously read "Invoices, Customers, Charges". Charges is
> never called. Test clocks is, and omitting it is not a harmless over-restriction: Stripe
> refuses a back-dated `due_date`, so every invoice more than roughly thirty days overdue exists
> only on a test clock. A key without that permission causes the read client to skip the
> test-clock scan and the run to succeed with the oldest and largest receivables missing.
>
> Grant exactly three permissions, all Read: Invoices, Customers, Test clocks.

Ship a seeding script, `scripts/seed_stripe_test_data.py`, that creates six customers and eight
invoices in test mode with a spread of overdue ages from 3 to 95 days, including one with no
email and one already paid, so the agent's filtering is visible rather than assumed.

### Rate limits and failures

- Exponential backoff with jitter on `RateLimitError`, three attempts.
- `StripeError` inside a tool returns a structured error to the agent rather than crashing the
  run. The agent should be able to note the failure and continue.
- Cache customer lookups for the lifetime of a run.

---

## Section 8 · Letter Generation & Delivery

### Composition

The agent writes subject and body. Facts are injected by the tool layer, never recalled by the
model. The proposal record stores the letter as text plus the structured facts it was built from,
so the operator can see both.

Every letter must contain: customer name, invoice number, amount due, original due date, days
overdue, the hosted payment link, and a contact route for disputes.

### Tone ladder

| Tone | Trigger | Character |
|---|---|---|
| `friendly` | 1–14 days | Assumes oversight. Reminder, payment link, thanks. |
| `firm` | 15–59 days | States the balance plainly, asks for a payment date, offers to discuss terms. |
| `final` | 60+ days | Final notice before escalation. Factual, unemotional, no threats of specific legal action. |

> **Compliance guardrail**
>
> Collection correspondence is regulated. The system prompt must forbid threats of legal action,
> credit reporting, or any consequence the client has not authorised, and forbid any claim about
> fees or interest not present in the Stripe data. Put this in the prompt *and* in a
> `tests/test_letter_guardrails.py` suite built from it, so the operator can see both. Flag to
> the client that FDCPA scope is a legal question for their counsel, not an engineering one.

### Email adapter

Interface: `send(to, subject, body, meta) -> DeliveryResult`. Three implementations, selected by
`EMAIL_ADAPTER`.

| Adapter | Behaviour |
|---|---|
| `outbox` (default) | Writes the message to the database and to `/data/outbox`. Nothing leaves the machine. |
| `smtp` | Point at Mailpit: `docker compose --profile smtp up -d` |
| `resend` | Real delivery. Requires `RESEND_API_KEY`. Off by default, deliberately. |

Default to `outbox` so a reviewer who clones the repository cannot email a real person by
accident. The adapter lives in the gateway. The agent service must not import it.

---

## Section 9 · Web Test UI

Four screens. HTMX and Jinja, no Node build. The UI's job is to make the approval architecture
legible to someone evaluating it, so surface the boundary in the interface rather than in the
logs. Keep it plain — Tailwind defaults are fine, and no time goes into visual design.

### 1 · Dashboard `/`

- Header: connection status for Stripe, Anthropic and the gateway, from `/healthz`.
- Counters: overdue invoices, pending proposals, approved today, sent today.
- Primary action: **Start agent run**, with optional minimum days overdue and maximum proposals.
- Recent runs table: started, status, tool calls, proposals created.

### 2 · Run detail `/runs/{id}`

- Live transcript over SSE, newest last, auto-scrolling. Each entry rendered by type: reasoning
  as prose; tool call as name plus arguments; tool result as a collapsible JSON block.
- Each tool entry carries a **READ** or **DRAFT** chip. There is no ACTION chip, and its absence
  is the point — worth a one-line footnote in the UI itself.
- Proposals created during the run appear inline as cards as they are made.
- On completion: summary, counts, and a link to the queue.

### 3 · Approval queue `/proposals`

- Left: pending proposals, each showing customer, amount, days overdue and tone chip. Sorted by
  amount descending.
- Right: the selected proposal — the agent's rationale first, then invoice facts, then payment
  history, then the letter in an editable textarea.
- Actions: **Approve & send**, **Reject** (note required), and **Save edit**. Editing after
  saving invalidates any prior hash, which the UI states.
- On approve, show the gateway's response verbatim, including the verification checks that
  passed. On refusal, show the error code prominently rather than a generic failure toast.
- **Required:** a "Try to send without approval" button on each pending proposal that calls the
  gateway directly and displays the `403 not_approved`. Roughly an hour of work. Do not drop it —
  it is step 4 of the handoff demo.

### 4 · Audit log `/audit`

- Reverse-chronological table: timestamp, actor, event, subject, detail.
- Filter by actor and event type.
- A **Verify chain** control that recomputes the hash chain and reports intact or broken, with
  the first divergent row if broken.

---

## Section 10 · Repository, Docker & Configuration

### Structure

```
stripe-collections-agent/
├── README.md              # quickstart, 10 minutes to running demo
├── docker-compose.yml
├── .env.example
├── app/                   # AGENT SERVICE — no write credentials
│   ├── main.py
│   ├── api/               # runs, proposals, invoices, audit
│   ├── agent/
│   │   ├── loop.py
│   │   ├── prompts.py
│   │   └── tools/         # read_tools.py, draft_tools.py
│   ├── stripe_client/read.py   # READ-ONLY key
│   ├── approval/token.py       # mints signed tokens
│   ├── store/             # models, repositories, migrations
│   └── web/               # templates, static
├── gateway/               # ACTION GATEWAY — isolated
│   ├── main.py
│   ├── verify.py          # the seven checks
│   ├── executor.py
│   ├── email_adapter/     # outbox, smtp, resend
│   └── stripe_client/write.py
├── shared/                # schemas, hashing, audit chain
├── scripts/seed_stripe_test_data.py
├── tests/
│   ├── test_boundary_refusals.py   # the negative paths
│   ├── test_agent_tools.py
│   ├── test_letter_guardrails.py
│   └── test_audit_chain.py
├── docs/                  # generated OpenAPI + boundary guide
└── knowledge-base/        # Obsidian vault
```

### Compose

```yaml
services:
  app:
    build: {context: ., dockerfile: docker/app.Dockerfile}
    ports: ["8000:8000"]              # public
    environment:
      STRIPE_API_KEY_READ, ANTHROPIC_API_KEY,
      APPROVAL_SIGNING_SECRET, GATEWAY_URL=http://gateway:9000
    volumes: ["appdata:/data"]
    depends_on: [gateway]

  gateway:
    build: {context: ., dockerfile: docker/gateway.Dockerfile}
    # NO ports: — internal network only. This is the boundary.
    environment:
      STRIPE_API_KEY_WRITE, APPROVAL_SIGNING_SECRET,
      EMAIL_ADAPTER=outbox
    volumes: ["appdata:/data"]

  mailpit:                            # optional, profile: smtp
    image: axllent/mailpit
    ports: ["8025:8025"]
```

### Environment

| Variable | Notes |
|---|---|
| `STRIPE_API_KEY_READ` | Restricted test key. Agent service. |
| `STRIPE_API_KEY_WRITE` | Gateway only. Optional unless invoice send enabled. |
| `ANTHROPIC_API_KEY` | Agent service. |
| `APPROVAL_SIGNING_SECRET` | Shared by both services and nothing else. 32+ bytes. |
| `EMAIL_ADAPTER` | `outbox` \| `smtp` \| `resend`. Default `outbox`. |
| `ENABLE_STRIPE_INVOICE_SEND` | Default false. |
| `PROPOSAL_TTL_HOURS` | Default 72. |
| `MAX_TOOL_CALLS_PER_RUN` | Default 25. |

> **README requirement**
>
> A reader with Docker and a Stripe test key must reach a working demo in ten minutes with no
> other assistance: clone, copy `.env.example`, seed test data, `docker compose up`, open
> `localhost:8000`, run the agent, approve a letter, view the outbox. Write the README against
> that clock and test it on a clean machine before handoff.

---

## Section 11 · Acceptance, Demo & Build Sequence

### Acceptance criteria

| # | Criterion |
|---|---|
| 1 | Operator starts a run from the UI and watches the agent's tool calls stream live. |
| 2 | Agent finds overdue invoices in Stripe test mode and drafts a letter for each one it judges worth pursuing, with a written rationale. |
| 3 | No proposal is sent automatically. Every one lands in the queue as `pending`. |
| 4 | Approving a proposal executes the send through the gateway and moves it to `executed`. |
| 5 | Calling the gateway directly with an unapproved proposal returns `403 not_approved` and sends nothing. |
| 6 | Editing a letter after approval and replaying the token returns `409 payload_modified`. |
| 7 | The gateway is unreachable from the host. No published port. |
| 8 | The audit log shows the full chain from run start to execution and verifies intact. |
| 9 | `docker compose up` reproduces all of the above on a clean machine. |
| 10 | Obsidian vault opens and its internal links resolve. |

### Handoff demo — the running order

1. Show the compose file. Point at the gateway with no published port. Attempt
   `curl localhost:9000` from the host: connection refused.
2. Start a run. Narrate the agent choosing its own tools rather than following a script.
3. Open the queue. Three letters, three tones, three rationales.
4. Press **Try to send without approval**. Show the `403`.
5. Approve one. Show the seven checks passing and the letter arriving in the outbox.
6. Reject one with a note. Attempt execution. Show the refusal.
7. Open the audit log. Verify the chain.
8. Open the Obsidian vault at the architecture note and follow a link.

### Build sequence and checkpoints

Eric reviews at the end of each day, allowing for the time difference. Push to `main` before you
sign off so the review happens on real code rather than a description of it. Anything that will
not land, flag on the day rather than on Wednesday.

| Day | Work |
|---|---|
| **Mon 24 Aug** | Repository, compose, both services skeletal. Data model and migrations. Audit chain. Gateway with all seven checks and the full negative-path test suite passing. *The boundary is finished on day one, because it is what is being evaluated.* |
| **Tue 25 Aug** | Stripe read client and seed script. Agent loop, tool schemas, system prompt. Proposal creation. Letter guardrail tests. End-to-end run producing proposals. |
| **Wed 26 Aug** | UI: four screens, SSE transcript, approval flow, refusal button. README and boundary guide. Obsidian vault. Clean-machine rehearsal of the full demo. Handoff. |

### Obsidian knowledge base

Ships at `knowledge-base/`, opened as a vault. Minimum notes, each linked:

- `00 — Start Here` — what the system is, how to run it, where to go next.
- `01 — Architecture` — the two services and why they are separate.
- `02 — The Approval Boundary` — the seven checks and the threat each one answers.
- `03 — Agent Design` — tool classification, prompt, guardrails.
- `04 — Stripe Integration` — the overdue query, field mapping, currency handling.
- `05 — Data Model` · `06 — API Reference` · `07 — Running Locally`
- `08 — Decisions` — one note per decision: context, choice, alternatives rejected.
- `09 — Development Standards` — conventions any future contributor must follow, including the
  import rule that preserves the boundary.
- `10 — Extending This` — how to add a new action type, which is the client's most likely next
  move.

> **Write note 10 carefully**
>
> The client intends to commission many agents from us. "Extending This" is the note that turns
> this trial into a reusable template and shapes what they ask for next, so it deserves real
> attention rather than a stub. Have Claude Code generate the vault from the finished codebase on
> Wednesday morning, then edit notes 08 and 10 by hand — those two carry judgment that the
> generated text will not.
