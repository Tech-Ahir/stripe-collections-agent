"""The data model (brief section 3).

Both services map these tables. The models live in ``shared/`` rather than under ``app/``
precisely so the gateway never has to import the agent service in order to read a
proposal -- see CLAUDE.md rule 3 and knowledge-base note 08.

Lifecycle enums are enforced by CHECK constraints as well as by Python, so the database
itself documents the proposal lifecycle and a typo'd status cannot be persisted.

``audit_events`` has no UPDATE or DELETE path anywhere in this codebase (rule 7). Its
``id`` is an explicit monotonic integer rather than an autoincrement, because each row's
hash commits to its own id and the value must therefore be known before the INSERT.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from shared.clock import now_utc
from shared.types import UtcDateTime


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _check(column: str, *allowed: str) -> CheckConstraint:
    values = ", ".join("'" + v + "'" for v in allowed)
    return CheckConstraint(column + " IN (" + values + ")", name="ck_" + column)


# ----------------------------------------------------------------------------------
# Runs and the transcript
# ----------------------------------------------------------------------------------

RUN_STATUSES = ("queued", "running", "awaiting_approval", "completed", "failed")
STEP_TYPES = ("thought", "tool_call", "tool_result", "message")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (_check("status", *RUN_STATUSES),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    operator_id: Mapped[str] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Addition to the brief's table: the run's own parameters (min_days_overdue,
    # max_proposals). The draft tool enforces the proposal cap from here, and the run
    # detail screen shows what the operator actually asked for.
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    steps: Mapped[list[RunStep]] = relationship(
        back_populates="run", order_by="RunStep.seq", cascade="all, delete-orphan"
    )
    proposals: Mapped[list[Proposal]] = relationship(back_populates="run")


class RunStep(Base):
    """One entry in the run transcript.

    The transcript is a deliverable, not a debug view: every tool call and every tool
    result the agent makes is persisted here, in order, and streamed to the UI.
    """

    __tablename__ = "run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="ux_run_steps_run_seq"),
        _check("type", *STEP_TYPES),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(16))
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)

    run: Mapped[Run] = relationship(back_populates="steps")


# ----------------------------------------------------------------------------------
# Proposals, approvals, executions
# ----------------------------------------------------------------------------------

PROPOSAL_STATUSES = ("pending", "approved", "rejected", "executed", "failed", "expired")
TERMINAL_PROPOSAL_STATUSES = ("rejected", "executed", "failed", "expired")
ACTION_TYPES = ("send_collection_letter",)
TONES = ("friendly", "firm", "final")


class Proposal(Base):
    """A drafted letter awaiting a human decision -- the agent's terminal capability."""

    __tablename__ = "proposals"
    __table_args__ = (
        _check("status", *PROPOSAL_STATUSES),
        _check("action_type", *ACTION_TYPES),
        # One pending proposal per invoice, enforced by the store and not by the prompt.
        Index(
            "ux_proposals_one_pending_per_invoice",
            "stripe_invoice_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(32), default="send_collection_letter")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(71))
    rationale: Mapped[str] = mapped_column(Text)
    stripe_invoice_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_email: Mapped[str] = mapped_column(String(320))
    amount_due: Mapped[int] = mapped_column(BigInteger)  # minor units, always
    currency: Mapped[str] = mapped_column(String(3))
    days_overdue: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)

    run: Mapped[Run] = relationship(back_populates="proposals")
    approvals: Mapped[list[Approval]] = relationship(back_populates="proposal")
    executions: Mapped[list[Execution]] = relationship(back_populates="proposal")


class Approval(Base):
    """The human decision.

    ``edited_body`` holds what the operator actually read, when they changed the draft
    before approving. The payload hash is recomputed from that text, never from the
    original draft.
    """

    __tablename__ = "approvals"
    __table_args__ = (_check("decision", "approve", "reject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), index=True)
    decision: Mapped[str] = mapped_column(String(8))
    actor: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)
    token_nonce: Mapped[str | None] = mapped_column(String(36), nullable=True)

    proposal: Mapped[Proposal] = relationship(back_populates="approvals")


class Execution(Base):
    """A gateway-side send attempt. ``idempotency_key`` is unique: one key, one send."""

    __tablename__ = "executions"
    __table_args__ = (
        # 'pending' is written before the external call (section 5, execution step 1);
        # sections 3 and 5 together imply these three states.
        _check("status", "pending", "succeeded", "failed"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    proposal: Mapped[Proposal] = relationship(back_populates="executions")


class TokenNonce(Base):
    """Consumed approval-token nonces. Gateway-owned.

    An addition to the brief's table list. Check 3 -- "nonce not seen before" -- needs a
    seen-set that the gateway owns and can make atomic. ``approvals.token_nonce`` records
    which nonce was *minted*, which is a different fact. With ``nonce`` as the primary
    key, consuming a nonce is an INSERT that either succeeds or collides, so two
    concurrent replays cannot both get through.

    ``idempotency_key`` records which request consumed the nonce, and it is what lets
    section 5's checks 3 and 7 both hold. A *replay* is the same token used to cause a new
    send, so a different idempotency key. A *retry* is the same logical request arriving
    twice, so the same key -- and that falls through to check 7, which returns the original
    result rather than refusing. Without this column one of the two required behaviours
    has to give way.
    """

    __tablename__ = "token_nonces"

    nonce: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    consumed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)


class OutboxMessage(Base):
    """A captured letter.

    An addition to the brief's table list, required by section 8: the default adapter
    "writes the message to the database and to /data/outbox/".
    """

    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    proposal_id: Mapped[str] = mapped_column(String(36), index=True)
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    adapter: Mapped[str] = mapped_column(String(16))
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)


# ----------------------------------------------------------------------------------
# The audit chain
# ----------------------------------------------------------------------------------

AUDIT_ACTORS = ("agent", "operator", "gateway", "system")


class AuditEvent(Base):
    """Append-only and hash-chained. Never updated, never deleted.

    ``prev_hash`` and ``hash`` are both UNIQUE. That is what makes a forked chain
    physically impossible: if two processes read the same tail and both try to append,
    the second INSERT violates the uniqueness of ``prev_hash`` and is retried against
    the new tail.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("prev_hash", name="ux_audit_prev_hash"),
        UniqueConstraint("hash", name="ux_audit_hash"),
        _check("actor", *AUDIT_ACTORS),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=now_utc)
    actor: Mapped[str] = mapped_column(String(16), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(71))
    hash: Mapped[str] = mapped_column(String(71))
