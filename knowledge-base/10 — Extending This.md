---
tags: [howto, architecture]
---

# 10 — Extending This

This system sends collection letters. That is one action type. The architecture is not about
letters.

What it is about is this: **an agent that can propose, a human who can approve, and a separate
process that can act.** Anything that fits that shape fits here — issuing a refund, cancelling a
subscription, applying a credit note, posting to a customer's account, filing a dispute
response. The collection letter is the first one, not the only possible one.

This note is how to add the second, and what must not move while you do.

---

## The three invariants

Everything else is negotiable. These are not.

1. **The agent's toolset gains a DRAFT tool, never an ACTION tool.** A new capability means a
   new thing the agent can *propose*. If you find yourself adding a tool that does the thing,
   stop: that code belongs in the gateway.
2. **The gateway decides, from its own state.** A new action type adds a branch to the executor,
   not a new route and not a new way in. The seven checks run first, unchanged, in order.
3. **The hash binds the approval to the content.** Whatever the new payload is, the operator
   approves a specific one and the gateway re-hashes it. If the new action's payload cannot be
   canonically serialised, fix that before anything else.

If a change seems to require breaking one of these, it is the design that needs discussing, not
the invariant that needs bending. See [[09 — Development Standards]].

---

## Adding an action type, concretely

Worked example: **`issue_refund`** — the agent proposes a refund on an overdue invoice the
customer has disputed; a human approves; the gateway calls Stripe.

### 1. Name the action and widen the enum

`shared/models.py`:

```python
ACTION_TYPES = ("send_collection_letter", "issue_refund")
```

The CHECK constraint updates with it, so an unknown action type cannot be persisted. Startup
migrates additively — but a CHECK constraint change is **not** something `ADD COLUMN` can
express, so this is one of the cases that needs `docker compose down -v` in development, or a
real migration in anything holding data. See decision 11 in [[08 — Decisions]].

### 2. Define the payload, and make it hashable

The payload is what the operator approves and what the gateway re-hashes. Put it in
`shared/schemas.py` as a Pydantic model, with every fact the action needs and nothing derived at
execution time.

```python
class RefundPayload(BaseModel):
    action_type: Literal["issue_refund"]
    invoice_id: str
    charge_id: str
    amount_minor: int          # minor units, always
    amount_display: str        # preformatted; see 04
    currency: str
    reason: str
```

**Rule of thumb:** if the gateway would have to look something up at execution time to know what
to do, that thing belongs in the payload. Anything the gateway derives itself is outside the
hash and therefore outside what the human approved.

### 3. Add the DRAFT tool

`app/agent/tools/draft_tools.py`, alongside `propose_collection_letter`. It:

- takes the facts it needs by id, and reads them from `registry.facts_by_invoice` — never from
  what the model wrote;
- validates whatever the equivalent of `guardrails.py` is for this action (for a refund: an
  amount that cannot exceed the charge, a reason from a fixed set);
- writes a `proposals` row with `status="pending"` and the new `action_type`;
- returns a proposal ref and **nothing that looks like a result**.

Register it in `app/agent/tools/registry.py`. Declare it `ToolClass.DRAFT`. `ToolSpec` will
refuse to construct it if you reach for `ACTION`.

### 4. Teach the gateway to execute it

`gateway/executor.py`. Branch on `verified.claims.action_type`:

```python
EXECUTORS = {
    "send_collection_letter": _send_letter,
    "issue_refund": _issue_refund,
}
```

Each executor keeps the same shape, and the shape is the point:

1. claim the work — `executions` row plus the nonce, one transaction, **commit**;
2. emit `action.execution.started`;
3. do the outward-facing thing, with no transaction open;
4. update the row and the proposal, emit `.succeeded` or `.failed`.

An unknown `action_type` must **refuse**, not default. A dictionary lookup that falls back to
"send a letter" is how a refund becomes an email.

### 5. Give the gateway the credential, and only the gateway

A refund needs write access to Charges. Add it to the **gateway's** environment in
`docker-compose.yml` and to `GatewaySettings`. Do not add it to `AppSettings` — that class has no
field for it, which is deliberate, and `app/guards.py` should learn the new variable name so the
agent service refuses to boot if it can see it.

