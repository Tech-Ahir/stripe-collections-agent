# The Approval Boundary and the Agent's Tool Contract

A written guide to the two things this system is: an agent that cannot act, and a gateway that
can act but cannot be persuaded. It is meant to be readable in ten minutes, alongside
`docs/openapi.json` for the generated API surface.

The Obsidian vault in `knowledge-base/` covers the same ground with more context and cross-links;
this file is the standalone version for someone who wants one document.

---

## 1. The shape

```
browser ──HTTP :8000──▶  app  (agent service)  ──POST /internal/actions/execute──▶  gateway
                         STRIPE_API_KEY_READ       + X-Approval-Token  (HMAC)         STRIPE_API_KEY_WRITE
                         ANTHROPIC_API_KEY         + X-Idempotency-Key                EMAIL_ADAPTER
                         no SMTP, no write key                                        NO PUBLISHED PORT
```

Two processes, two credential sets, one crossing point.

The gateway publishes no port. From the host:

```console
$ curl localhost:9000
curl: (7) Failed to connect to localhost port 9000: Could not connect to server
```

From inside the network:

```console
$ docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://gateway:9000/healthz').status)"
200
```

That difference is the boundary made physical.

---

## 2. The agent's tool contract

The agent is given exactly five tools. Four read, one drafts. **None of them can act.**

| Class | Effect | Tools |
|---|---|---|
| READ | No external effect | `list_overdue_invoices`, `get_invoice`, `get_customer`, `get_payment_history` |
| DRAFT | Writes internal records only | `propose_collection_letter` |
| ACTION | Reaches the outside world | *none — see below* |

`send_collection_letter` is not a tool the agent has and is refused. It is **absent from the
schema**. It exists only inside the gateway, reachable only by an approved proposal.

This is checkable in five seconds, from the running code:

```console
$ docker compose exec app python scripts/show_agent_tools.py

  [READ ] list_overdue_invoices(min_days_overdue, limit, min_amount_cents)
  [READ ] get_invoice(invoice_id)
  [READ ] get_customer(customer_id)
  [READ ] get_payment_history(customer_id, limit)
  [DRAFT] propose_collection_letter(invoice_id, subject, body, tone, rationale)

  [ok] tools declared                             5
  [ok] classes present                            ['DRAFT', 'READ']
  [ok] ACTION tools                               0
  [ok] tools whose name implies acting            none
  [ok] 'send_collection_letter' in the wire JSON  False
```

`ToolSpec.__post_init__` raises if a tool is declared `ACTION`, so such a tool cannot be
constructed at all. **Absence at definition time, not refusal at runtime** — the second is what a
permission check gives you, and it is weaker.

### The contract in detail

```
list_overdue_invoices(min_days_overdue: int|null = 1,
                      limit: int|null = 25,
                      min_amount_cents: int|null = None) -> Invoice[]
get_invoice(invoice_id: str) -> Invoice
get_customer(customer_id: str) -> Customer
get_payment_history(customer_id: str, limit: int|null = 10) -> Payment[]

propose_collection_letter(invoice_id: str,
                          subject: str,
                          body: str,
                          tone: friendly|firm|final,
                          rationale: str) -> ProposalRef
    Persists a proposal in status=pending. Returns proposal_id.
    NO EXTERNAL EFFECT. This is the agent's terminal capability.
```

Every schema is `strict: true`, closed (`additionalProperties: false`) and fully required.
Optional parameters are nullable rather than omitted, because strict mode requires every property
in `required`; the descriptions say "null means use the default".

Amounts always arrive as an integer in **minor units** plus a preformatted display string. The
model is never handed a number it has to divide. `2500` means `$25.00`, and the schema text says
so where the model will read it.

---

## 3. The seven checks

`gateway/verify.py`. In order, refusing on the first failure.

| # | Check | Refusal | Threat answered |
|---|---|---|---|
| 1 | HMAC signature valid | `401 invalid_signature` | A forged request |
| 2 | Token not expired | `401 token_expired` | A stale approval |
| 3 | Nonce not seen before | `409 token_replayed` | One approval, two sends |
| 4 | Proposal exists and **this database** says it is approved | `403 not_approved` | A send with no human approval behind it |
| 5 | An approve record exists and names the token's approver and nonce | `403 approval_mismatch` | A fabricated approver |
| 6 | Recomputed payload hash equals the token's | `409 payload_modified` | Approve one letter, send another |
| 7 | Idempotency key unused | `200` + the original result | A retry causing a second send |

### Check 4 is the one that matters

The status is read with the gateway's own `session.get`, from the gateway's own database. It is
not taken from the request body, and not from the token — the token carries a proposal *id*, and
the status is looked up.

A forged or replayed request therefore cannot cause a send, because the gateway's answer comes
from state the caller does not control.

### Nothing in the body is used in any decision

The proposal id and payload hash come from the signed token. The body's copies are logged for
forensics and are otherwise inert.

So a caller holding a valid token for proposal A who asks for proposal B gets **A** — the one
they were authorised for. Not a refusal, and not B.

### The token

```
base64url({"proposal_id","payload_hash","action_type","approver","nonce","iat","exp"})
  "." base64url(HMAC-SHA256(secret, first_segment))
```

