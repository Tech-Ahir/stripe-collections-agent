---
tags: [reference]
---

# 05 — Data Model

SQLAlchemy models in `shared/models.py`. Lifecycle enums are enforced by **CHECK constraints**
as well as by Python, so the database itself documents the lifecycle and a typo'd status cannot
be persisted.

The models live in `shared/` rather than under `app/` so the gateway never has to import the
agent service in order to read a proposal — see [[08 — Decisions]].

## Tables

| Table | Fields |
|---|---|
| `runs` | `id` (uuid) · `goal` · `status` (queued\|running\|awaiting_approval\|completed\|failed) · `started_at` · `ended_at` · `operator_id` · `error` · `params` (json) |
| `run_steps` | `id` · `run_id` · `seq` (int) · `type` (thought\|tool_call\|tool_result\|message) · `tool_name` · `payload` (json) · `created_at` |
| `proposals` | `id` (uuid) · `run_id` · `action_type` · `status` (pending\|approved\|rejected\|executed\|failed\|expired) · `payload` (json) · `payload_hash` · `rationale` · `stripe_invoice_id` · `customer_email` · `amount_due` · `currency` · `days_overdue` · `created_at` · `expires_at` |
| `approvals` | `id` · `proposal_id` · `decision` (approve\|reject) · `actor` · `note` · `edited_body` · `decided_at` · `token_nonce` |
| `executions` | `id` · `proposal_id` · `idempotency_key` (unique) · `result` (json) · `status` (pending\|succeeded\|failed) · `executed_at` · `error` |
| `audit_events` | `id` · `ts` · `actor` (agent\|operator\|gateway\|system) · `event` · `subject_type` · `subject_id` · `detail` (json) · `prev_hash` · `hash` |
| `token_nonces` | `nonce` (pk) · `proposal_id` · `idempotency_key` · `consumed_at` |
| `outbox_messages` | `id` · `proposal_id` · `to_email` · `subject` · `body` · `meta` (json) · `adapter` · `file_path` · `created_at` |

## Additions to the brief's table list, and why

Four, all deliberate and none silent.

- **`token_nonces`** — gateway-owned. Check 3 ("nonce not seen before") needs a seen-set the
  gateway owns and can make atomic; `approvals.token_nonce` records what was *minted*, which is
  a different fact. `nonce` is the primary key, so consuming one is an INSERT that either
  succeeds or collides. Its `idempotency_key` column is what lets checks 3 and 7 coexist —
  see [[02 — The Approval Boundary]].
- **`outbox_messages`** — required by section 8: the default adapter "writes the message to the
  database and to /data/outbox/".
- **`runs.params`** — the run's own parameters, so the proposal cap and the minimum days
  overdue survive a restart and the run-detail screen can show what the operator asked for.
- **`executions.status` includes `pending`** — section 5's execution step 1 writes the row
  before the external call, which sections 3 and 5 together imply.

## Two details worth getting right

### `payload_hash` is what makes approval meaningful

The operator approves a specific letter. The gateway re-hashes the payload it holds and
compares. If they differ it refuses. **Approving one letter cannot be used to send a different
one.**

Hashing is over canonical JSON — sorted keys, tight separators, real UTF-8 — so the same
content always produces the same hash and different content never does. Comparison is
constant-time and exact: there is no normalise-then-compare path, and a payload differing by a
single space is a different payload.

When the operator edits before approving, the edited text is stored on the approval record
**and** the payload hash is recomputed from the edited content *before* the token is minted.
The operator approves what they actually read.

### `audit_events` is hash-chained

Each row stores the hash of the previous row. It is append-only, with **no update or delete path
anywhere in the codebase** — `tests/test_audit_chain.py` scans the shipped source to keep it
that way, ignoring docstrings so prose about the rule does not trip its own check.

`prev_hash` and `hash` are **both UNIQUE**. That makes a forked chain *impossible* rather than
unlikely: if two processes read the same tail and both try to append, the second INSERT violates
the uniqueness of `prev_hash`, and `shared/db.py:run_in_transaction` retries the losing writer
against the new tail. The chain is written by both services and verified by either.

`verify_chain()` detects all four ways to break it:

| Tampering | Detected by |
|---|---|
| A row was edited | its recomputed hash differs |
| A row was deleted | a gap in the id sequence |
| Rows were reordered or re-linked | `prev_hash` does not match the predecessor |
| The genesis row was replaced | the first row's `prev_hash` is not the genesis constant |

To correct a mistake, **append a correcting event**. If you find yourself wanting
`UPDATE audit_events`, that is the bug.

## Proposal lifecycle

```
  agent drafts          operator reviews         gateway executes
       │                       │                        │
   [pending] ──approve──▶ [approved] ──execute──▶ [executed]
       │                       │                        │
       ├──reject──▶ [rejected] │                        └──error──▶ [failed]
       │                       │
       └──ttl elapsed──▶ [expired]

  Terminal states: rejected, executed, failed, expired.
  Only [approved] is executable, and only once.
```

`approved` being the only executable status is why `app/approval/probe.py` refuses to target
it: the "try to send without approval" demonstration may attempt any other status, and never
that one.

## Run lifecycle

```
  operator starts            agent works              queue is dealt with
       │                          │                          │
   [queued] ──picked up──▶ [running] ──proposed──▶ [awaiting_approval]
       │                          │                          │
       │                          │                          └──all decided──▶ [completed]
       │                          └──proposed nothing──▶ [completed]
       │
       └──error, cap, timeout, or the process died──▶ [failed]

  Terminal states: completed, failed.
```

Two transitions are worth knowing because their absence was a bug.

**`awaiting_approval` → `completed`.** Nothing used to make this move. The loop set
`awaiting_approval` as its last act, and a run whose every proposal had been approved and
executed still reported that it was waiting — the status described the moment the agent
stopped, not the state of the work. `settle_run_if_decided()` moves it once nothing of that
run's is pending, and is called after each decision *and* on the read paths, so rows that went
stale before it existed heal the first time anyone looks. Any mix of terminal outcomes counts:
a run whose proposals were all *rejected* is as settled as one all executed.

**`queued` / `running` → `failed`.** A run's status is a row; the thread advancing it is in a
process. A restart left rows nothing would ever finish. `abandon_orphaned_runs()` fails them at
startup, where the reasoning is exact: this process has just begun, so it owns no in-flight
runs, so anything still queued or running was abandoned. That holds for a single-instance
deployment, which is what `docker-compose.yml` describes; two app replicas against one database
would need a worker heartbeat instead. Whatever the run managed to produce — its partial
transcript, any proposals — survives; only the status is corrected.

Both write to the audit log (`run.settled`, `run.abandoned`), because a status changing without
a human asking for it should be visible.

## Schema management

`shared/schema.py` creates missing tables, adds missing **columns** with a type-appropriate
backfill, recreates their indexes, logs every change, appends it to the audit log, and raises
`SchemaDrift` on anything `ADD COLUMN` cannot express.

This exists because the gap bit during the build: `create_all` creates tables but never alters
them, so a container running against an older volume died at runtime with "no such column"
rather than at startup with something a reader could act on.

Alembic is deliberately not used for the trial — see [[08 — Decisions]] and
[[10 — Extending This]] for when to change that.

## Related

- [[02 — The Approval Boundary]] — how `payload_hash` and `token_nonces` are used
- [[06 — API Reference]] — how these tables surface over HTTP
