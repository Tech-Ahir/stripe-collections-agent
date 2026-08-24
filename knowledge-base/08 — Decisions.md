---
tags: [decisions]
---

# 08 — Decisions

One entry per decision: the context, the choice, and what was rejected. Written so that a
future contributor who disagrees can disagree with the *reasoning* rather than rediscover it.

Several of these were made *against* the more obvious option. Those are the ones worth reading.

---

## 1. Two processes, not one process with a permission check

**Context.** The requirement is that the agent cannot send. A single service with
`if approved: send()` satisfies that requirement on a good day.

**Choice.** Two containers, two credential sets, one HTTP crossing point.

**Rejected.** A single process with an authorisation check. It fails differently: the check and
the capability share an address space, so one bug, one refactor, one `if DEBUG` reaches both.
Splitting them means the agent does not decline to send — it holds no credential, contains no
sending code, and has no network path to the thing that does.

**Cost, honestly.** More moving parts, a shared secret to manage, and an HTTP hop on the
approval path. Worth it here because the boundary *is* the product. In a system where it were
merely a nice property, this would be over-engineering.

---

## 2. The gateway publishes no port

**Context.** Testing the gateway directly is convenient. Publishing 9000 would make it easier.

**Choice.** No `ports:` entry. The gateway is reachable only on the internal Docker network.

**Rejected.** Publishing it "for testing". `curl localhost:9000` returning a connection refused
is the single most legible demonstration this system has, and a published port silently deletes
it. Tests reach the gateway through `docker compose exec app`, or through the internal network
in `scripts/demo_boundary.py`, which is barely less convenient.

`tests/test_compose_boundary.py` asserts the absence, because no functional test would notice
the port appearing.

---

## 3. The data model lives in `shared/`, not under `app/`

**Context.** The brief's suggested layout puts `store/` under `app/`. But the gateway must read
a proposal's status from the database — that is check 4, the check that matters.

**Choice.** Models, engine and session management in `shared/`.

**Rejected.** Models under `app/`, with the gateway importing them. That would make the gateway
depend on the agent service, which is the wrong direction: a future import inside `app/store/`
could pull agent code, a Stripe client, or an Anthropic client into the gateway's process.
`shared/` is constrained by contract 5 to depend on neither service, so it cannot drag anything
across.

---

## 4. No JWT library

**Context.** The approval token needs signing and verifying. `pyjwt` is one line.

**Choice.** About sixty lines of hand-rolled HMAC-SHA256 in `shared/approval_token.py`.

**Rejected.** A JWT library. Three reasons, in order of weight:

1. **There is no `alg` field to attack.** The most common JWT vulnerability is `alg: none` or an
   algorithm downgrade. This format has one algorithm, not negotiable, and no header at all.
2. **A reviewer can read the whole thing.** Sixty lines that can be read end to end beat a
   dependency whose defaults have to be audited — and this file is the commercial value of the
   trial.
3. `extra="forbid"` on the claims means a token carrying an unexpected field is *refused*
   rather than accepted with the extra ignored, which is the opposite of most libraries' default.

**Cost.** Hand-rolled crypto is normally a bad sign. What makes it defensible is the narrowness:
one algorithm, one key, one message shape, constant-time comparison, and a dedicated test file
that changes every claim individually to prove the signature covers all of them.

---

## 5. Expiry is not checked inside `decode()`

**Context.** It would be natural for a token decoder to reject an expired token.

**Choice.** `decode()` verifies the signature and parses. Expiry is check 2, in the gateway.

**Rejected.** Folding expiry into decoding. The brief gives signature failure and expiry
*different refusal codes*, and the gateway must be able to report which one happened. One
function that answers "invalid" to both questions collapses two distinct answers into one.

---

## 6. A manual agent loop, not the SDK's tool runner

**Context.** The Anthropic SDK ships a tool runner that drives the tool-call loop.

**Choice.** `app/agent/loop.py`, written out.

**Rejected.** The tool runner. Three things it hides are exactly what this file must expose:

- every tool call persisted as a numbered transcript row **as it happens**, because the
  transcript is a deliverable and a failed run must leave a usable partial one;
- the caps enforced *mid-loop*, so the 26th tool call fails the run cleanly rather than after
  the fact;
- a control flow a reviewer reads top to bottom.

It also avoids a beta dependency on the one code path the client is evaluating.

---

## 7. Guardrails enforced in code, not only in the prompt

