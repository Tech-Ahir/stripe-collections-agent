"""The DRAFT tool (brief section 4).

    propose_collection_letter(invoice_id, subject, body, tone, rationale) -> ProposalRef
        Persists a proposal in status=pending. Returns proposal_id.
        NO EXTERNAL EFFECT. This is the agent's terminal capability.

One tool, and it is where the agent's reach ends. It writes a row. Nothing leaves the
system, nothing is scheduled, and no credential capable of sending anything is reachable
from here -- ``.importlinter`` contract 6 forbids ``app.agent`` from importing even the
token minter or the gateway client.

Four things are refused here rather than merely discouraged in the prompt, because section 4
sets that precedent itself ("a duplicate is rejected by the store, not by the prompt"):

* an invoice the agent has not actually read in this run;
* an invoice with no deliverable email address;
* a letter that breaks section 8's compliance rules or invents a figure;
* a second pending proposal for the same invoice, or one past the run's cap.

Every refusal comes back as a correctable error, so the agent can fix the letter and call
again inside the same run.
"""

from __future__ import annotations

from typing import Any

from app.agent.guardrails import review_letter
from app.agent.tools.base import ToolClass, ToolFailure, ToolSpec, schema
from app.store.repositories import DuplicatePendingProposal, RunStore
from shared.models import TONES


def build_draft_tool(
    *,
    store: RunStore,
    facts_by_invoice: dict[str, dict[str, Any]],
    max_proposals: int,
    ttl_hours: int,
) -> ToolSpec:
    """The single DRAFT tool.

    ``facts_by_invoice`` is populated by the READ tools as the agent uses them. It is what
    makes "facts are injected by the tool layer, never recalled by the model" true: the
    letter's figures are taken from this dict, not from anything the model wrote.
    """

    def propose_collection_letter(
        invoice_id: str,
        subject: str,
        body: str,
        tone: str,
        rationale: str,
    ) -> dict[str, Any]:
        facts = facts_by_invoice.get(invoice_id)
        if facts is None:
            raise ToolFailure(
                {
                    "error": "invoice_not_read",
                    "message": (
                        f"You have not read invoice {invoice_id} in this run, so its facts "
                        "are not available and no letter can be built from them. Call "
                        "list_overdue_invoices or get_invoice first."
                    ),
                    "recoverable": True,
                }
            )

        if not facts.get("deliverable"):
            raise ToolFailure(
                {
                    "error": "not_deliverable",
                    "message": facts.get(
                        "not_deliverable_reason",
                        "This invoice has no email address on file, so no letter can be "
                        "sent. Do not propose one.",
                    ),
                    "invoice_id": invoice_id,
                    "recoverable": True,
                }
            )

        if tone not in TONES:
            raise ToolFailure(
                {
                    "error": "unknown_tone",
                    "message": f"tone must be one of {list(TONES)}, not {tone!r}.",
                    "recoverable": True,
                }
            )

        if not (rationale or "").strip():
            raise ToolFailure(
                {
                    "error": "rationale_required",
                    "message": (
                        "Every proposal needs a written rationale explaining why this "
                        "invoice and why this tone. The operator reads it first, and it is "
                        "what makes the queue reviewable at a glance."
                    ),
                    "recoverable": True,
                }
            )

        if store.proposal_count() >= max_proposals:
            raise ToolFailure(
                {
                    "error": "proposal_cap_reached",
                    "message": (
                        f"This run's limit of {max_proposals} proposals has been reached. "
                        "Summarise what you have done and stop proposing."
                    ),
                    "recoverable": True,
                }
            )

        report = review_letter(subject=subject, body=body, facts=facts)
        if not report.ok:
            raise ToolFailure(report.as_tool_error())

        # The stored payload is the approved content: the letter plus the facts it was
        # built from, so the operator can see both and the gateway can re-hash exactly it.
        payload = {
            "action_type": "send_collection_letter",
            "invoice_id": invoice_id,
            "invoice_number": facts.get("invoice_number") or facts.get("number"),
            # Carried so the approval screen can show the payment history the agent read
            # for this customer, which section 9 puts between the facts and the letter.
            "customer_id": facts.get("customer_id"),
            "customer_name": facts.get("customer_name"),
            "customer_email": facts.get("customer_email"),
            "amount_due": facts.get("amount_due"),
            "amount_display": facts.get("amount_display"),
            "currency": facts.get("currency"),
            "due_date": facts.get("due_date"),
            "days_overdue": facts.get("days_overdue"),
            "hosted_invoice_url": facts.get("hosted_invoice_url"),
            "subject": subject,
            "body": body,
            "tone": tone,
        }

        try:
            proposal_id = store.create_proposal(
                payload=payload,
                rationale=rationale,
                invoice_id=invoice_id,
                customer_email=str(facts.get("customer_email")),
                amount_due=int(facts.get("amount_due") or 0),
                currency=str(facts.get("currency") or "usd"),
                days_overdue=int(facts.get("days_overdue") or 0),
                ttl_hours=ttl_hours,
            )
        except DuplicatePendingProposal as exc:
            raise ToolFailure(
                {
                    "error": "duplicate_pending_proposal",
                    "message": str(exc),
                    "invoice_id": invoice_id,
                    "recoverable": True,
                }
            ) from exc

        return {
            "proposal_id": proposal_id,
            "status": "pending",
            "tone": tone,
            "invoice_id": invoice_id,
            "amount_display": facts.get("amount_display"),
            "awaiting": "human approval",
            "note": (
                "Nothing has been sent. This letter is queued for a human to review, and "
                "only an approval can cause it to leave the system."
            ),
        }

    return ToolSpec(
        name="propose_collection_letter",
        tool_class=ToolClass.DRAFT,
        description=(
            "Draft a collection letter for one invoice and place it in the approval queue. "
            "This is the ONLY thing you can do with an invoice, and it does not send "
            "anything: it writes an internal record that a human will review and approve "
            "or reject. Use only figures returned by the read tools -- the amount, due "
            "date, invoice number and payment link are checked against what was actually "
            "retrieved, and a letter containing anything else is rejected."
        ),
        input_schema=schema(
            {
                "invoice_id": {
                    "type": "string",
                    "description": "The Stripe invoice id this letter is about.",
                },
                "subject": {
                    "type": "string",
                    "description": "The email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The letter itself. It must contain the customer name, the invoice "
                        "number, the amount due exactly as amount_display gave it, the "
                        "original due date, how many days overdue it is, the hosted "
                        "payment link, and a way to raise a dispute."
                    ),
                },
                "tone": {
                    "type": "string",
                    "enum": list(TONES),
                    "description": (
                        "friendly for 1-14 days overdue, firm for 15-59, final for 60+, "
                        "adjusted for what the payment history shows."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "Why this invoice and why this tone. Shown to the operator first; "
                        "it is what makes the queue reviewable at a glance."
                    ),
                },
            }
        ),
        handler=propose_collection_letter,
    )
