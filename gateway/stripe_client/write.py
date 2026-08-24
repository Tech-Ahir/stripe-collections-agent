"""The write-capable Stripe client.

Nothing under `app/` may import this module: contract 3 in `.importlinter`. It exists to
serve exactly one optional step of execution -- `stripe.Invoice.send_invoice` -- which is
off by default (`ENABLE_STRIPE_INVOICE_SEND=false`).

A `StripeClient` instance is constructed per call with an explicit key rather than setting
the module-global `stripe.api_key`. A global would be process-wide and invisible, and the
whole point of this file is that the write credential's reach is small and legible.
"""

from __future__ import annotations

from typing import Any


def send_invoice(api_key: str, invoice_id: str) -> dict[str, Any]:
    """Ask Stripe to email the invoice. Test mode only.

    Returns a small dict for the audit record rather than the whole Stripe object: the
    audit log should say what happened, not mirror the provider's schema.
    """
    if not api_key:
        return {
            "attempted": False,
            "reason": "STRIPE_API_KEY_WRITE is not configured",
        }

    import stripe

    client = stripe.StripeClient(api_key)
    invoice = client.v1.invoices.send_invoice(invoice_id)
    return {
        "attempted": True,
        "invoice_id": invoice.id,
        "status": invoice.status,
        "hosted_invoice_url": getattr(invoice, "hosted_invoice_url", None),
    }