### 6. Write the refusal tests first

Copy the shape of `tests/test_boundary_refusals.py` for the new action type. At minimum: a
pending proposal, a rejected proposal, a forged token, a modified payload, a replay, and a
repeated idempotency key. **Do not move on until they pass.** They are the deliverable; the
happy path is the easy part.

### 7. The UI

The queue is already action-agnostic in structure. It renders `rationale`, then facts, then the
letter body — for a refund, the third panel is the refund summary rather than a textarea. Add a
partial per action type and select on `action_type`; keep the rationale-first ordering, because
that is what makes a queue reviewable at a glance.

---

## What you will be tempted to do, and shouldn't

| Temptation | Why it breaks something |
|---|---|
| Add `execute_now=true` to the propose tool | That is an ACTION tool wearing a DRAFT tool's name. |
| Let the gateway accept the action type from the request body | Nothing in the body is trusted. It comes from the signed token. |
| Add a second gateway route for the new action | One route, one set of checks. A second route is a second place to forget check 4. |
| Auto-approve "low-risk" actions | The moment a threshold decides, the human is out of the loop and this is a different product. If the client asks for it, it is a *policy* stored and audited like any other, and it still goes through the gateway with a signed token minted by whatever made the decision. |
| Reuse one idempotency key across action types | It is unique across all executions, by design. |
| Skip the payload hash because the payload is "just an id" | An id is content. Approving refund A must not send refund B. |

---

## Changes that need more than a new action type

**More than one operator.** `operator_id` is a single identity today. Real approvers mean a
users table, and the interesting part is not authentication — it is that `approvals.actor` and
the token's `approver` become claims about a *person*, so check 5 starts carrying weight it does
not carry now. Add authorisation (who may approve what, and up to what amount) at the approval
endpoint, and put the policy in the audit log alongside the decision.

**Postgres.** A URL change plus a real migration tool. The SQLAlchemy layer is already portable;
the SQLite-specific pieces are the WAL pragmas in `shared/db.py`, the partial unique index
(Postgres has the same feature with different syntax — both are already declared), and the
`is_retryable_write_conflict` message matching. Once there, the audit chain's retry becomes rarer
because Postgres serialises better, and `run_in_transaction` can shrink.

**Real migrations.** Adopt Alembic the first time the schema changes under data anyone cares
about. `shared/schema.py` should then become a *check* rather than a repair: compare and refuse,
so a mismatch is a deployment error instead of a silent `ADD COLUMN`.

**Many concurrent runs.** The thread pool in `app/runner.py` is capped at two and the SSE stream
polls per client. Both are fine for one operator and both are the first things to replace: a real
queue for runs, and a shared change-feed for the stream.

**Retries on a failed execution.** Today a failed execution is terminal and recorded. Automatic
retry is a genuine feature request and the safe version is narrow: retry the *same* execution row
under the *same* idempotency key, so the gateway's check 7 makes a duplicate send impossible.
Never mint a fresh token to retry — that is a second approval nobody gave.

---

## The part that generalises

If the client commissions another agent from us, the reusable core is not the letters and not
the Stripe integration. It is these five files:

| File | What it is |
|---|---|
| `shared/approval_token.py` | An approval that binds an approver to exact content, for fifteen minutes, once. |
| `gateway/verify.py` | Seven ordered checks against state the caller does not control. |
| `shared/audit.py` | An append-only chain that makes tampering visible, safe across processes. |
| `app/agent/tools/base.py` | A capability classification that refuses to construct the dangerous class. |
| `shared/hashing.py` | Canonical serialisation, so "the same content" means one thing. |

None of them mentions invoices. Lifting them into a shared internal package is the obvious next
move, and the thing to resist while doing it is generalising the *seven checks* into
configuration. Their order and their strictness are the product; a checks framework with a
config file is how a boundary becomes a suggestion.

## Related

- [[02 — The Approval Boundary]] — the checks a new action type inherits
- [[03 — Agent Design]] — where a new DRAFT tool goes
- [[08 — Decisions]] — why the pieces are shaped this way
- [[09 — Development Standards]] — the rules a contribution must not break
