---
tags: [stripe]
---

# 04 — Stripe Integration

## Identifying overdue invoices

An invoice is overdue when its status is `open` and its `due_date` is in the past.

```python
client.v1.invoices.list(params={
    "status": "open",                       # finalized, still owed
    "due_date": {"lt": int(time.time())},   # past due
    "limit": 100,
    "expand": ["data.customer"],
})
```

Filtering happens **server-side**. `status="open"` means finalized with a balance remaining.
Stripe's dashboard shows these as *Past due* once the due date passes, but past-due is a
display badge rather than a distinct API status, so the `due_date` filter is what does the
work.

## Field mapping

| Stripe field | Projected as | Use |
|---|---|---|
| `id`, `number` | `id`, `number` | Identity; the number appears in the letter |
| `customer` (expanded) | `customer_name`, `customer_email` | Addressing |
| `amount_due` | `amount_due` + `amount_due_display` | Minor units **and** a formatted string |
| `amount_remaining` | `amount_remaining` + display | |
| `currency` | `currency` | Lowercased ISO code |
| `due_date` | `due_date` (ISO date) + `days_overdue` | Days derived in Python |
| `hosted_invoice_url` | `hosted_invoice_url` | The payment link. Never fabricated |
| `collection_method`, `attempt_count` | same | Whether automatic collection is still retrying |
| `status_transitions.finalized_at` | `finalized_at` | Age of the receivable |

Two projected fields have no Stripe counterpart, and both exist so the prompt does not have to
remember a rule:

- **`deliverable`** — false when the customer has no email address, with
  `not_deliverable_reason` explaining it. The brief says "if null, the agent must not propose a
  letter"; saying so *in the data* is stronger than saying so in the prompt.
- **`automatic_collection_note`** — present when `collection_method="charge_automatically"` and
  `attempt_count > 0`, so the agent can reason about a card that is mid-retry rather than
  dunning someone whose payment may be about to succeed.

## Currency handling

> Stripe returns **minor units**. 2500 is $25.00.

Formatting happens once, in Python, in `shared/money.py`, using the currency's exponent — and
the model is handed a preformatted `amount_due_display` string alongside the integer. **The
model never divides by anything.** This is the single most likely source of an embarrassing
error in a demo, so:

- Not every currency has two decimal places. JPY, KRW, VND and eleven others have none, so
  2500 JPY is ¥2,500 — dividing by 100 would understate it a hundredfold. KWD, BHD and three
  others have three.
- Scaling is done by exponent shift on a `Decimal`, never by division on a float. A float
  amount **raises**, because a float arriving here means minor units were already lost upstream.
- `guardrails.py` refuses any letter containing the raw minor-unit integer, and
  `tests/test_money.py` asserts the brief's own failure case: a $2,500 invoice must not render
  as $250,000.

## The payment link drifts, and that had to be handled

**Stripe reissues `hosted_invoice_url` on every read.** The trailing characters carry a
per-read timestamp and nonce.

This was found the hard way: the guardrail compared links exactly, and rejected a letter
carrying a perfectly genuine link because Stripe had reissued it between the read and the
draft. Exact matching here is not merely strict — it is unimplementable.

Measured against live test mode:

- two reads of the **same** invoice share 140 of 159 characters;
- two **different** invoices diverge at character 92.

So the guardrail compares a **120-character identity prefix** of scheme + host + path: strict
enough to reject a different invoice, a different Stripe account or a different host, tolerant
of a reissue. `tests/test_letter_guardrails.py::test_the_measured_premise_still_holds` pins
those two numbers, so if Stripe's format changes the test says so rather than the guardrail
quietly weakening.

The letter is **not** rewritten to the newest link. Substituting text after the guardrail has
approved it would mean the operator reviews one thing and the customer receives another, which
is the failure check 6 exists to prevent.

## Keys

| Variable | Scope |
|---|---|
| `STRIPE_API_KEY_READ` | Agent service. **Restricted** key, read-only on Invoices, Customers, Charges. |
| `STRIPE_API_KEY_WRITE` | Gateway only. Needed solely if `ENABLE_STRIPE_INVOICE_SEND` is on. |
| `STRIPE_API_KEY_SEED` | The seed script only. Run by a human, never by a service. |

`/healthz` reports which *kind* of read key it was given, so running with a standard key
instead of a restricted one is visible rather than hidden — and the UI header flags it.

## Test-mode fixtures, and four Stripe constraints

`scripts/seed_stripe_test_data.py` creates six customers and eight invoices with overdue ages
spread from 3 to 95 days, including one customer with no email and one invoice already paid.

```bash
docker compose run --rm seed scripts/seed_stripe_test_data.py --recreate  # replace ours
docker compose run --rm seed scripts/seed_stripe_test_data.py --list      # what the agent sees
docker compose run --rm seed scripts/seed_stripe_test_data.py --destroy   # remove ours
```

The `seed` service is a one-off container behind a compose profile, and the **only** place
`STRIPE_API_KEY_SEED` appears. `app/guards.py` refuses to start the agent service if it can
see that key: seeding needs write access, and "it is only used by a script" describes intent
rather than capability.

Every constraint below was discovered by trying, and each shapes the script:

1. **`due_date` must be in the future** — at creation *and* on update. A genuinely
   95-days-overdue invoice cannot be created directly at all. The fixture is therefore built on
   a **test clock** frozen in the past: relative to the clock the due dates are in the future,
   and in real time they are already overdue.
2. **Objects on a test clock are omitted from unfiltered list calls**, and `invoices.list`
   offers no `test_clock` filter — so the fixture the brief asks for is invisible to the query
   the brief specifies. Resolved by additionally running the *identical* server-side filter
   scoped per test-clock customer. Only a `customer` scope is added, because that is the only
   handle Stripe gives on a clock's objects; nothing is paged and filtered in Python. Controlled
   by `STRIPE_INCLUDE_TEST_CLOCK_INVOICES`, and inert in live mode where no test clocks exist.
3. **A test clock holds at most three customers**, so six need two clocks. Both carry the
   fixture's name, which is how `--destroy` removes exactly our data and nothing that was
   already in the account.
4. **`send_invoice` requires the customer to have an email**, so the no-email customer is
   created with a placeholder, its invoice finalized, and the address then removed. Stripe keeps
   the invoice's finalized email *snapshot*, which settled a real question:
   `project_invoice` prefers the customer's **current** address, because the question is whether
   a letter can be sent today and only the customer record answers that.

A note on `attempt_count`: every fixture invoice uses `collection_method="send_invoice"`,
because Stripe only accepts `due_date` on that method — so a genuinely retrying invoice cannot
also be an overdue one. The projection handles a non-zero `attempt_count` and a unit test covers
it against a fixture.

## Failures

- Exponential backoff with jitter on `RateLimitError`, three attempts.
- Any other `StripeError` becomes a **structured error returned to the agent**, not a crash. The
  agent can note the failure and continue with the invoices it already has.
- Customer lookups are cached for the lifetime of a run, and the overdue query warms that cache
  from the expanded customer it already holds.
- `StripeObject` is **not** a mapping in stripe-python 15 — `dict(obj)` raises. Dict-based
  fixtures hid that until live data broke `project_customer` on first contact, so every
  projection is now also exercised against genuine `StripeObject`s.

## Related

- [[03 — Agent Design]] — the tools these projections feed
- [[08 — Decisions]] — why the test-clock scan is opt-out rather than absent
