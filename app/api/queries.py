"""Read models for the public API and the UI.

One place that turns database rows into the shapes section 6 publishes, so the API and the
four screens cannot drift apart: the templates render these same dictionaries.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.repositories import (
    expire_stale_proposals,
    settle_decided_runs,
    settle_run_if_decided,
)
from shared.audit import verify_chain
from shared.clock import start_of_utc_day
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

log = logging.getLogger("app.api.queries")


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
        # A run waiting on a queue with nothing left in it is not waiting on anything.
        # Settling here means rows that went stale before this existed heal on first read.
        settle_decided_runs(session)
        runs = list(
            session.execute(
                select(Run).order_by(Run.started_at.desc()).limit(limit).offset(offset)
            ).scalars()
        )
        counts = _run_counts(session, [run.id for run in runs])
        return [_run_dict(run, counts.get(run.id, {})) for run in runs]


def transcript_index(session, run_id: str) -> dict[str, dict[str, Any]]:
    """Who and what the ids in a transcript refer to, taken from the run's own results.

    A tool call carries ids, not names: `get_payment_history` is called with
    `cus_V88U8Ng76gSGrl`, and seven of those in a row are indistinguishable on screen. The
    names are already in the transcript -- `list_overdue_invoices` returned them -- so this
    walks the run's own results and builds the lookup rather than re-reading Stripe.

    Nothing here reaches outside the database, and a missing name simply means the screen
    keeps showing the id.
    """
    index: dict[str, dict[str, Any]] = {"customers": {}, "invoices": {}}
    steps = session.execute(
        select(RunStep).where(RunStep.run_id == run_id, RunStep.type == "tool_result")
    ).scalars()
    for step in steps:
        result = (step.payload or {}).get("result")
        if not isinstance(result, dict):
            continue
        rows = result.get("invoices") or result.get("history") or []
        if isinstance(result.get("invoice"), dict):
            rows = [*rows, result["invoice"]]
        for row in rows:
            if not isinstance(row, dict):
                continue
            invoice_id = row.get("id") or row.get("invoice_id")
            if invoice_id:
                # Merge, never replace. The same invoice appears in several results and they
                # carry different fields: `list_overdue_invoices` knows the customer's name,
                # `get_payment_history` does not. Assigning the later row wholesale erased
                # names the run had already established.
                known = index["invoices"].setdefault(invoice_id, {})
                for key, value in (
                    ("number", row.get("number")),
                    ("customer", row.get("customer_name")),
                    ("amount", row.get("amount_due_display")),
                ):
                    if value and not known.get(key):
                        known[key] = value
            if row.get("customer_id") and row.get("customer_name"):
                index["customers"][row["customer_id"]] = row["customer_name"]
    return index


def _step_about(payload: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One line saying what this call was about, in names rather than ids."""
    arguments = payload.get("arguments") or {}
    if not isinstance(arguments, dict):
        return {}
    about: dict[str, Any] = {}

    customer_id = arguments.get("customer_id")
    if customer_id:
        about["customer"] = index["customers"].get(customer_id) or customer_id

    invoice_id = arguments.get("invoice_id")
    if invoice_id:
        known = index["invoices"].get(invoice_id, {})
        about["customer"] = known.get("customer") or about.get("customer")
        about["invoice"] = known.get("number") or invoice_id
        about["amount"] = known.get("amount")

    if arguments.get("tone"):
        about["tone"] = arguments["tone"]
    return {key: value for key, value in about.items() if value}


def step_dict(step: RunStep, index: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = step.payload or {}
    return {
        "seq": step.seq,
        "type": step.type,
        "tool_name": step.tool_name,
        "tool_class": payload.get("tool_class"),
        "payload": payload,
        # Presentation only: names for the ids in `payload`, resolved from this run's own
        # results. Absent when nothing in the transcript identified them.
        "about": _step_about(payload, index) if index and step.type == "tool_call" else {},
        "created_at": step.created_at,
    }


def get_run(run_id: str, *, after_seq: int = 0) -> dict[str, Any] | None:
    with session_scope() as session:
        expire_stale_proposals(session)
        settle_run_if_decided(session, run_id)
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
        index = transcript_index(session, run_id)
        detail = _run_dict(run, counts)
        detail["transcript"] = [step_dict(step, index) for step in steps]
        detail["proposal_ids"] = proposal_ids
        return detail


def steps_after(run_id: str, after_seq: int) -> list[dict[str, Any]]:
    """The tail of a transcript. This is what the SSE endpoint polls."""
    with session_scope() as session:
        index = transcript_index(session, run_id)
        return [
            step_dict(step, index)
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
                "payment_history": payment_history_seen_by_the_agent(
                    session, run_id=proposal.run_id, customer_id=payload.get("customer_id")
                ),
            }
        )
        return detail