**Context.** The brief asks for the compliance rules in the system prompt *and* in a test suite.

**Choice.** Both, plus enforcement at the tool boundary: a letter that breaks a rule is refused
by `propose_collection_letter` and returned to the agent as a **correctable** error.

**Rejected.** Prompt-only. The brief itself sets the precedent — "a duplicate is rejected by the
store, not by the prompt" — and the same logic applies to compliance. A prompt is a request; a
guardrail is a rule.

**The half of this that is easy to get wrong.** A guardrail tuned only to block threats will
also block the brief's own tone ladder: "final notice" and "escalation" are its vocabulary for
the 60-day letter. There are as many tests asserting that firm language *passes* as asserting
that threats fail.

---

## 8. Payment links are compared on a prefix, not exactly

**Context.** Section 8 says a letter's payment link is "never fabricated". Exact comparison is
the obvious implementation.

**Choice.** Compare a 120-character identity prefix of scheme + host + path.

**Why the obvious thing is impossible.** **Stripe reissues `hosted_invoice_url` on every read.**
Exact matching rejected a letter carrying a perfectly genuine link, because the link had been
reissued between the read and the draft. Measured against live test mode: two reads of the same
invoice share 140 of 159 characters; two *different* invoices diverge at 92. Any threshold in
(92, 140] distinguishes invoices while tolerating a reissue.

**Rejected — and this one matters.** Rewriting the letter to the newest link at proposal time.
It would have made exact comparison work. It would also mean text changes *after* the guardrail
approved it, so the operator reviews one thing and the customer receives another — which is
precisely the failure check 6 exists to prevent. Comparing loosely and never rewriting is the
weaker check and the stronger guarantee.

A test pins both measured numbers, so if Stripe's format changes the test fails rather than the
guardrail quietly weakening.

---

## 9. The unapproved-send probe mints a *real* token

**Context.** Section 9 requires a "try to send without approval" button.

**Choice.** It mints a correctly signed token for a proposal that is not approved, and the
gateway refuses it at check 4.

**Rejected.** Sending a forged token. That returns `401 invalid_signature` and proves only that
HMAC works, which any HMAC does. Signing properly means checks 1, 2 and 3 all pass and the
refusal comes from the gateway reading the status out of its own database — which is the claim
this architecture actually makes, and what acceptance criterion 5 describes.

**The constraint that makes it safe.** `app/approval/probe.py` is a separate module from the
token minter, precisely because it breaks that module's invariant, and it refuses to target the
one status that could execute (`approved`). Pending, rejected, expired and executed are all
fair game — which is what lets the demo's "reject one, then attempt execution" step reach the
gateway at all.

The security property never depended on the app being unable to mint such a token. It depends on
the gateway not believing it.

---

## 10. `outbox` is the default email adapter, and `resend` is not merely off

**Context.** Three adapters: capture locally, SMTP, or real delivery.

**Choice.** `outbox` by default. `resend` requires an explicit key and **raises at construction
if it is missing**, rather than falling back.

**Rejected.** Defaulting to SMTP with a localhost fallback, or letting `resend` degrade to
capture when unconfigured. Someone clones this repository, runs the demo, and approves a letter
addressed to a real customer in the seeded data. The default has to be the one where that is
harmless. And an adapter that silently stops sending is as bad as one that silently starts: a
typo in `EMAIL_ADAPTER` fails at startup rather than changing what happens to a letter.

---

## 11. `create_all` plus an additive migrator, not Alembic

**Context.** Section 3 asks for zero setup for a reviewer. `create_all` gives that and does not
alter existing tables.

**Choice.** `shared/schema.py`: create missing tables, add missing columns with a
type-appropriate backfill, recreate their indexes, log every change, append it to the audit log,
and raise `SchemaDrift` on anything `ADD COLUMN` cannot express.

**Rejected.** Plain `create_all` (the gap bit during the build — a container against an older
volume died at runtime with "no such column" rather than at startup with something actionable),
and Alembic (real migration machinery for a three-day trial with one SQLite file, and another
thing for a reviewer to run).

**When to change this.** The moment the schema changes under a database holding data anyone
cares about. See [[10 — Extending This]].

**One detail worth keeping.** A backfilled `token_nonces.idempotency_key` is `''`, which can
never equal a real key — so an old nonce reads as consumed by some other request and check 3
*refuses*. When a backfill has to guess, it should guess towards refusing.

---

