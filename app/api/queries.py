"""Read models for the public API and the UI.

One place that turns database rows into the shapes section 6 publishes, so the API and the
four screens cannot drift apart: the templates render these same dictionaries.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.repositories import expire_stale_proposals
from shared.audit import verify_chain
from shared.db import session_scope
from shared.models import (
    Approval,
    AuditEvent,
    Execution,
    OutboxMessage,
    Proposal,
    Run,
    RunStep,
)

# ----------------------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------------------


def _run_counts(session: Session, run_ids: list[str]) -> dict[str, dict[str, int]]:
    if not run_ids:
        return {}
    counts: dict[str, dict[str, int]] = {
        run_id: {"tool_calls": 0, "proposals": 0, "pending_proposals": 0} for run_id in run_ids
    }
    for run_id, total in session.execute(
        select(RunStep.run_id, func.count())
        .where(RunStep.run_id.in_(run_ids), RunStep.type == "tool_call")
        .group_by(RunStep.run_id)
    ):
        counts[run_id]["tool_calls"] = total
    for run_id, total in session.execute(
        select(Proposal.run_id, func.count())
        .where(Proposal.run_id.in_(run_ids))
        .group_by(Proposal.run_id)
    ):
        counts[run_id]["proposals"] = total
    for run_id, total in session.execute(
        select(Proposal.run_id, func.count())
        .where(Proposal.run_id.in_(run_ids), Proposal.status == "pending")
        .group_by(Proposal.run_id)
    ):
        counts[run_id]["pending_proposals"] = total
    return counts


def _run_dict(run: Run, counts: dict[str, int]) -> dict[str, Any]:
    return {
        "id": run.id,
        "goal": run.goal,
        "status": run.status,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "operator_id": run.operator_id,
        "error": run.error,
        "params": run.params or {},
        "tool_calls": counts.get("tool_calls", 0),
        "proposals": counts.get("proposals", 0),
        "pending_proposals": counts.get("pending_proposals", 0),
    }


def list_runs(*, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
    with session_scope() as session:
        runs = list(
            session.execute(
                select(Run).order_by(Run.started_at.desc()).limit(limit).offset(offset)
            ).scalars()
        )
        counts = _run_counts(session, [run.id for run in runs])
        return [_run_dict(run, counts.get(run.id, {})) for run in runs]


def step_dict(step: RunStep) -> dict[str, Any]:
    payload = step.payload or {}
    return {
        "seq": step.seq,
        "type": step.type,
        "tool_name": step.tool_name,
        "tool_class": payload.get("tool_class"),
        "payload": payload,
        "created_at": step.created_at,
    }


def get_run(run_id: str, *, after_seq: int = 0) -> dict[str, Any] | None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return None
        counts = _run_counts(session, [run_id]).get(run_id, {})
        steps = list(
            session.execute(
                select(RunStep)
                .where(RunStep.run_id == run_id, RunStep.seq > after_seq)
                .order_by(RunStep.seq)
            ).scalars()
        )
        proposal_ids = list(
            session.execute(
                select(Proposal.id)
                .where(Proposal.run_id == run_id)
                .order_by(Proposal.amount_due.desc())
            ).scalars()
        )
        detail = _run_dict(run, counts)
        detail["transcript"] = [step_dict(step) for step in steps]
        detail["proposal_ids"] = proposal_ids
        return detail


def steps_after(run_id: str, after_seq: int) -> list[dict[str, Any]]:
    """The tail of a transcript. This is what the SSE endpoint polls."""
    with session_scope() as session:
        return [
            step_dict(step)
            for step in session.execute(
                select(RunStep)
                .where(RunStep.run_id == run_id, RunStep.seq > after_seq)
                .order_by(RunStep.seq)
            ).scalars()
        ]


def run_status(run_id: str) -> str | None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        return run.status if run else None


# ----------------------------------------------------------------------------------
# Proposals
# ----------------------------------------------------------------------------------


def _proposal_summary(proposal: Proposal) -> dict[str, Any]:
    payload = proposal.payload or {}
    return {
        "id": proposal.id,
        "run_id": proposal.run_id,
        "action_type": proposal.action_type,
        "status": proposal.status,
        "customer_name": payload.get("customer_name"),
        "customer_email": proposal.customer_email,
        "invoice_number": payload.get("invoice_number"),
        "stripe_invoice_id": proposal.stripe_invoice_id,
        "amount_due": proposal.amount_due,
        "amount_display": payload.get("amount_display") or str(proposal.amount_due),
        "currency": proposal.currency,
        "days_overdue": proposal.days_overdue,
        "tone": payload.get("tone"),
        "rationale": proposal.rationale,
        "subject": payload.get("subject"),
        "created_at": proposal.created_at,
        "expires_at": proposal.expires_at,
    }


def list_proposals(
    *, status: str | None = "pending", run_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Section 9: "Sorted by amount descending." Largest exposure first."""
    with session_scope() as session:
        expire_stale_proposals(session)
        query = select(Proposal)
        if status:
            query = query.where(Proposal.status == status)
        if run_id:
            query = query.where(Proposal.run_id == run_id)
        rows = list(
            session.execute(query.order_by(Proposal.amount_due.desc()).limit(limit)).scalars()
        )
        return [_proposal_summary(row) for row in rows]


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        expire_stale_proposals(session)
        proposal = session.get(Proposal, proposal_id)
        if proposal is None:
            return None
        payload = proposal.payload or {}
        detail = _proposal_summary(proposal)
        detail.update(
            {
                "body": payload.get("body", ""),
                "payload": payload,
                "payload_hash": proposal.payload_hash,
                # The facts the letter was built from, so the operator sees both (section 8).
                "invoice_facts": {
                    key: payload.get(key)
                    for key in (
                        "invoice_id",
                        "invoice_number",
                        "customer_name",
                        "customer_email",
                        "amount_display",
                        "amount_due",
                        "currency",
                        "due_date",
                        "days_overdue",
                        "hosted_invoice_url",
                    )
                },
                "approvals": [
                    {
                        "id": row.id,
                        "decision": row.decision,
                        "actor": row.actor,
                        "note": row.note,
                        "edited": row.edited_body is not None,
                        "decided_at": row.decided_at,
                    }
                    for row in session.execute(
                        select(Approval)
                        .where(Approval.proposal_id == proposal_id)
                        .order_by(Approval.id)
                    ).scalars()
                ],
                "executions": [
                    {
                        "id": row.id,
                        "status": row.status,
                        "idempotency_key": row.idempotency_key,
                        "result": row.result,
                        "error": row.error,
                        "executed_at": row.executed_at,
                    }
                    for row in session.execute(
                        select(Execution)
                        .where(Execution.proposal_id == proposal_id)
                        .order_by(Execution.id)
                    ).scalars()
                ],
                "payment_history": [],
            }
        )
        return detail


