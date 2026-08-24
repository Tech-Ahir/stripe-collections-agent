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


def settle_run_if_decided(session: Session, run_id: str) -> bool:
    """Move a run out of `awaiting_approval` once nothing of its is still pending.

    The loop's last act is to set `awaiting_approval`, and until this existed nothing ever
    moved it again -- so a run whose every proposal had been approved and executed still
    showed "awaiting approval" on the dashboard forever. The status was describing the
    moment the agent stopped, not the state of the work.

    `completed` is the right terminal state: the run did its job and the queue it produced
    has been dealt with. It is deliberately not `executed` -- a run whose proposals were all
    *rejected* is equally settled, and the run itself never executes anything.

    Idempotent, and safe to call from a read path.
    """
    run = session.get(Run, run_id)
    if run is None or run.status != "awaiting_approval":
        return False

    still_pending = session.execute(
        select(func.count())
        .select_from(Proposal)
        .where(Proposal.run_id == run_id, Proposal.status == "pending")
    ).scalar_one()
    if still_pending:
        return False

    outcomes = dict(
        session.execute(
            select(Proposal.status, func.count())
            .where(Proposal.run_id == run_id)
            .group_by(Proposal.status)
        ).all()
    )
    run.status = "completed"
    run.ended_at = run.ended_at or now_utc()
    audit.append(
        session,
        actor="system",
        event=audit.RUN_SETTLED,
        subject_type="run",
        subject_id=run_id,
        detail={"outcomes": outcomes, "reason": "every proposal from this run has been decided"},
    )
    log.info("run %s settled: %s", run_id, outcomes)
    return True


def settle_decided_runs(session: Session | None = None) -> int:
    """Settle every run that is waiting on a queue with nothing left in it.

    Called from the read paths as well as after each decision, so runs that went stale
    before this existed heal the first time anyone looks at them.
    """

    def work(active: Session) -> int:
        # A proposal past its TTL is decided, so expiry has to happen first or a run whose
        # last pending letter merely lapsed would keep saying it was waiting on it.
        expire_stale_proposals(active)
        waiting = list(
            active.execute(select(Run.id).where(Run.status == "awaiting_approval")).scalars()
        )
        return sum(1 for run_id in waiting if settle_run_if_decided(active, run_id))

    if session is not None:
        return work(session)
    with session_scope() as owned:
        return work(owned)


def abandon_orphaned_runs(
    session: Session | None = None, *, reason: str | None = None
) -> list[str]:
    """Fail any run whose worker no longer exists.

    A run's status lives in the database; the thread executing it lives in a process. A
    restart, a crash or a rebuild therefore leaves `queued` and `running` rows that nothing
    will ever finish -- the dashboard showed one of each, stuck for hours.

    Called at startup, where the reasoning is exact: this process has just begun, so it owns
    no in-flight runs, so anything still marked queued or running was abandoned by whatever
    process died. That reasoning holds only for a single-instance deployment, which is what
    docker-compose.yml describes; running two app replicas against one database would need
    a worker heartbeat instead.
    """
    message = reason or (
        "The service restarted while this run was in progress, so no worker is executing "
        "it any more. Start a new run."
    )

    def work(active: Session) -> list[str]:
        orphaned = list(
            active.execute(select(Run).where(Run.status.in_(("queued", "running")))).scalars()
        )
        for run in orphaned:
            run.status = "failed"
            run.error = message
            run.ended_at = run.ended_at or now_utc()
            audit.append(
                active,
                actor="system",
                event=audit.RUN_ABANDONED,
                subject_type="run",
                subject_id=run.id,
                detail={"previous_status": "queued/running", "reason": message},
            )
        if orphaned:
            log.warning("marked %d abandoned run(s) as failed at startup", len(orphaned))
        return [run.id for run in orphaned]

    if session is not None:
        return work(session)
    with session_scope() as owned:
        return work(owned)


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
