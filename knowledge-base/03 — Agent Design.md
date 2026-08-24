---
tags: [agent]
---

# 03 — Agent Design

## Tool classification

Every tool is one of three kinds. The classification is declared in code
(`app/agent/tools/base.py`) and it is the thing a reviewer checks.

| Class | Effect | Tools |
|---|---|---|
| **READ** | No external effect. Auto-executes. | `list_overdue_invoices`, `get_invoice`, `get_customer`, `get_payment_history` |
| **DRAFT** | Writes internal records only. Auto-executes. | `propose_collection_letter` |
| **ACTION** | Reaches the outside world. **Not exposed to the agent at all.** | `send_collection_letter` — lives in the gateway, invocable only by an approved proposal |

## Read this twice

`send_collection_letter` is **not** a tool the agent has and is refused. It is **absent from
the agent's tool schema entirely**. The agent's only route to the outside world is to create a
proposal that a human may later approve.

Refusal at runtime is a weaker design than absence at definition time.
`ToolSpec.__post_init__` **raises** if a tool is declared `ACTION`, so an ACTION tool cannot be
constructed, let alone registered.

`ToolClass.ACTION` exists as a name so the classification is complete for a reader. Nothing is
ever registered with it, and tests assert that.

Check it yourself in five seconds:

```bash
docker compose exec app python scripts/show_agent_tools.py
```

It prints every tool, its class, and a pass/fail summary: five tools, classes DRAFT and READ,
zero ACTION tools, no tool name containing send/email/deliver/dispatch/execute, and
`send_collection_letter` absent from the serialised wire JSON.

## The loop

`app/agent/loop.py`, written out rather than delegated to the SDK's tool runner. See
[[08 — Decisions]] for why.

1. The operator starts a run with a goal, either the default or free text.
2. The system prompt establishes the role, the constraints, and the rule that it cannot send.
3. The agent calls read tools to gather facts. **It decides which and how many.**
4. For each invoice it judges worth pursuing, it drafts a letter and calls
   `propose_collection_letter`.
5. Every tool call and result is persisted as a `run_step` and streamed to the UI.
6. The agent produces a closing summary. The run becomes `awaiting_approval`.
7. The run is over. Approval happens on the operator's schedule, not inside the loop.

Nothing prescribes an order. The tests drive the loop with several different sequences on
purpose: some runs pull payment history first, some skip it. That variability is the proof that
this is an agent and not a pipeline.

## Tool schemas

```
list_overdue_invoices(min_days_overdue: int|null = 1,
                      limit: int|null = 25,
                      min_amount_cents: int|null = None) -> Invoice[]
get_invoice(invoice_id: str) -> Invoice        # full detail, line items, payment attempts
get_customer(customer_id: str) -> Customer    # name, email, created, delinquent, metadata
get_payment_history(customer_id: str,
                    limit: int|null = 10) -> Payment[]
propose_collection_letter(invoice_id: str, subject: str, body: str,
                          tone: friendly|firm|final,
                          rationale: str) -> ProposalRef
```

Every schema is closed (`additionalProperties: false`) and fully required, with `strict: true`,
so tool inputs validate exactly and handlers need no defensive parsing. Optional parameters are
typed nullable rather than omitted, because strict mode requires every property in `required`.

`propose_collection_letter` **persists a proposal in status=pending and returns a proposal_id.
NO EXTERNAL EFFECT.** It is the agent's terminal capability.

## Facts are injected, never recalled

Section 8 of the brief: "Facts are injected by the tool layer, never recalled by the model."

The registry captures each invoice's projected facts as the agent reads them, keyed by invoice
id (`app/agent/tools/registry.py`). When the agent later drafts a letter, the figures written
into the stored payload come from **that capture**, not from anything the model wrote — and the
guardrails check the letter's text against it.

A consequence: proposing for an invoice the agent has not read in this run is refused, because
there are no facts to build a letter from.

## What the system prompt establishes

`app/agent/prompts.py`. At minimum, and all of it deliberately:

- **Role** — a collections assistant working on behalf of the account holder.
- **That it cannot send anything**, stated plainly and early, and that every letter is reviewed
  by a human before it goes anywhere. This changes how the model writes.
- **Escalation keyed to days_overdue** — courteous in the first fortnight, firmer past thirty
  days, final notice past sixty.
- **Tone calibrated by payment history** — a reliable customer nine days late is not addressed
  like a habitual non-payer.
- **A required rationale** on every proposal explaining why this invoice and why this tone.
- **Never invent amounts, dates, invoice numbers or payment links.** Every figure must come
  from a tool result.
- The compliance prohibitions of section 8, which are also enforced in code.

What the prompt does *not* do is prescribe a sequence.

## Guardrails

| Guardrail | Where | Behaviour |
|---|---|---|
| Max 25 tool calls per run | `loop.py`, from `MAX_TOOL_CALLS_PER_RUN` | Fails the run cleanly, partial transcript intact |
| Max 10 proposals per run | the draft tool, from `MAX_PROPOSALS_PER_RUN` | Correctable tool error; the run continues and summarises |
| One pending proposal per invoice | a partial unique index on `proposals` | Refused by the **store**, not by the prompt |
| Proposals expire after 72 hours | `PROPOSAL_TTL_HOURS` | Applied lazily on read and at approval time |
| No forbidden claims, no invented figures | `app/agent/guardrails.py` | Correctable tool error, so the agent can rewrite in the same run |
| No letter for an undeliverable invoice | the draft tool | Refused with the reason from the read layer |
| No proposal without a rationale | the draft tool | Refused |

Every refusal comes back as a **correctable** error with the reason spelled out, so the model
can fix the letter and call again. `tests/test_agent_loop.py` proves that round trip: a letter
containing a legal threat is rejected, rewritten, and accepted, and only one proposal exists at
the end.

## Compliance

Collection correspondence is regulated. Five families of claim are refused outright: threats of
legal action, credit reporting, third-party escalation, fees or interest, and threats to
suspend or terminate service. Fees and interest are refused categorically because **no fee or
interest figure exists anywhere in the data this system reads**, so any such claim is
necessarily invented.

Equally important, and tested just as heavily: legitimate firm language still passes. "Final
notice" and "escalation" are the brief's own vocabulary for the 60-day letter, and a guardrail
that blocked them would make the tone ladder unwritable — a different bug with the same cause.

> **For the client's counsel, not for us.** What `guardrails.py` provides is a mechanical
> floor: the specific phrasings it knows about cannot get through. Whether this system's
> correspondence falls within FDCPA scope, and what that requires, is a legal question for your
> counsel. A determined model can reword a threat in a way no phrase list anticipates, which is
> why the human approval step is the real control and this is the backstop.

## Related

- [[02 — The Approval Boundary]] — what happens after a human approves
- [[04 — Stripe Integration]] — where the facts come from
- [[10 — Extending This]] — adding a tool without adding a capability
