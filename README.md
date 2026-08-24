# Stripe Collections Agent

An agent that finds overdue Stripe invoices and drafts collection letters. **It cannot
send them.** A human reviews each letter and approves or rejects it, and only on approval
does a physically separate service perform the send.

> **Build in progress.** The full quickstart, boundary guide and Obsidian vault land in
> Phase 6. What is present today is listed under *Status* below, and everything listed as
> done is verified rather than asserted.

---

## The shape of it

```
browser ──HTTP :8000──▶  app  (agent service)  ──POST /internal/actions/execute──▶  gateway
                         STRIPE_API_KEY_READ       + X-Approval-Token  (HMAC)         STRIPE_API_KEY_WRITE
                         ANTHROPIC_API_KEY         + X-Idempotency-Key                EMAIL_ADAPTER
                         no SMTP, no write key                                        NO PUBLISHED PORT
```

Two processes, two credential sets, one crossing point. The agent can read from Stripe
and think; it holds no capability to act. The gateway can act, and cannot be persuaded —
it executes only against a signed approval it can verify, and it reads the proposal's
status from the database rather than from anything the caller sends it.

The architectural rules are in [`CLAUDE.md`](CLAUDE.md). They are enforced by tests and by
CI, not by convention:

| Rule | Enforced by |
|---|---|
| Two services, never merged | `tests/test_compose_boundary.py` |
| The gateway publishes no port | `tests/test_compose_boundary.py` |
| The agent cannot import the email adapter or write client | `.importlinter` + `tests/test_import_boundary.py` |
| The agent's environment holds no action credential | `app/guards.py` (refuses to boot) |
| `audit_events` is append-only | `tests/test_audit_chain.py` |

## Running it

```bash
cp .env.example .env      # fill in STRIPE_API_KEY_READ and APPROVAL_SIGNING_SECRET
docker compose up -d --wait
curl http://localhost:8000/healthz
curl http://localhost:9000/healthz     # refused, by design
```

Tests, import contracts and lint, all inside the container — no host Python needed:

```bash
docker compose run --rm tests                    # pytest
docker compose run --rm tests lint-imports       # the import boundary contracts
docker compose run --rm tests ruff check .
```

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton, data model, hash-chained audit log, both services, Docker | **done** |
| 2 | The action gateway: the seven checks, the executor, the outbox adapter, the refusal suite | next |
| 3 | Stripe read client, field projection, currency handling, seed script | |
| 4 | Agent loop, tool schemas, system prompt, proposal creation, guardrails | |
| 5 | Public API and the four UI screens | |
| 6 | README, boundary guide, OpenAPI, Obsidian vault, clean-machine rehearsal | |

Open items owned by the operator, not by the code:

- `STRIPE_API_KEY_READ` is currently a standard test key. The read-only split is a stated
  deliverable, so mint a **restricted** key (read on Invoices, Customers, Charges) before
  handoff. `/healthz` reports `key_kind` so the gap is visible rather than hidden.
- `ANTHROPIC_API_KEY` is unset, so the agent loop cannot run live yet. The loop, its
  guardrails and its transcript are testable without it; a live run is a key-drop away.

Test mode only, throughout. There is no live-key code path, and the default email adapter
captures letters to the database and `/data/outbox` so a clone of this repository cannot
email a real person.
