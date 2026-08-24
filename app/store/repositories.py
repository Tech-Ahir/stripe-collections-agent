"""Persistence for runs, transcripts and proposals.

The transcript is a deliverable, not a debug view (section 1), so every step the agent
takes is written here with a monotonic sequence number as it happens -- not buffered and
flushed at the end. That is what lets the SSE endpoint tail the database and what leaves a
usable partial transcript when a run fails mid-way.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared import audit
from shared.clock import hours_from_now, now_utc
from shared.db import session_scope
from shared.hashing import hash_payload
from shared.models import Proposal, Run, RunStep

log = logging.getLogger("app.store")


class DuplicatePendingProposal(RuntimeError):
    """One pending proposal per invoice -- rejected by the store, not by the prompt."""

    def __init__(self, invoice_id: str):
        super().__init__(
            f"A pending proposal already exists for invoice {invoice_id}. "
            "Do not propose a second letter for the same invoice in this run."
        )
        self.invoice_id = invoice_id


class RunStore:
    """Everything the loop needs to record what it did."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    # -- lifecycle -----------------------------------------------------------------

    @staticmethod
    def create(
        *,
        goal: str,
        operator_id: str,
        params: dict[str, Any],
    ) -> str:
        with session_scope() as session:
            run = Run(
                goal=goal,
                status="queued",
                operator_id=operator_id,
                params=params,
                started_at=now_utc(),
            )
            session.add(run)
            session.flush()
            audit.append(
                session,
                actor="operator",
                event=audit.RUN_STARTED,
                subject_type="run",
                subject_id=run.id,
                detail={"goal": goal, "params": params},
            )
            return run.id

    def set_status(self, status: str, *, error: str | None = None) -> None:
        with session_scope() as session:
            run = session.get(Run, self.run_id)
            if run is None:
                raise LookupError(self.run_id)
            run.status = status
            if error is not None:
                run.error = error
            if status in ("completed", "failed", "awaiting_approval"):
                run.ended_at = now_utc()
                audit.append(
                    session,
                    actor="agent",
                    event=audit.RUN_FAILED if status == "failed" else audit.RUN_COMPLETED,
                    subject_type="run",
                    subject_id=self.run_id,
                    detail={"status": status, "error": error},
                )

    # -- the transcript ------------------------------------------------------------

    def append_step(
        self,
        *,
        type: str,
        payload: dict[str, Any],
        tool_name: str | None = None,
    ) -> int:
        """Append one transcript entry and return its sequence number."""
        with session_scope() as session:
            next_seq = (
                session.execute(
                    select(func.coalesce(func.max(RunStep.seq), 0)).where(
                        RunStep.run_id == self.run_id
                    )
                ).scalar_one()
                + 1
            )
            session.add(
                RunStep(
                    run_id=self.run_id,
                    seq=next_seq,
                    type=type,
                    tool_name=tool_name,
                    payload=payload,
                    created_at=now_utc(),
                )
            )
            return next_seq

    def tool_call_count(self) -> int:
        with session_scope() as session:
            return session.execute(
                select(func.count())
                .select_from(RunStep)
                .where(RunStep.run_id == self.run_id, RunStep.type == "tool_call")
            ).scalar_one()

    def proposal_count(self) -> int:
        with session_scope() as session:
            return session.execute(
                select(func.count()).select_from(Proposal).where(Proposal.run_id == self.run_id)
            ).scalar_one()

    # -- proposals -----------------------------------------------------------------

    def create_proposal(
        self,
        *,
        payload: dict[str, Any],
        rationale: str,
        invoice_id: str,
        customer_email: str,
        amount_due: int,
        currency: str,
        days_overdue: int,
        ttl_hours: int,
    ) -> str:
        """Persist a proposal in status=pending. NO EXTERNAL EFFECT.

        The uniqueness of a pending proposal per invoice is enforced by a partial unique
        index, so a duplicate is refused by the database even if two runs race.
        """
        with session_scope() as session:
            proposal = Proposal(
                run_id=self.run_id,
                action_type="send_collection_letter",
                status="pending",
                payload=payload,
                payload_hash=hash_payload(payload),
                rationale=rationale,
                stripe_invoice_id=invoice_id,
                customer_email=customer_email,
                amount_due=amount_due,
                currency=currency,
                days_overdue=days_overdue,
                created_at=now_utc(),
                expires_at=hours_from_now(ttl_hours),
            )
            session.add(proposal)
            try:
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                # SQLite names the COLUMN in a uniqueness error, not the index -- it says
                # "UNIQUE constraint failed: proposals.stripe_invoice_id". Postgres names
                # the index. Match either, or a real duplicate escapes as a raw
                # IntegrityError and takes the run down instead of becoming a correctable
                # tool error.
                message = str(getattr(exc, "orig", exc))
                if (
                    "ux_proposals_one_pending_per_invoice" in message
                    or "proposals.stripe_invoice_id" in message
                ):
                    raise DuplicatePendingProposal(invoice_id) from exc
                raise
            audit.append(
                session,
                actor="agent",
                event=audit.PROPOSAL_CREATED,
                subject_type="proposal",
                subject_id=proposal.id,
                detail={
                    "run_id": self.run_id,
                    "invoice_id": invoice_id,
                    "tone": payload.get("tone"),
                    "amount_display": payload.get("amount_display"),
                    "days_overdue": days_overdue,
                    "payload_hash": proposal.payload_hash,
                },
            )
            return proposal.id


def expire_stale_proposals(session: Session | None = None) -> int:
    """Move pending proposals past their TTL to `expired`.

    Section 4: "Proposals expire after 72 hours. Stale approvals are worse than no
    approvals." Applied lazily on read as well as by this sweep, so a proposal cannot be
    approved after its deadline merely because no scheduler ran.
    """

    def work(active: Session) -> int:
        stale = list(
            active.execute(
                select(Proposal).where(
                    Proposal.status == "pending", Proposal.expires_at <= now_utc()
                )
            ).scalars()
        )
        for proposal in stale:
            proposal.status = "expired"
            audit.append(
                active,
                actor="system",
                event=audit.PROPOSAL_EXPIRED,
                subject_type="proposal",
                subject_id=proposal.id,
                detail={"expires_at": str(proposal.expires_at)},
            )
        return len(stale)

    if session is not None:
        return work(session)
    with session_scope() as owned:
        return work(owned)