def proposal_counters() -> dict[str, int]:
    with session_scope() as session:
        expire_stale_proposals(session)
        by_status = dict(
            session.execute(select(Proposal.status, func.count()).group_by(Proposal.status)).all()
        )
        return {
            "pending": by_status.get("pending", 0),
            "approved": by_status.get("approved", 0),
            "rejected": by_status.get("rejected", 0),
            "executed": by_status.get("executed", 0),
            "failed": by_status.get("failed", 0),
            "expired": by_status.get("expired", 0),
            "sent": session.execute(select(func.count()).select_from(OutboxMessage)).scalar_one(),
        }


# ----------------------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------------------


def audit_page(
    *,
    limit: int = 50,
    offset: int = 0,
    actor: str | None = None,
    event: str | None = None,
) -> dict[str, Any]:
    """Newest first, with the chain's verification status alongside (section 6)."""
    with session_scope() as session:
        query = select(AuditEvent)
        counter = select(func.count()).select_from(AuditEvent)
        if actor:
            query = query.where(AuditEvent.actor == actor)
            counter = counter.where(AuditEvent.actor == actor)
        if event:
            query = query.where(AuditEvent.event == event)
            counter = counter.where(AuditEvent.event == event)

        rows = list(
            session.execute(
                query.order_by(AuditEvent.id.desc()).limit(limit).offset(offset)
            ).scalars()
        )
        status = verify_chain(session)
        return {
            "events": [
                {
                    "id": row.id,
                    "ts": row.ts,
                    "actor": row.actor,
                    "event": row.event,
                    "subject_type": row.subject_type,
                    "subject_id": row.subject_id,
                    "detail": row.detail or {},
                    "prev_hash": row.prev_hash,
                    "hash": row.hash,
                }
                for row in rows
            ],
            "total": session.execute(counter).scalar_one(),
            "limit": limit,
            "offset": offset,
            "chain": status.as_dict(),
        }


def audit_filters() -> dict[str, list[str]]:
    with session_scope() as session:
        return {
            "actors": sorted(session.execute(select(AuditEvent.actor).distinct()).scalars().all()),
            "events": sorted(session.execute(select(AuditEvent.event).distinct()).scalars().all()),
        }


def outbox(limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as session:
        return [
            {
                "id": row.id,
                "proposal_id": row.proposal_id,
                "to_email": row.to_email,
                "subject": row.subject,
                "body": row.body,
                "adapter": row.adapter,
                "file_path": row.file_path,
                "created_at": row.created_at,
            }
            for row in session.execute(
                select(OutboxMessage).order_by(OutboxMessage.created_at.desc()).limit(limit)
            ).scalars()
        ]
