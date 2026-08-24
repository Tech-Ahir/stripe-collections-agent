---
tags: [overview]
---

# 00 — Start Here

An agent that finds overdue Stripe invoices and drafts collection letters. **It cannot send
them.** A human reviews each letter and approves or rejects it, and only on approval does a
physically separate service perform the send.

If you read one other note, read [[02 — The Approval Boundary]]. That is what this system is.

## What actually happens

1. An operator starts a run from the web UI at `localhost:8000`.
2. The agent connects to Stripe, finds overdue invoices, and decides which warrant a letter.
   It chooses which tools to call and in what order; nothing about that sequence is fixed.
3. For each invoice it judges worth pursuing it drafts a letter and places it in a proposal
   queue. **Nothing leaves the system.**
4. A human reviews each proposal, may edit the letter, and approves or rejects it.
5. On approval the public API mints an HMAC-signed token and hands it to the **action
   gateway** — a separate process with its own credentials that the agent cannot reach.
6. The gateway runs seven checks against its own copy of the state, and only then sends.

## The shape of it

```
browser ──HTTP :8000──▶  app  (agent service)  ──POST /internal/actions/execute──▶  gateway
                         STRIPE_API_KEY_READ       + X-Approval-Token  (HMAC)         STRIPE_API_KEY_WRITE
                         ANTHROPIC_API_KEY         + X-Idempotency-Key                EMAIL_ADAPTER
                         no SMTP, no write key                                        NO PUBLISHED PORT
```

Two processes, two credential sets, one crossing point.

## Where to go next

| If you want to | Read |
|---|---|
| Understand why there are two services | [[01 — Architecture]] |
| Understand the seven checks and what each one prevents | [[02 — The Approval Boundary]] |
| See what the agent can and cannot do | [[03 — Agent Design]] |
| Know how overdue invoices are found, and the currency trap | [[04 — Stripe Integration]] |
| Look up a table or a column | [[05 — Data Model]] |
| Call the API from something else | [[06 — API Reference]] |
| Run it | [[07 — Running Locally]] |
| Know why something is the way it is | [[08 — Decisions]] |
| Contribute without breaking the boundary | [[09 — Development Standards]] |
| **Add a second action type** | [[10 — Extending This]] |

## Run it in three commands

```bash
cp .env.example .env      # fill in STRIPE_API_KEY_READ and APPROVAL_SIGNING_SECRET
docker compose up -d --wait
open http://localhost:8000
```

Full instructions, including seeding test data, are in [[07 — Running Locally]].

## The one-minute version of the demo

```bash
curl localhost:9000                                   # connection refused. The gateway
                                                      # publishes no port.
docker compose exec app python scripts/show_agent_tools.py
                                                      # five tools, READ and DRAFT only,
                                                      # no send capability anywhere
docker compose exec app python scripts/demo_boundary.py
                                                      # nine attempts against the live
                                                      # gateway; one send
```

## Scope

Stripe **test mode only**. There is no live-key code path. The default email adapter captures
letters to the database and to `/data/outbox`, so cloning this repository and running the demo
cannot email a real person — see [[08 — Decisions]] on why that is the default rather than an
option.

Out of scope for this trial: real outbound email to real customers, multi-tenancy, billing,
user registration, the mobile app, and production hardening (rate limiting, secrets
management beyond environment variables, HA).