## 12. The SSE stream tails the database

**Context.** Streaming a live transcript usually means an in-process queue per run.

**Choice.** The endpoint polls `run_steps` for rows past a sequence number.

**Rejected.** An in-memory queue or pub/sub. Three things fall out of the database approach for
free: a reconnecting browser resumes from `?after=` with nothing lost, several tabs can watch
one run, and the stream is a *view* over the persisted transcript rather than a second copy of
it that could disagree with it. The transcript is the deliverable; the stream should not be a
parallel truth.

**Cost.** A 400ms poll per connected client. Irrelevant at one operator, and the shape to
revisit first if this ever serves many.

---

## 13. `audit_events.prev_hash` is UNIQUE

**Context.** Two processes append to one hash chain. The usual answer is a lock or a serialising
transaction.

**Choice.** `prev_hash` and `hash` are both UNIQUE, and `run_in_transaction` retries a losing
writer against the new tail.

**Rejected.** Relying on transaction isolation. A UNIQUE constraint makes a forked chain
*impossible* rather than unlikely, independently of isolation level, database engine, or whether
a future contributor understands SQLite's locking. A test proves it by trying: two rows claiming
the same predecessor is rejected at the storage layer.

A pleasant consequence: it also blocks a whole class of tampering. Re-pointing a row's
`prev_hash` at a row that already has a successor cannot even be stored, so `verify_chain` never
has to catch that one.

---

## 14. The test-clock scan is opt-out, not absent

**Context.** Section 7 wants a fixture spanning 3 to 95 days overdue *and* a purely server-side
`due_date` filter. Stripe makes those mutually exclusive: back-dated due dates are refused, test
clocks are the only way round it, and clock objects are omitted from unfiltered list calls while
`invoices.list` offers no `test_clock` filter.

**Choice.** Additionally run the *identical* server-side filter scoped per test-clock customer.
`STRIPE_INCLUDE_TEST_CLOCK_INVOICES=false` turns it off; it is inert in live mode.

**Rejected.** Paging the whole invoice list and filtering in Python (the brief forbids it, and
rightly), and dropping the overdue spread to whatever Stripe will create directly — which would
have collapsed the tone ladder to a single tone and gutted the demo.

**Flagged, not hidden.** This is a genuine tension inside the specification rather than an
implementation choice, and it is the kind of thing the brief says to raise rather than decide
silently in code. The requirement that mattered — no client-side filtering — is intact; the only
addition is a `customer` scope, because that is the only handle Stripe gives on a clock's
objects.

---

## 15. A refusal answers with the gateway's HTTP status

**Context.** When the gateway refuses, the app's own call succeeded. Returning 200 with the
refusal in the body is defensible.

**Choice.** Mirror the gateway's status: 403, 409 or 401 as the case may be.

**Rejected.** Always 200. Acceptance criterion 5 is worded as an HTTP fact — "returns 403
not_approved" — and a reviewer checks it with curl. A 200 carrying a buried refusal reads as a
system that swallowed it.

The approval itself still stands: a human decided, and the gateway declining to act does not
unmake that.

---

## 16. The scripted fixture run is badged as scripted

**Context.** Without an Anthropic key the queue is empty, and the approval flow, the refusal
button and the audit chain are all unreviewable.

**Choice.** `scripts/dev_seed_run.py` drives the **real** loop — same tools, same guardrails,
same store — with a fixed script in place of the model, over live Stripe data. The run's goal
says so, its first transcript entry says so, and the UI badges it "scripted fixture".

**Rejected.** A quiet fixture that looks like a real run. Section 12 warns about changes that
"demo identically" while failing the evaluation, and a fake agent run passed off as a real one
is the same failure in the other direction. There is deliberately **no environment variable**
that swaps a scripted model in at runtime: a reviewer either has a key and sees a real agent, or
has none and is told so plainly.

---

## 17. Summarised thinking is requested explicitly

**Context.** Section 9 wants reasoning rendered as prose in the transcript.

**Choice.** `thinking={"type": "adaptive", "display": "summarized"}`.

**Rejected.** The default. On Claude Sonnet 5 `display` defaults to `"omitted"`, which would
have left every reasoning row empty — and the tempting repair is to have the UI narrate around
the tool calls instead, which would be invented narration presented as the model's reasoning.
Asking for the summary gets the real thing.

(`budget_tokens` is rejected outright by this model family; adaptive thinking replaces it.)