Minted only by the approval endpoint, at the moment a human approves, signed with
`APPROVAL_SIGNING_SECRET`, valid fifteen minutes, single use.

No JWT library and **no `alg` field**, so an algorithm-downgrade attack has nothing to attack.
Unknown claims are refused rather than ignored. Comparison is constant-time.

### Why check 3 records the idempotency key

Both of these must hold:

- replay a valid token → `409 token_replayed`
- repeat the same idempotency key → single send, original result returned

They only coexist if a *replay* means reusing the token for a **new** request. So the consumed
nonce is recorded against the key that consumed it: a different key is a replay, the same key is
the same request arriving twice and falls through to check 7.

### Execution

1. Write the `executions` row and consume the nonce — one transaction, both unique, **commit**.
2. Emit `action.execution.started`.
3. Call the email adapter. *(No transaction is open across this.)*
4. Optionally `stripe.Invoice.send_invoice`, off by default.
5. Update the row, set the proposal to `executed`.
6. Emit `action.execution.succeeded` or `.failed` with the provider response.

Committing before the external call means there is a durable record that the system was about to
act even if the process dies mid-send — and no write lock is held across network I/O.

---

## 4. Demonstrating it

```console
$ docker compose exec app python scripts/demo_boundary.py

1. A pending proposal, executed with a VALID token -> 403 not_approved
   HTTP 403  not_approved       [PASS]   failed at check 4; passed: hmac_signature_valid, token_not_expired, nonce_unused
2. A forged token                                  -> 401 invalid_signature   [PASS]  failed at check 1
3. An expired token                                -> 401 token_expired       [PASS]  failed at check 2
4. A REJECTED proposal                             -> 403 not_approved        [PASS]  failed at check 4
5. Approved, then the letter is altered            -> 409 payload_modified    [PASS]  failed at check 6
6. Properly approved and unmodified                -> 200, all seven pass     [PASS]
7. The same token, a NEW request                   -> 409 token_replayed      [PASS]  failed at check 3
8. The same token AND the same key                 -> 200, original result    [PASS]
9. The audit log: INTACT

9/9 steps as expected  (one send, from the one properly approved proposal)
```

Nine attempts, one send.

The same seven refusals exist as tests, named to be read against the table above:

```console
$ docker compose run --rm tests pytest tests/test_boundary_refusals.py -q
31 passed
```

### From the UI

The **Try to send without approval** button on every pending proposal signs a *real* token and
calls the gateway. Every cryptographic check passes; check 4 refuses. The response shows the
code, the failed check number, and which checks passed first.

It cannot call the gateway from the browser — the gateway publishes no port — so the app makes
the call on the internal network. The refusal is genuine either way.

---

## 5. The record

`audit_events` is append-only and hash-chained. Each row commits to the previous row's hash, and
both `prev_hash` and `hash` are UNIQUE, which makes a forked chain impossible rather than
unlikely. There is no UPDATE or DELETE path anywhere in the codebase, and a test scans the
shipped source to keep it that way.

Every refusal is in there too — `action.refused`, with the code and the failed check — so the log
shows attempted executions rather than silent gaps.

```console
$ curl -s localhost:8000/v1/audit/verify
{"intact":true,"length":14,"head_hash":"sha256:...","broken_at_id":null,"reason":null}
```

`verify_chain` detects an edited row (its hash no longer matches), a deleted row (a gap in the id
sequence), a re-linked row (`prev_hash` mismatch) and a replaced genesis row.

---

## 6. What stops this being dismantled

Four independent layers, any one of which survives losing the others:

| Layer | Mechanism |
|---|---|
| Static | `.importlinter`, 7 contracts, in CI |
| Type | `AppSettings` declares no action credential — it cannot name the field |
| Runtime | `app/guards.py` **refuses to boot** if the agent can see one |
| Filesystem | The agent image contains no `gateway/` directory |

Plus tests that read the deployment configuration itself, because "publish the gateway port to
make testing easier" and "merge the gateway into the app" are changes no functional test would
notice.

```console
$ docker compose run --rm tests lint-imports
The agent service cannot import the action gateway KEPT
The agent service cannot import the email adapter KEPT
The agent service cannot import the write-capable Stripe client KEPT
The gateway cannot import the agent service KEPT
Shared code depends on neither service KEPT
The agent subsystem cannot mint approval tokens or call the gateway KEPT
Only the gateway may import smtplib KEPT
Contracts: 7 kept, 0 broken.
```

---

## 7. Where to look in the source

| Question | File |
|---|---|
| The seven checks | `gateway/verify.py` |
| The token codec | `shared/approval_token.py` |
| Minting, at approval time | `app/approval/token.py` |
| The "try without approval" probe | `app/approval/probe.py` |
| Execution and its transaction boundaries | `gateway/executor.py` |
| The agent's tools and their classification | `app/agent/tools/` |
| The loop | `app/agent/loop.py` |
| Compliance and invented-figure checks | `app/agent/guardrails.py` |
| The audit chain | `shared/audit.py` |
| Currency | `shared/money.py` |
| The invariants, for contributors and for AI assistants | `CLAUDE.md` |
