---
tags: [standards]
---

# 09 — Development Standards

Conventions any future contributor must follow. The first section is not negotiable; the rest is
ordinary good practice.

## The seven rules

These are in `CLAUDE.md` at the repository root as well, because that is the file an AI coding
assistant reads on every session and these are exactly the rules it will otherwise refactor away.

1. **TWO services**: `app` (agent) and `gateway` (actions). Never merge them.
2. **`gateway` has NO published port** in `docker-compose.yml`. Internal network only.
3. **`app` never imports** the email adapter, the write-capable Stripe client, or anything under
   `gateway/`. Enforced by import-linter in CI.
4. **The agent's tool schema contains READ and DRAFT tools ONLY.** There is no send tool. Not a
   gated one. Not a disabled one. None.
5. **The gateway verifies all seven checks** in `gateway/verify.py`, in order, before any
   external call. Never skip, reorder or short-circuit them.
6. **Proposal status is read from the database by the gateway.** Never trusted from the request
   body.
7. **`audit_events` is append-only.** No UPDATE or DELETE path anywhere.

### If a rule is inconvenient

Stop and ask. Do not simplify around it. These rules *are* the deliverable.

Concretely, do not: merge the gateway into the app as a module or router; give the agent a send
tool that checks approval status; publish the gateway's port to make testing easier; use one
Stripe key for both services; compare the payload hash loosely or drop it when it fails; add an
UPDATE to `audit_events` to fix a status (append a correcting event); let the model format
currency or pass raw `amount_due` into a letter; or hardcode the agent's tool-call sequence for
reliability.

## The import contracts

`.importlinter`, run by `lint-imports` in CI. Seven contracts, and the *names* are documentation:

1. The agent service cannot import the action gateway
2. The agent service cannot import the email adapter
3. The agent service cannot import the write-capable Stripe client
4. The gateway cannot import the agent service
5. Shared code depends on neither service
6. The agent subsystem cannot mint approval tokens or call the gateway
7. Only the gateway may import `smtplib`

Contracts 2 and 3 are subsumed by 1 and are written out anyway, because each answers a question
the brief asks explicitly.

Contract 6 is the subtle one: even *inside* the agent service, `app/agent/**` cannot reach
`app/approval/**`, `app/gateway_client.py` or `app/services/**`. The agent subsystem cannot mint
its own permission slip or call the gateway, even by accident.

`tests/test_import_boundary.py` adds two layers static analysis cannot give: a fresh interpreter
imports the whole agent service and asserts no `gateway.*` or mail module appears in
`sys.modules` (catching a lazy import inside a function), and the settings classes are checked to
have no field for the other side's credentials.

## Tests that must stay green

| Suite | Guards |
|---|---|
| `tests/test_boundary_refusals.py` | The seven checks. **If a change makes a refusal test fail, the change is wrong — not the test.** |
| `tests/test_import_boundary.py` | Rule 3 and contract 6 |
| `tests/test_compose_boundary.py` | Rules 1 and 2, and the credential split, read from the deployment config itself |
| `tests/test_audit_chain.py` | Rule 7, including a source scan for any mutation path |
| `tests/test_agent_tools.py` | Rule 4, including the serialised wire schema |
| `tests/test_letter_guardrails.py` | The section 8 compliance floor |
| `tests/test_money.py` | The currency trap |

Run everything in the container, which is the canonical Python 3.12 runtime:

```bash
docker compose run --rm tests
docker compose run --rm tests lint-imports
docker compose run --rm tests ruff check .
```

## Writing tests here

- **Assert the property, not the implementation.** `test_the_gateway_publishes_no_port` reads the
  compose file; it does not mock a socket.
- **Test the negative path first.** For anything on the approval path, the refusal test is the
  deliverable and the happy path is the easy part.
- **A guard test should fail when the guard is removed.** Several tests exist purely to catch a
  future deletion: the source scan for `UPDATE audit_events`, the contract-count check that
  notices a silently skipped import contract, the `0 broken` assertion that notices
  `lint-imports` running vacuously.
- **Pin measured premises.** Where behaviour depends on a measurement of someone else's system —
  Stripe's link stability, for instance — assert the measurement too, so a change upstream fails
  a test instead of quietly weakening a check.
- **Prefer real objects to mocks at boundaries.** Dict fixtures for Stripe objects hid a real bug
  (`dict(StripeObject)` raises); the projections are now exercised against genuine
  `StripeObject`s as well.

## Code conventions

- Python 3.12 in the containers. Source stays importable on 3.11 where that is free, so `ruff`'s
  UP047 (PEP 695 generics) is the one rule disabled, with a comment saying why.
- `ruff check` and `ruff format` clean, line length 100.
- Type hints on anything crossing a module boundary.
- Docstrings explain **why**, not what. Several quote the brief directly, because "the spec says
  this and here is the line that does it" is the most useful comment this codebase can carry.
- Money is `int` minor units plus a preformatted display string, never a float. See
  [[04 — Stripe Integration]].
- All timestamps are timezone-aware UTC, through `shared/clock.py` and the `UtcDateTime` column.
  Naive datetimes do not get past those two files.
- Structured errors over exceptions at the agent boundary: a tool failure the model can act on is
  data, not a crash.

## Committing

- Commit at the end of each phase, and push before signing off, so review happens on real code
  rather than a description of it.
- Commit messages say what was verified and how, not just what changed. If a claim is not
  verified, the message says that too.
- Never commit `.env`. It is gitignored, `.dockerignore`d, and worth checking anyway:
  `git diff --cached | grep -E 'sk_(test|live)_'`.

## Before you call anything done

1. `docker compose down -v`, then up from clean. Everything works with no manual steps beyond
   `.env`.
2. Run the eight-step demo in [[07 — Running Locally]], in order.
3. `curl localhost:9000` from the host: refused.
4. `scripts/show_agent_tools.py`: no send capability.
5. `tests/test_boundary_refusals.py`: read the seven test names against the table in
   [[02 — The Approval Boundary]].
6. Open this vault and click through every link.

## Related

- [[02 — The Approval Boundary]] — what the refusal tests protect
- [[08 — Decisions]] — why these rules exist in this shape
- [[10 — Extending This]] — how to add a capability without weakening any of it
