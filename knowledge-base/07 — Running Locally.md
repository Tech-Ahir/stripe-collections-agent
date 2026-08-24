---
tags: [howto]
---

# 07 — Running Locally

Docker is the only host dependency. No Python, no Node, no database to install.

## Ten minutes to a working demo

```bash
# 1. Configure
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste into APPROVAL_SIGNING_SECRET
#   also set STRIPE_API_KEY_READ (a restricted rk_test_... key)
#   and STRIPE_API_KEY_SEED (your standard sk_test_... key, for the fixture only)
#   ANTHROPIC_API_KEY is needed only to run the agent live

# 2. Bring it up
docker compose up -d --build --wait

# 3. Seed Stripe test mode: six customers, eight invoices, 3 to 95 days overdue
docker compose run --rm --entrypoint python tests scripts/seed_stripe_test_data.py --recreate

# 4. Open it
#    http://localhost:8000
```

Then either start a run from the dashboard (needs `ANTHROPIC_API_KEY`), or populate the queue
without a model:

```bash
docker compose exec app python scripts/dev_seed_run.py
```

## Verifying the boundary from a terminal

```bash
# The gateway publishes no port. This must fail.
curl localhost:9000

# It answers on the internal network only.
docker compose exec app python -c "import urllib.request; \
  print(urllib.request.urlopen('http://gateway:9000/healthz').status)"

# The agent's complete toolset, with a pass/fail summary.
docker compose exec app python scripts/show_agent_tools.py

# Nine attempts against the live gateway; exactly one send.
docker compose exec app python scripts/demo_boundary.py
```

## Tests, lint and the import contracts

All inside the container:

```bash
docker compose run --rm tests                                   # the whole suite
docker compose run --rm tests pytest tests/test_boundary_refusals.py -v
docker compose run --rm tests lint-imports                      # the 7 import contracts
docker compose run --rm tests ruff check .
```

`tests/test_boundary_refusals.py` must stay green at all times. If a change makes a refusal
test fail, **the change is wrong, not the test.**

## Inspecting what happened

```bash
# The captured letters, on disk inside the gateway
docker compose exec gateway ls -la /data/outbox
docker compose exec gateway sh -c 'cat /data/outbox/*.eml'

# The audit chain
curl -s localhost:8000/v1/audit/verify

# Or in the UI: /audit, then press "Verify chain"
```

## Configuration

| Variable                             | Default                | Notes                                                                 |
| ------------------------------------ | ---------------------- | --------------------------------------------------------------------- |
| `STRIPE_API_KEY_READ`                | —                      | **Required.** Restricted, read-only. Agent service only.              |
| `APPROVAL_SIGNING_SECRET`            | —                      | **Required**, 32+ bytes. Shared by both services and nothing else.    |
| `ANTHROPIC_API_KEY`                  | —                      | Needed only for a live agent run.                                     |
| `STRIPE_API_KEY_SEED`                | —                      | The seed script only. Never read by a service.                        |
| `STRIPE_API_KEY_WRITE`               | empty                  | Gateway only. Needed only if invoice send is enabled.                 |
| `ANTHROPIC_MODEL`                    | `claude-sonnet-5`      |                                                                       |
| `EMAIL_ADAPTER`                      | `outbox`               | `outbox` \| `smtp` \| `resend`                                        |
| `ENABLE_STRIPE_INVOICE_SEND`         | `false`                |                                                                       |
| `PROPOSAL_TTL_HOURS`                 | `72`                   |                                                                       |
| `MAX_TOOL_CALLS_PER_RUN`             | `25`                   |                                                                       |
| `MAX_PROPOSALS_PER_RUN`              | `10`                   |                                                                       |
| `STRIPE_INCLUDE_TEST_CLOCK_INVOICES` | `true`                 | Makes the test-mode fixture visible. See [[04 — Stripe Integration]]. |
| `ENABLE_UNAPPROVED_ATTEMPT_DEMO`     | `true`                 | The "try to send without approval" button.                            |
| `OPERATOR_ID`                        | `operator@servicia.ai` | The single operator identity.                                         |

Compose refuses to start if `STRIPE_API_KEY_READ` or `APPROVAL_SIGNING_SECRET` is unset, rather
than booting into a state where tokens cannot be verified.

## Seeing a real email arrive

The default adapter captures letters and nothing leaves the machine. To watch a real SMTP
delivery into a real inbox, with still no external delivery:

```bash
docker compose --profile smtp up -d           # starts Mailpit
# set EMAIL_ADAPTER=smtp in .env, then:
docker compose up -d --force-recreate gateway
# approve a proposal, then open http://localhost:8025
```

`EMAIL_ADAPTER=resend` sends for real. It is off by default, requires an explicit key, and is
the only configuration in which this system can reach a real inbox. See [[08 — Decisions]].

## Starting completely clean

```bash
docker compose down -v          # removes the volume, so the database goes too
docker compose up -d --build --wait
```

The Stripe fixture is **not** in that volume — it lives in your Stripe test account. Remove it
separately:

```bash
docker compose run --rm --entrypoint python tests scripts/seed_stripe_test_data.py --destroy
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `curl localhost:9000` connects | The gateway has been given a published port. That is a bug — see [[09 — Development Standards]]. |
| The agent service will not start, complaining about credentials | It can see `STRIPE_API_KEY_WRITE`, `SMTP_PASSWORD` or `RESEND_API_KEY`. Those belong to the gateway. |
| `/healthz` says `anthropic: not_configured` | `ANTHROPIC_API_KEY` is unset. Everything except a live run still works. |
| The header says "read key is not restricted" | You are using a standard `sk_test_` key. Mint a restricted one; the read-only split is a stated deliverable. |
| The queue is empty after a run | The agent may have judged nothing worth pursuing — read its closing summary on the run screen. Or the fixture is missing: `seed_stripe_test_data.py --list`. |
| A run fails immediately | Almost always a missing `ANTHROPIC_API_KEY`. The run's error field says so verbatim. |
| "no such column" at runtime | An old volume against newer code. Startup normally migrates additively; if it raised `SchemaDrift`, use `docker compose down -v`. |

## Related

- [[00 — Start Here]] — the three-command version
- [[06 — API Reference]] — what to call
- [[09 — Development Standards]] — before you change anything
