"""Populate one run and its proposals WITHOUT calling a model.

    docker compose exec app python scripts/dev_seed_run.py

Why this exists: ANTHROPIC_API_KEY may not be set yet, and the approval flow, the refusal
button, the audit chain and all four screens should be reviewable regardless. This drives the
**real** agent loop -- the same run_agent, the same tool registry, the same guardrails, the
same proposal store -- with the model replaced by a fixed script of turns.

**It is labelled as scripted everywhere it could be mistaken for a real run.** The run's goal
says so, its first transcript entry says so, and ``params.scripted`` is true so the UI badges
it. A demo that looks identical to the real thing while being fake is exactly what the
brief's section 12 warns about, so this one does not look identical.

Invoice facts come from live Stripe test mode when a read key is available, so the letters
contain real numbers and pass the same guardrails a model's letters must pass.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.llm import ScriptedLLM, turn  # noqa: E402
from app.agent.loop import build_run_context, run_agent  # noqa: E402
from app.config import settings  # noqa: E402
from app.store.repositories import RunStore  # noqa: E402
from shared.schema import ensure_schema  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"

SCRIPTED_NOTICE = (
    "This run was produced by scripts/dev_seed_run.py. No model was called: the tool calls, "
    "the reasoning and the letters below came from a fixed script, so the approval flow can "
    "be reviewed before an Anthropic key is available. A real run looks the same in shape "
    "and is driven by the model's own choices."
)

TONE_FOR_DAYS = ((14, "friendly"), (59, "firm"), (10**9, "final"))

OPENERS = {
    "friendly": "We think this one may simply have been overlooked.",
    "firm": "This balance is now well past its due date and we need a payment date.",
    "final": "This is a final notice before the account is escalated internally.",
}

CLOSERS = {
    "friendly": "Thank you -- and apologies if this has crossed with your payment.",
    "firm": "If the timing is difficult, reply and we will discuss terms.",
    "final": "Please treat this as a priority and reply with a payment date.",
}


def tone_for(days_overdue: int) -> str:
    for ceiling, tone in TONE_FOR_DAYS:
        if days_overdue <= ceiling:
            return tone
    return "final"


def pick_across_the_tone_ladder(rows: list[dict], *, limit: int = 3) -> list[dict]:
    """Choose invoices that span the tone ladder, largest first within each band.

    Selecting purely by amount is the obvious thing and it produced two `final` letters and
    one `firm` -- while section 11 step 3 of the brief is "three letters, three tones, three
    rationales". The tone ladder is the thing being demonstrated, so the fixture picks one
    invoice from each band before it takes a second from any.
    """
    by_amount = sorted(rows, key=lambda row: row["amount_due"], reverse=True)
    chosen: list[dict] = []
    for wanted in ("friendly", "firm", "final"):
        for row in by_amount:
            if row in chosen:
                continue
            if tone_for(row["days_overdue"] or 0) == wanted:
                chosen.append(row)
                break
    # Top up from whatever is left if a band was empty, so a thin fixture still demos.
    for row in by_amount:
        if len(chosen) >= limit:
            break
        if row not in chosen:
            chosen.append(row)
    chosen.sort(key=lambda row: row["amount_due"], reverse=True)
    return chosen[:limit]


def compose(facts: dict, tone: str) -> tuple[str, str]:
    """A letter containing every fact section 8 requires, and no figure we did not retrieve."""
    subject = (
        f"Invoice {facts['invoice_number']} is {facts['days_overdue']} days past due"
        if tone != "final"
        else (
            f"Final notice: invoice {facts['invoice_number']}, "
            f"{facts['days_overdue']} days past due"
        )
    )
    body = "\n".join(
        [
            f"Dear {facts['customer_name']},",
            "",
            f"Invoice {facts['invoice_number']} for {facts['amount_display']}, originally due "
            f"on {facts['due_date']}, is now {facts['days_overdue']} days past due.",
            "",
            OPENERS[tone],
            "",
            f"You can settle it here: {facts['hosted_invoice_url']}",
            "",
            "If this has already been paid, or you believe it is in error, reply to this "
            "email and we will look into it straight away.",
            "",
            CLOSERS[tone],
            "",
            "Kind regards,",
            "Servicia Collections",
        ]
    )
    return subject, body


def rationale_for(facts: dict, tone: str, history: dict | None) -> str:
    parts = [f"{facts['days_overdue']} days overdue at {facts['amount_display']}"]
    if history:
        summary = history.get("summary", {})
        if summary.get("paid_on_time") and summary["paid_on_time"] == summary.get("paid"):
            parts.append(
                f"every one of {summary['paid']} prior invoice(s) paid on time, so a "
                "courteous reminder rather than a firmer notice"
            )
        elif summary.get("still_unpaid", 0) > 1:
            parts.append(
                f"{summary['still_unpaid']} invoices still unpaid, so the firmer tone is "
                "warranted regardless of the day count"
            )
    parts.append(f"tone: {tone}")
    return "; ".join(parts) + "."


def main() -> int:
    config = settings()
    ensure_schema()

    run_id = RunStore.create(
        goal=(
            "[SCRIPTED FIXTURE - no model was called] Review the overdue invoices and draft "
            "a collection letter for each one worth pursuing."
        ),
        operator_id=config.operator_id,
        params={
            "min_days_overdue": 1,
            "max_proposals": config.max_proposals_per_run,
            "scripted": True,
        },
    )
    store, registry = build_run_context(run_id=run_id, settings=config)

    # Read real invoices through the real tool, so the letters carry real figures.
    listing = registry.execute(
        "list_overdue_invoices",
        {"min_days_overdue": 1, "limit": 25, "min_amount_cents": None},
    )
    deliverable = [row for row in listing.get("invoices", []) if row["deliverable"]]
    skipped = [row for row in listing.get("invoices", []) if not row["deliverable"]]
    candidates = pick_across_the_tone_ladder(deliverable, limit=3)

    if not candidates:
        print(f"{YELLOW}No deliverable overdue invoices found.{RESET}")
        print("Run scripts/seed_stripe_test_data.py --recreate first.")
        store.set_status("failed", error="no overdue invoices to work with")
        return 1

    turns = [
        turn(
            text=[SCRIPTED_NOTICE],
            tools=[
                (
                    "list_overdue_invoices",
                    {"min_days_overdue": 1, "limit": 25, "min_amount_cents": None},
                )
            ],
        )
    ]

    histories: dict[str, dict] = {}
    for row in candidates:
        facts = registry.facts_by_invoice[row["id"]]
        history = registry.execute(
            "get_payment_history", {"customer_id": facts["customer_id"], "limit": 10}
        )
        histories[row["id"]] = history
        tone = tone_for(facts["days_overdue"] or 0)
        subject, body = compose(facts, tone)
        turns.append(
            turn(
                thinking=[
                    f"{facts['customer_name']} is {facts['days_overdue']} days overdue for "
                    f"{facts['amount_display']}. Payment history: "
                    f"{history['summary']['paid_on_time']} of {history['summary']['paid']} "
                    f"prior invoices paid on time. A {tone} letter is appropriate."
                ],
                tools=[
                    ("get_payment_history", {"customer_id": facts["customer_id"], "limit": 10}),
                    (
                        "propose_collection_letter",
                        {
                            "invoice_id": row["id"],
                            "subject": subject,
                            "body": body,
                            "tone": tone,
                            "rationale": rationale_for(facts, tone, history),
                        },
                    ),
                ],
            )
        )

    closing = [
        f"I drafted {len(candidates)} letters: "
        + ", ".join(
            f"{registry.facts_by_invoice[row['id']]['customer_name']} "
            f"({tone_for(registry.facts_by_invoice[row['id']]['days_overdue'] or 0)})"
            for row in candidates
        )
        + "."
    ]
    if skipped:
        closing.append(
            f"I left {len(skipped)} overdue invoice(s) alone because there is no email "
            "address on file, so no letter could be delivered."
        )
    closing.append("Nothing has been sent. Each letter is waiting for a human decision.")
    turns.append(turn(text=["\n\n".join(closing)]))

    outcome = run_agent(
        run_id=run_id,
        goal="[SCRIPTED FIXTURE] Draft collection letters for the overdue invoices.",
        llm=ScriptedLLM(turns),
        registry=registry,
        store=store,
        settings=config,
    )

    print(f"{BOLD}Scripted run {run_id}{RESET}")
    print(f"  status     : {outcome.status}")
    print(f"  tool calls : {outcome.tool_calls}")
    print(f"  proposals  : {outcome.proposals}")
    if skipped:
        print(f"  skipped    : {len(skipped)} undeliverable")
    print(f"\n{GREEN}Open http://localhost:8000/proposals to review them.{RESET}")
    print(f"{DIM}No model was called. The run is badged as scripted in the UI.{RESET}\n")
    return 0 if outcome.proposals else 1


if __name__ == "__main__":
    raise SystemExit(main())
