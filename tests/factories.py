"""Test data builders.

Kept separate from the tests so the refusal suite reads as a list of scenarios rather
than a list of setup steps.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from shared.approval_token import ApprovalTokenClaims, mint
from shared.clock import now_utc, seconds_from_now
from shared.hashing import hash_payload
from shared.models import Approval, Proposal, Run

DEFAULT_BODY = (
    "Dear Acme Industries,\n\n"
    "Our records show invoice INV-1001 for $250.00, due on 2026-08-01, is now 9 days "
    "past due.\n\n"
    "You can settle it here: https://invoice.stripe.com/i/test_1001\n\n"
    "If you believe this is in error, reply to this email and we will look into it.\n\n"
    "Kind regards,\nServicia Collections"
)


def letter_payload(
    *,
    invoice_id: str = "in_1001",
    body: str = DEFAULT_BODY,
    subject: str = "Invoice INV-1001 is 9 days past due",
    tone: str = "friendly",
    customer_email: str = "ap@acme.test",
) -> dict[str, Any]:
    """The canonical approved content: the letter plus the facts it was built from."""
    return {
        "action_type": "send_collection_letter",
        "invoice_id": invoice_id,
        "invoice_number": "INV-1001",
        "customer_name": "Acme Industries",
        "customer_email": customer_email,
        "amount_display": "$250.00",
        "amount_due_minor": 25000,
        "currency": "usd",
        "due_date": "2026-08-01",
        "days_overdue": 9,
        "hosted_invoice_url": "https://invoice.stripe.com/i/test_1001",
        "subject": subject,
        "body": body,
        "tone": tone,
    }


def make_run(session: Session, *, goal: str = "Collect overdue invoices") -> Run:
    run = Run(
        goal=goal,
        status="awaiting_approval",
        operator_id="operator@servicia.ai",
        params={"min_days_overdue": 1, "max_proposals": 10},
    )
    session.add(run)
    session.flush()
    return run


def make_proposal(
    session: Session,
    *,
    run: Run | None = None,
    status: str = "pending",
    payload: dict[str, Any] | None = None,
    ttl_seconds: int = 3600,
) -> Proposal:
    run = run or make_run(session)
    payload = payload or letter_payload()
    proposal = Proposal(
        run_id=run.id,
        action_type="send_collection_letter",
        status=status,
        payload=payload,
        payload_hash=hash_payload(payload),
        rationale="9 days late with a clean payment history, so a courteous reminder.",
        stripe_invoice_id=payload["invoice_id"],
        customer_email=payload["customer_email"],
        amount_due=payload["amount_due_minor"],
        currency=payload["currency"],
        days_overdue=payload["days_overdue"],
        expires_at=seconds_from_now(ttl_seconds),
    )
    session.add(proposal)
    session.flush()
    return proposal


def record_approval(
    session: Session,
    proposal: Proposal,
    *,
    decision: str = "approve",
    actor: str = "operator@servicia.ai",
    edited_body: str | None = None,
    nonce: str | None = None,
) -> Approval:
    """Record a decision the way the public API would, without going through it.

    Phase 2 tests the gateway in isolation: the approval endpoint that normally writes
    this row arrives in Phase 5.
    """
    if edited_body is not None:
        payload = dict(proposal.payload)
        payload["body"] = edited_body
        proposal.payload = payload
        proposal.payload_hash = hash_payload(payload)

    approval = Approval(
        proposal_id=proposal.id,
        decision=decision,
        actor=actor,
        note=None,
        edited_body=edited_body,
        token_nonce=nonce or str(uuid.uuid4()),
    )
    proposal.status = "approved" if decision == "approve" else "rejected"
    session.add(approval)
    session.flush()
    return approval


def mint_token(
    proposal: Proposal,
    approval: Approval,
    secret: str,
    *,
    payload_hash: str | None = None,
    ttl_seconds: int = 900,
    approver: str | None = None,
    nonce: str | None = None,
    iat_offset: int = 0,
) -> str:
    claims = ApprovalTokenClaims(
        proposal_id=proposal.id,
        payload_hash=payload_hash or proposal.payload_hash,
        action_type=proposal.action_type,
        approver=approver or approval.actor,
        nonce=nonce or approval.token_nonce,
        iat=int(now_utc().timestamp()) + iat_offset,
        exp=int(now_utc().timestamp()) + iat_offset + ttl_seconds,
    )
    return mint(claims, secret)


def mint_claims(secret: str, **overrides: Any) -> str:
    """Mint a token from arbitrary claims, for cases with no matching database row."""
    now = int(now_utc().timestamp())
    values: dict[str, Any] = {
        "proposal_id": str(uuid.uuid4()),
        "payload_hash": "sha256:" + "0" * 64,
        "action_type": "send_collection_letter",
        "approver": "operator@servicia.ai",
        "nonce": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 900,
    }
    values.update(overrides)
    return mint(ApprovalTokenClaims(**values), secret)
