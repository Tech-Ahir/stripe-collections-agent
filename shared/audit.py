"""The append-only, hash-chained audit log (brief section 3, CLAUDE.md rule 7).

Each row stores the hash of the row before it. Removing a row, reordering rows or editing
a field all break the linkage, and ``verify_chain`` reports the first row at which the
break occurs. That is the whole point: the log does not prevent tampering, it makes
tampering visible.

Design notes worth knowing before you change anything here:

* **There is no update and no delete function in this module, and none anywhere else in
  the codebase.** To correct a mistake, append a correcting event. If you find yourself
  wanting ``UPDATE audit_events``, that is the bug.
* ``append`` runs inside *the caller's* transaction, so an action can never be committed
  without its audit event. It does not open its own transaction.
* A forked chain is physically impossible rather than merely unlikely: ``prev_hash`` is
  UNIQUE, so if two processes read the same tail and both try to append, the second INSERT
  violates the constraint. ``shared/db.py:run_in_transaction`` retries the whole unit of
  work when that happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.clock import now_utc
from shared.hashing import GENESIS_HASH, hash_payload
from shared.models import AuditEvent
from shared.types import format_utc

# ----------------------------------------------------------------------------------
# Event catalogue. Names are stable; the UI filters on them.
# ----------------------------------------------------------------------------------

RUN_STARTED = "run.started"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
AGENT_TOOL_CALLED = "agent.tool.called"
PROPOSAL_CREATED = "proposal.created"
PROPOSAL_EDITED = "proposal.edited"
PROPOSAL_EXPIRED = "proposal.expired"
APPROVAL_GRANTED = "approval.granted"
APPROVAL_REJECTED = "approval.rejected"
APPROVAL_TOKEN_MINTED = "approval.token.minted"
ACTION_EXECUTION_STARTED = "action.execution.started"
ACTION_EXECUTION_SUCCEEDED = "action.execution.succeeded"
ACTION_EXECUTION_FAILED = "action.execution.failed"
ACTION_REFUSED = "action.refused"
SYSTEM_STARTED = "system.started"


def compute_hash(
    *,
    id: int,
    ts_iso: str,
    actor: str,
    event: str,
    subject_type: str,
    subject_id: str,
    detail: dict[str, Any],
    prev_hash: str,
) -> str:
    """The row's commitment to its own content and to the row before it."""
    return hash_payload(
        {
            "id": id,
            "ts": ts_iso,
            "actor": actor,
            "event": event,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "detail": detail,
            "prev_hash": prev_hash,
        }
    )


def row_hash(row: AuditEvent) -> str:
    """Recompute a persisted row's hash from its own fields."""
    return compute_hash(
        id=row.id,
        ts_iso=format_utc(row.ts),
        actor=row.actor,
        event=row.event,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        detail=row.detail or {},
        prev_hash=row.prev_hash,
    )


def tail(session: Session) -> AuditEvent | None:
    return session.execute(
        select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)
    ).scalar_one_or_none()


def append(
    session: Session,
    *,
    actor: str,
    event: str,
    subject_type: str,
    subject_id: str,
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one event to the chain, inside the caller's transaction.

    Raises ``IntegrityError`` if another process appended concurrently. Callers that need
    to survive that should run inside ``shared.db.run_in_transaction``.
    """
    previous = tail(session)
    next_id = 1 if previous is None else previous.id + 1
    prev_hash = GENESIS_HASH if previous is None else previous.hash

    ts = now_utc()
    ts_iso = format_utc(ts)
    payload = dict(detail or {})

    row = AuditEvent(
        id=next_id,
        ts=ts,
        actor=actor,
        event=event,
        subject_type=subject_type,
        subject_id=str(subject_id),
        detail=payload,
        prev_hash=prev_hash,
        hash=compute_hash(
            id=next_id,
            ts_iso=ts_iso,
            actor=actor,
            event=event,
            subject_type=subject_type,
            subject_id=str(subject_id),
            detail=payload,
            prev_hash=prev_hash,
        ),
    )
    session.add(row)
    session.flush()
    return row


# ----------------------------------------------------------------------------------
# Verification
# ----------------------------------------------------------------------------------


@dataclass(slots=True)
class ChainStatus:
    """The result of walking the chain from the genesis row to the tail."""

    intact: bool
    length: int
    head_hash: str | None = None
    broken_at_id: int | None = None
    reason: str | None = None
    checked: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intact": self.intact,
            "length": self.length,
            "head_hash": self.head_hash,
            "broken_at_id": self.broken_at_id,
            "reason": self.reason,
        }


def verify_chain(session: Session) -> ChainStatus:
    """Recompute every hash and every link. Reports the first divergent row.

    Four independent ways the chain can be broken, all detected:
      1. a row's content was edited          -> its recomputed hash differs
      2. a row was deleted                   -> the id sequence has a gap
      3. rows were reordered or re-linked     -> prev_hash does not match the predecessor
      4. the first row was replaced           -> genesis prev_hash is wrong
    """
    rows = list(session.execute(select(AuditEvent).order_by(AuditEvent.id.asc())).scalars())
    if not rows:
        return ChainStatus(intact=True, length=0, head_hash=None)

    expected_prev = GENESIS_HASH
    expected_id = 1
    for row in rows:
        if row.id != expected_id:
            return ChainStatus(
                intact=False,
                length=len(rows),
                broken_at_id=row.id,
                reason=f"id sequence broken: expected {expected_id}, found {row.id} "
                "(a row was deleted or inserted out of band)",
            )
        if row.prev_hash != expected_prev:
            return ChainStatus(
                intact=False,
                length=len(rows),
                broken_at_id=row.id,
                reason="prev_hash does not match the previous row's hash "
                "(rows reordered, re-linked, or one removed)",
            )
        recomputed = row_hash(row)
        if recomputed != row.hash:
            return ChainStatus(
                intact=False,
                length=len(rows),
                broken_at_id=row.id,
                reason="row content does not match its stored hash (the row was edited)",
            )
        expected_prev = row.hash
        expected_id += 1

    return ChainStatus(
        intact=True,
        length=len(rows),
        head_hash=rows[-1].hash,
        checked=[r.id for r in rows],
    )
