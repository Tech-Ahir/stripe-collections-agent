# Stripe Collections Agent

An agent that finds overdue Stripe invoices and drafts collection letters. It cannot send
them. A human approves each letter, and a physically separate service performs the send.

## Non-negotiable architecture rules

1. TWO services: `app` (agent) and `gateway` (actions). Never merge them.
2. `gateway` has NO published port in docker-compose.yml. Internal network only.
3. `app` NEVER imports: the email adapter, the write-capable Stripe client,
   or anything under `gateway/`. Enforced by import-linter in CI.
4. The agent's tool schema contains READ and DRAFT tools ONLY.
   There is no send tool. Not a gated one. Not a disabled one. None.
5. The gateway verifies all seven checks in `gateway/verify.py`, in order,
   before any external call. Never skip, reorder or short-circuit them.
6. Proposal status is read from the DB by the gateway. Never trusted
   from the request body.
7. `audit_events` is append-only. No UPDATE or DELETE path anywhere.

## If a rule is inconvenient

Stop and ask. Do not simplify around it. These rules ARE the deliverable;
the client is evaluating this architecture specifically.

Concretely, do NOT do any of the following, however tempting:

- Merge the gateway into the app as a module or a router.
- Give the agent a `send_collection_letter` tool that checks approval status.
  Runtime refusal is a weaker design than absence at definition time.
- Publish the gateway's port "to make testing easier".
- Use one Stripe key for both services.
- Compare the payload hash loosely, or drop the comparison when it fails.
- Add an UPDATE to `audit_events` to fix a status. Append a correcting event instead.
- Let the model format currency, or pass raw `amount_due` into a letter.
- Hardcode the agent's tool-call sequence "for reliability". It is an agent,
  not a pipeline. The model chooses its own tools and order.

## Stack

Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX + Tailwind CDN,
Anthropic Messages API tool use, stripe SDK, Docker Compose, uv.
No Node build step. No frontend framework.

## Testing

`tests/test_boundary_refusals.py` must stay green at all times.
If a change makes a refusal test fail, the change is wrong — not the test.

`tests/test_import_boundary.py` and `lint-imports` guard rule 3.
`tests/test_audit_chain.py` guards rule 7.

## Money

Stripe returns minor units. Format in Python (`shared/money.py`). The model never does
currency arithmetic and never sees a bare integer it is expected to divide.

## Credentials

| Variable | Lives in | Notes |
|---|---|---|
| `STRIPE_API_KEY_READ` | app only | Restricted key. Read-only on Invoices, Customers, Charges. |
| `STRIPE_API_KEY_WRITE` | gateway only | Never present in the app's environment. |
| `STRIPE_API_KEY_SEED` | seed script only | Run by a human, never by a service. |
| `ANTHROPIC_API_KEY` | app only | |
| `APPROVAL_SIGNING_SECRET` | both, and nothing else | 32+ bytes. |

Test mode only, throughout. There is no live-key code path.
