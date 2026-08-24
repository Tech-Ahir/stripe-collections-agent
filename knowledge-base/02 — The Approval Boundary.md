---
tags: [architecture, security]
---

# 02 — The Approval Boundary

This is the component the client is buying. It is about a hundred and fifty lines in
`gateway/verify.py` and it is worth reading in full.

The gateway performs **seven checks, in order, and refuses on the first failure**. Nothing may
be skipped, reordered or short-circuited. The order is the contract, and
`tests/test_boundary_refusals.py` asserts both the order and that a refusal at check N reports
exactly N−1 passes.

## The seven checks and the threat each one answers

| # | Check | Refusal | The threat it answers |
|---|---|---|---|
| 1 | HMAC signature valid | `401 invalid_signature` | **A forged request.** Without the shared secret, a caller cannot produce a token at all. |
| 2 | Token not expired | `401 token_expired` | **A stale approval**, replayed long after the operator moved on. Tokens live fifteen minutes. |
| 3 | Nonce not seen before | `409 token_replayed` | **One approval turned into two sends.** |
| 4 | Proposal exists and **this database** says it is approved | `403 not_approved` | **A send with no human approval behind it.** |
| 5 | An approve record exists, and names the token's approver and nonce | `403 approval_mismatch` | **An approved status with no approval behind it**, or a token attributed to someone who never approved anything. |
| 6 | Recomputed payload hash equals the token's | `409 payload_modified` | **Approving one letter and sending a different one.** |
| 7 | Idempotency key unused | `200` with the original result | **A retried request causing a second send.** |

Check 7 is the only row whose outcome is not a refusal: a used key returns the original result
and does not re-send.

## Check 4 is the one that matters

The gateway reads the proposal's status **with its own `session.get`, from its own database**.
It does not accept a status asserted in the request body. It does not accept one asserted in
the token either — the token carries a proposal *id*, and the status is looked up.

That is why a forged or replayed request cannot cause a send: the gateway's answer comes from
state the caller does not control.

Press **Try to send without approval** in the UI to watch it happen. That button sends a
*correctly signed* token for a pending proposal, so checks 1, 2 and 3 all pass, and check 4
refuses anyway. The weaker version of this demonstration sends a forgery and gets a 401, which
proves only that HMAC works.

## Nothing in the request body is used in any decision

The brief says the gateway trusts only the signature, its own record of the proposal, and the
idempotency key. So `gateway/verify.py` takes the proposal id and the payload hash **from the
token**, not from the body. The body's copies are recorded in the audit log for forensics and
are otherwise inert.

A consequence worth stating plainly: a caller who holds a valid token for proposal A and asks
for proposal B gets **proposal A** — the one they were authorised for. Not a refusal, and
certainly not B. `tests/test_boundary_refusals.py::test_a_proposal_id_in_the_body_cannot_redirect_the_send`
asserts exactly that.

## The approval token

Minted only by the approval endpoint of the public API, at the moment a human approves, and
signed with `APPROVAL_SIGNING_SECRET` — a secret shared by the two services and by nothing
else.

```json
{
  "proposal_id":  "uuid",
  "payload_hash": "sha256:...",
  "action_type":  "send_collection_letter",
  "approver":     "operator@servicia.ai",
  "nonce":        "uuid",
  "iat":          1756...,
  "exp":          1756...
}
```

Two segments, `base64url(claims) "." base64url(hmac_sha256(secret, first_segment))`. The
signature covers the *encoded* first segment, so verification never has to re-serialise the
claims to check them.

There is **no JWT library and no `alg` field**, which means the single most common JWT
vulnerability — `alg: none` — is not a thing that can be attempted here. `extra="forbid"` on
the claims means a token carrying an unexpected field is refused rather than accepted with the
extra ignored. Comparison is constant-time.

Expiry is deliberately *not* checked inside `decode()`. The brief makes signature validity
check 1 and expiry check 2, with different refusal codes, and the gateway must be able to
report which one failed. Folding them together would collapse two distinct answers into one.

## Why check 3 records the idempotency key

The brief requires both "replay a valid token → `409 token_replayed`" and "repeat the same
idempotency key → single send, original result returned". Both can only hold if a **replay**
means reusing the token to cause a *new* request.

So the consumed nonce is recorded against the idempotency key that consumed it
(`token_nonces.idempotency_key`). A different key is a replay. The same key is the same
logical request arriving twice, and falls through to check 7. Without that column, one of the
two required behaviours has to give way.

Check 4 knows about this too: a proposal that left `approved` *because of this very
idempotency key* is not an unapproved send, so check 4 recognises its own completed work.

## Execution, once all seven pass

1. Write an `executions` row with the idempotency key, status `pending`, **and consume the
   nonce in the same transaction**. Both are unique, so two concurrent requests cannot both
   proceed.
2. Emit `action.execution.started` to the audit log.
3. **Commit.** Only now does anything external happen.
4. Call the email adapter with the approved subject and body.
5. Optionally call `stripe.Invoice.send_invoice`, if `ENABLE_STRIPE_INVOICE_SEND` is on. Off
   by default.
6. Update the execution row, set the proposal to `executed`, emit
   `action.execution.succeeded` or `.failed` with the provider response.

Steps 1–3 commit *before* the external call for two reasons: there is a durable record that
the system was about to act even if the process dies mid-send, and no database write lock is
held across network I/O — which on SQLite would block every reader for the duration.

## Every refusal is in the record

A refused attempt appends `action.refused` to the audit log with the code, the failed check
number, and which checks passed first. A reviewer opening the audit log sees the rejected
proposal's attempted execution, not a silent gap.

## The negative paths, as tests

These are deliverables, not incidental unit tests. Each maps to a step of the handoff demo.

| Scenario | Expected |
|---|---|
| Execute a pending proposal | `403 not_approved`, nothing sent |
| Execute a rejected proposal | `403 not_approved` |
| Execute with a forged token | `401 invalid_signature` |
| Approve, alter the letter, execute | `409 payload_modified` |
| Replay a valid token | `409 token_replayed` |
| Repeat the same idempotency key | single send, original result |
| Reach the gateway from the host | connection refused |

Run them:

```bash
docker compose run --rm tests pytest tests/test_boundary_refusals.py -v
docker compose exec app python scripts/demo_boundary.py     # the same, against the live gateway
```

## Related

- [[01 — Architecture]] — why the boundary is two processes
- [[03 — Agent Design]] — why the agent has no send tool to refuse in the first place
- [[08 — Decisions]] — why no JWT library, and why the probe mints a real token
- [[10 — Extending This]] — adding a second action type without weakening any of this
