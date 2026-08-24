"""The READ tools (brief section 4).

Four tools, no external effect, auto-executing. The agent decides which of them to call and
in what order -- the sequence is emphatically not hardcoded (section 1: "do not hardcode the
sequence 'list invoices, then draft letter for each'. Let the model choose. It will
sometimes pull payment history first, sometimes skip it. That variability is the proof.").

Every figure these return has already been formatted by the tool layer. The model is never
handed an amount it has to divide or a date it has to subtract.
"""

from __future__ import annotations

from typing import Any

from app.agent.tools.base import ToolClass, ToolFailure, ToolSpec, nullable, schema
from app.stripe_client.read import StripeReadClient, StripeReadError


def _guard(operation):
    """Turn a Stripe failure into something the agent can read and continue past."""

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except StripeReadError as exc:
            raise ToolFailure(exc.as_tool_error()) from exc

    return wrapped


def build_read_tools(reader: StripeReadClient) -> list[ToolSpec]:
    """The four READ tools, bound to one run's Stripe client (and its customer cache)."""

    def list_overdue_invoices(
        min_days_overdue: int | None = 1,
        limit: int | None = 25,
        min_amount_cents: int | None = None,
    ) -> dict[str, Any]:
        invoices = reader.list_overdue_invoices(
            min_days_overdue=1 if min_days_overdue is None else max(0, int(min_days_overdue)),
            limit=25 if limit is None else max(1, min(int(limit), 100)),
            min_amount_cents=None if min_amount_cents is None else int(min_amount_cents),
        )
        undeliverable = [row["id"] for row in invoices if not row["deliverable"]]
        result: dict[str, Any] = {"count": len(invoices), "invoices": invoices}
        if undeliverable:
            result["note"] = (
                f"{len(undeliverable)} of these have no email address on file and cannot "
                "receive a letter. Do not propose one for them."
            )
        return result

    def get_invoice(invoice_id: str) -> dict[str, Any]:
        return reader.get_invoice(invoice_id)

    def get_customer(customer_id: str) -> dict[str, Any]:
        return reader.get_customer(customer_id)

    def get_payment_history(customer_id: str, limit: int | None = 10) -> dict[str, Any]:
        history = reader.get_payment_history(
            customer_id, limit=10 if limit is None else max(1, min(int(limit), 100))
        )
        paid = [entry for entry in history if entry["status"] == "paid"]
        on_time = [entry for entry in paid if (entry["days_late_when_paid"] or 0) == 0]
        summary = {
            "prior_invoices": len(history),
            "paid": len(paid),
            "paid_on_time": len(on_time),
            "still_unpaid": len([e for e in history if e["status"] == "open"]),
        }
        return {"summary": summary, "history": history}

    return [
        ToolSpec(
            name="list_overdue_invoices",
            tool_class=ToolClass.READ,
            description=(
                "List invoices that are finalized, still owed, and past their due date. "
                "Returns each invoice with its customer, the amount owed both as an "
                "integer in minor units and as a preformatted display string, the due "
                "date, how many days overdue it is, the hosted payment link, and whether "
                "a letter can actually be delivered. Sorted largest amount first. "
                "Pass null for any argument to use its default."
            ),
            input_schema=schema(
                {
                    "min_days_overdue": {
                        "type": nullable("integer"),
                        "description": "Ignore invoices less overdue than this. Default 1.",
                    },
                    "limit": {
                        "type": nullable("integer"),
                        "description": "Maximum invoices to return. Default 25, cap 100.",
                    },
                    "min_amount_cents": {
                        "type": nullable("integer"),
                        "description": (
                            "Ignore invoices owing less than this, in MINOR units "
                            "(2500 means $25.00). Null means no minimum."
                        ),
                    },
                }
            ),
            handler=_guard(list_overdue_invoices),
        ),
        ToolSpec(
            name="get_invoice",
            tool_class=ToolClass.READ,
            description=(
                "Full detail for one invoice, including its line items and how many "
                "payment attempts have been made. Use this when you need to know what the "
                "invoice was actually for."
            ),
            input_schema=schema(
                {
                    "invoice_id": {
                        "type": "string",
                        "description": "The Stripe invoice id, e.g. in_1A2b3C.",
                    }
                }
            ),
            handler=_guard(get_invoice),
        ),
        ToolSpec(
            name="get_customer",
            tool_class=ToolClass.READ,
            description=(
                "One customer's record: name, email address, when they were created, "
                "whether Stripe considers them delinquent, and their metadata."
            ),
            input_schema=schema(
                {
                    "customer_id": {
                        "type": "string",
                        "description": "The Stripe customer id, e.g. cus_1A2b3C.",
                    }
                }
            ),
            handler=_guard(get_customer),
        ),
        ToolSpec(
            name="get_payment_history",
            tool_class=ToolClass.READ,
            description=(
                "This customer's prior invoices and how each one turned out, newest first, "
                "with a summary of how many were paid and how many were paid on time. Use "
                "it to tell a first-time late payer from a repeat one: a reliable customer "
                "who is nine days late should not be addressed like a habitual non-payer."
            ),
            input_schema=schema(
                {
                    "customer_id": {
                        "type": "string",
                        "description": "The Stripe customer id.",
                    },
                    "limit": {
                        "type": nullable("integer"),
                        "description": "How many prior invoices to return. Default 10.",
                    },
                }
            ),
            handler=_guard(get_payment_history),
        ),
    ]
