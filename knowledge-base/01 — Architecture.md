---
tags: [architecture]
---

# 01 — Architecture

Two processes, two credential sets, one crossing point.

The **agent service** (`app/`) can read from Stripe and think. The **action gateway**
(`gateway/`) can act on the outside world. The agent cannot act, and the gateway cannot be
persuaded — it only executes on a signed approval it can verify.

That sentence is the entire design. Everything below is how it is held in place.

## Why two processes and not one module

A single process with a permission check has one failure mode that this design does not: the
check and the capability live in the same address space, so any bug, any refactor, any
`if debug:` reaches both. Splitting them means the agent does not merely *decline* to send —
it has no credential to send with, no code to send with, and no network path to the thing that
does.

Concretely, the agent service has:

- no write-capable Stripe key,
- no SMTP credentials and no Resend key,
- no `gateway/` directory in its container image,
- no import path to the email adapter (see [[09 — Development Standards]]).

## The four layers holding it in place

Each is independent. Removing any one leaves the other three.

| Layer | Mechanism | What it catches |
|---|---|---|
| **Static** | `.importlinter`, 7 contracts, run in CI | an import that exists in the source, even on a path no test executes |
| **Type** | `AppSettings` declares no action credential | code that tries to *read* a write key — it cannot name the field |
| **Runtime** | `app/guards.py` refuses to boot | a misconfigured compose file that hands the agent a key it should not have |
| **Filesystem** | `docker/app.Dockerfile` copies `app/` and `shared/`, never `gateway/` | code that is not there at all |

The runtime guard is worth dwelling on. If `STRIPE_API_KEY_WRITE`, `SMTP_PASSWORD` or
`RESEND_API_KEY` appears in the agent's environment, the service **fails to start**. A warning
would be discovered by nobody; a crash is discovered immediately. That trade is deliberate —
see [[08 — Decisions]].

## The crossing point

Exactly one route exists between the two services:

```
POST http://gateway:9000/internal/actions/execute
  X-Approval-Token: <base64 HMAC-SHA256 signed token>
  X-Idempotency-Key: <uuid>
  { "proposal_id": "...", "payload_hash": "sha256:..." }
```

No "resend", no "force", no "override", and no route that acts without a token. Every such
convenience would be a way to reach the outside world without an approval.

The gateway **publishes no port**. It is reachable only on the internal Docker network, which
is why `curl localhost:9000` from the host is refused. `tests/test_compose_boundary.py`
asserts that the compose file declares no `ports:` for it, because "publish the gateway port
to make testing easier" is a change no functional test would catch.

## The stack, and why

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn | A Python backend was asked for. FastAPI gives the OpenAPI document for free, and that document is a deliverable. |
| Agent | Anthropic Messages API, tool use, Claude Sonnet | Native tool-use loop. The tool schema is the agent's contract — see [[03 — Agent Design]]. |
| Stripe | `stripe` Python SDK | Official, test-mode native. |
| Persistence | SQLite via SQLAlchemy, on a Docker volume | Zero setup for a reviewer. The SQLAlchemy layer means Postgres is a URL change. |
| UI | Jinja2 + HTMX + Tailwind CDN | Served by the same FastAPI app: one image, no Node build. |
| Transport | Server-sent events for the run transcript | Simpler than websockets and sufficient for one-directional streaming. |
| Packaging | Docker, Docker Compose, uv | Reproducible, and fast to install from a pinned lock file. |

## Three container images, not two

`docker/app.Dockerfile` and `docker/gateway.Dockerfile` each contain one service and the
shared contract. A third, `docker/tests.Dockerfile`, is the only image holding both — it has
to, because the boundary tests import each side in order to prove they stay apart. It sits
behind the `test` compose profile so `docker compose up` never starts it.

## What `shared/` is allowed to be

`shared/` holds the things both sides must agree on and nothing else: the data model, the
approval-token codec, canonical hashing, the audit chain, money formatting, and the wire
schemas. Contract 5 forbids `shared/` from importing either service.

The models live there rather than under `app/` for a specific reason: the gateway must read a
proposal's status from the database, and if the models lived in the agent service the gateway
would have to import it. See [[08 — Decisions]].

## Related

- [[02 — The Approval Boundary]] — the seven checks, and the threat each one answers
- [[05 — Data Model]] — the tables
- [[09 — Development Standards]] — the import rule that keeps this true