def payment_history_seen_by_the_agent(
    session: Session, *, run_id: str, customer_id: str | None
) -> list[dict[str, Any]]:
    """The payment history the agent actually read, taken from the run transcript.

    Section 9 puts payment history between the invoice facts and the letter, so the operator
    can see the basis for the tone the agent chose. It is read back out of the transcript
    rather than re-fetched from Stripe for a reason: what matters to a reviewer is what the
    agent *saw* when it decided, not what Stripe says a moment later.

    Returns an empty list when the agent did not look -- which is itself information, and the
    UI says so rather than showing a blank panel.
    """
    if not customer_id:
        return []

    steps = list(
        session.execute(
            select(RunStep)
            .where(RunStep.run_id == run_id, RunStep.tool_name == "get_payment_history")
            .order_by(RunStep.seq)
        ).scalars()
    )

    # Paired by ADJACENCY, not by tool_use_id.
    #
    # The loop persists a call and then its result, so in sequence order every tool_result is
    # preceded by the tool_call it answers. An earlier version keyed a lookup on tool_use_id
    # instead, which looked obviously right and was wrong: a run asking about three customers
    # produced three calls all carrying `toolu_0`, the map collapsed them to the last one, and
    # every proposal but one showed an empty history panel. Ids are only guaranteed unique
    # within one assistant turn, so the ordering the loop already maintains is the stronger
    # signal. The id is still checked when both sides carry one.
    found: list[dict[str, Any]] = []
    asked_about: str | None = None
    asked_id: str | None = None
    for step in steps:
        payload = step.payload or {}
        if step.type == "tool_call":
            asked_about = (payload.get("arguments") or {}).get("customer_id")
            asked_id = payload.get("tool_use_id")
            continue
        if step.type != "tool_result" or payload.get("is_error"):
            asked_about = asked_id = None
            continue
        result_id = payload.get("tool_use_id")
        if asked_id and result_id and asked_id != result_id:
            asked_about = asked_id = None
            continue
        if asked_about == customer_id:
            result = payload.get("result") or {}
            history = result.get("history") if isinstance(result, dict) else None
            if isinstance(history, list):
                found = history  # keep the most recent look at this customer
        asked_about = asked_id = None
    return found


def proposal_counters() -> dict[str, int]:
    """Section 9's counters: pending proposals, approved today, sent today.

    "Today" is the current UTC day. A single operator identity has no timezone of its own and
    the audit log is UTC throughout, so any other day boundary would make the counters
    disagree with the log.

    "Sent today" counts **succeeded executions**, not outbox rows. The outbox is one of three
    adapters: with ``EMAIL_ADAPTER=smtp`` or ``resend`` there is no outbox row to count, and
    the counter would have read zero while letters were going out.
    """
    since = start_of_utc_day()
    with session_scope() as session:
        expire_stale_proposals(session)
        by_status = dict(
            session.execute(select(Proposal.status, func.count()).group_by(Proposal.status)).all()
        )
        approved_today = session.execute(
            select(func.count())
            .select_from(Approval)
            .where(Approval.decision == "approve", Approval.decided_at >= since)
        ).scalar_one()
        sent_today = session.execute(
            select(func.count())
            .select_from(Execution)
            .where(Execution.status == "succeeded", Execution.executed_at >= since)
        ).scalar_one()
        return {
            "pending": by_status.get("pending", 0),
            "approved_today": approved_today,
            "sent_today": sent_today,
            "approved": by_status.get("approved", 0),
            "rejected": by_status.get("rejected", 0),
            "executed": by_status.get("executed", 0),
            "failed": by_status.get("failed", 0),
            "expired": by_status.get("expired", 0),
            "sent_total": session.execute(
                select(func.count()).select_from(OutboxMessage)
            ).scalar_one(),
        }


#: Section 9 asks for an overdue-invoice counter on the dashboard, which means a Stripe call
#: on a page load. Cached briefly so repeatedly opening the dashboard does not hammer the API,
#: and degraded to ``None`` rather than failing the page when Stripe cannot be reached -- a
#: counter must never be the reason a screen 500s.
_OVERDUE_CACHE: dict[str, Any] = {"count": None, "at": 0.0}
OVERDUE_CACHE_SECONDS = 60.0


def overdue_invoice_count(*, force: bool = False) -> int | None:
    """How many invoices are open and past due, or ``None`` if Stripe could not be asked."""
    import time

    if not force and time.monotonic() - float(_OVERDUE_CACHE["at"]) < OVERDUE_CACHE_SECONDS:
        return _OVERDUE_CACHE["count"]

    from app.config import settings
    from app.stripe_client.read import StripeReadClient

    config = settings()
    try:
        reader = StripeReadClient(
            config.stripe_api_key_read,
            include_test_clock_fixtures=config.stripe_include_test_clock_invoices,
        )
        count: int | None = len(reader.list_overdue_invoices(min_days_overdue=1, limit=100))
    except Exception as exc:  # noqa: BLE001 - a counter must not take the page down
        log.info("overdue counter unavailable: %s", exc)
        count = None

    _OVERDUE_CACHE.update({"count": count, "at": time.monotonic()})
    return count


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
