"""Execution, once all seven checks pass (brief section 5).

    1. Write an executions row with the idempotency key, status pending.
    2. Emit action.execution.started to the audit log.
    3. Call the email adapter with the approved subject and body.
    4. Optionally call stripe.Invoice.send_invoice if ENABLE_STRIPE_INVOICE_SEND is on.
    5. Update the execution row, set proposal status to executed.
    6. Emit action.execution.succeeded or .failed with the provider response.

The transaction boundaries matter. Steps 1 and 2 commit *before* the external call, so the
system has a durable record that it was about to act even if the process dies mid-send; and
so that no database write lock is held across network I/O, which on SQLite would block
every reader for the duration.

The nonce is consumed in step 1, in the same transaction as the execution row. Both are
unique, so two concurrent requests cannot both proceed: one commits, the other collides and
is answered from the record the winner wrote.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from gateway.email_adapter.base import DeliveryError, DeliveryResult, EmailAdapter
from gateway.verify import CHECKS_BY_NUMBER, Refusal, Verified
from shared import audit
from shared.clock import now_utc
from shared.db import session_scope
from shared.errors import ApiError, ErrorCode
from shared.models import Execution, Proposal, TokenNonce

log = logging.getLogger("gateway.executor")


class ExecutionFailed(ApiError):
    def __init__(self, message: str, *, proposal_id: str, execution_id: int, detail: dict):
        super().__init__(
            ErrorCode.EXECUTION_FAILED,
            message,
            http_status=502,
            proposal_id=proposal_id,
            execution_id=execution_id,
            provider=detail or None,
        )


def _claim(verified: Verified) -> Execution:
    """Step 1 and 2, in one transaction, committed before anything external happens."""
    with session_scope() as session:
        execution = Execution(
            proposal_id=verified.proposal.id,
            idempotency_key=verified.idempotency_key,
            status="pending",
            executed_at=now_utc(),
        )
        session.add(execution)
        session.add(
            TokenNonce(
                nonce=verified.claims.nonce,
                proposal_id=verified.proposal.id,
                idempotency_key=verified.idempotency_key,
            )
        )
        session.flush()
        audit.append(
            session,
            actor="gateway",
            event=audit.ACTION_EXECUTION_STARTED,
            subject_type="proposal",
            subject_id=verified.proposal.id,
            detail={
                "execution_id": execution.id,
                "idempotency_key": verified.idempotency_key,
                "action_type": verified.claims.action_type,
                "approver": verified.claims.approver,
                "payload_hash": verified.claims.payload_hash,
                "checks_passed": [outcome.name for outcome in verified.checks],
                "body_disagreed_with_token": verified.body_disagreed_with_token,
            },
        )
        session.flush()
        session.expunge_all()
        return execution


def _finish(
    *,
    execution_id: int,
    proposal_id: str,
    status: str,
    result: dict[str, Any],
    error: str | None,
) -> None:
    """Steps 5 and 6. A separate transaction, taken only after the external call returns."""
    with session_scope() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.status = status
        execution.result = result
        execution.error = error
        execution.executed_at = now_utc()

        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "executed" if status == "succeeded" else "failed"

        audit.append(
            session,
            actor="gateway",
            event=(
                audit.ACTION_EXECUTION_SUCCEEDED
                if status == "succeeded"
                else audit.ACTION_EXECUTION_FAILED
            ),
            subject_type="proposal",
            subject_id=proposal_id,
            detail={"execution_id": execution_id, "result": result, "error": error},
        )


def record_refusal(refusal: Refusal | ApiError) -> None:
    """Append a refused attempt to the audit log.

    A refusal is the feature, so it belongs in the record. This is what makes the audit
    log show a rejected proposal's attempted execution rather than a silent gap.
    """
    detail: dict[str, Any] = {"code": refusal.code, "message": refusal.message}
    detail.update(refusal.extra)
    subject_id = str(refusal.extra.get("proposal_id") or "unknown")
    try:
        with session_scope() as session:
            audit.append(
                session,
                actor="gateway",
                event=audit.ACTION_REFUSED,
                subject_type="proposal",
                subject_id=subject_id,
                detail=detail,
            )
    except Exception:  # noqa: BLE001 - never let auditing turn a refusal into a 500
        log.exception("failed to record a refusal in the audit log: %s", refusal.code)


def execute(
    verified: Verified,
    *,
    adapter: EmailAdapter,
    stripe_send: bool = False,
    stripe_sender: Any = None,
) -> tuple[Execution, dict[str, Any], bool]:
    """Perform the approved action. Returns (execution, result, was_idempotent_replay)."""

    # ---- Check 7 already found this exact request. Return the original result. --------
    if verified.existing_execution is not None:
        existing = verified.existing_execution
        log.info(
            "idempotent replay of execution %s for proposal %s",
            existing.id,
            verified.proposal.id,
        )
        if existing.status == "failed":
            # Honest answer to "did my request succeed?": no. And still no second send.
            raise ExecutionFailed(
                existing.error or "The original attempt for this idempotency key failed.",
                proposal_id=verified.proposal.id,
                execution_id=existing.id,
                detail=dict(existing.result or {}),
            )
        return existing, dict(existing.result or {}), True

    # ---- Step 1 and 2 --------------------------------------------------------------
    try:
        execution = _claim(verified)
    except IntegrityError as exc:
        # Lost a race. Whoever won has written the answer; read it rather than re-sending.
        return _answer_from_the_winner(verified, exc)

    # ---- Step 3 and 4: the only outward-facing moment in the system -----------------
    payload = verified.proposal.payload
    result: dict[str, Any] = {}
    try:
        delivery: DeliveryResult = adapter.send(
            to=verified.proposal.customer_email,
            subject=payload["subject"],
            body=payload["body"],
            meta={
                "proposal_id": verified.proposal.id,
                "invoice_id": verified.proposal.stripe_invoice_id,
                "tone": payload.get("tone"),
                "approver": verified.claims.approver,
                "execution_id": execution.id,
            },
        )
        result["email"] = delivery.as_dict()

        if stripe_send and stripe_sender is not None:
            result["stripe_invoice_send"] = stripe_sender(verified.proposal.stripe_invoice_id)
        else:
            result["stripe_invoice_send"] = {
                "attempted": False,
                "reason": "ENABLE_STRIPE_INVOICE_SEND is off",
            }

    except DeliveryError as exc:
        result["email"] = {"adapter": exc.adapter, "accepted": False, **exc.detail}
        _finish(
            execution_id=execution.id,
            proposal_id=verified.proposal.id,
            status="failed",
            result=result,
            error=str(exc),
        )
        raise ExecutionFailed(
            str(exc),
            proposal_id=verified.proposal.id,
            execution_id=execution.id,
            detail=result.get("email", {}),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is still a failure
        _finish(
            execution_id=execution.id,
            proposal_id=verified.proposal.id,
            status="failed",
            result={"error": type(exc).__name__},
            error=str(exc),
        )
        log.exception("execution %s failed unexpectedly", execution.id)
        raise ExecutionFailed(
            f"The action failed: {exc}",
            proposal_id=verified.proposal.id,
            execution_id=execution.id,
            detail={"error": type(exc).__name__},
        ) from exc

    # ---- Step 5 and 6 --------------------------------------------------------------
    _finish(
        execution_id=execution.id,
        proposal_id=verified.proposal.id,
        status="succeeded",
        result=result,
        error=None,
    )
    return execution, result, False


def _answer_from_the_winner(
    verified: Verified, exc: IntegrityError
) -> tuple[Execution, dict[str, Any], bool]:
    """Two concurrent requests; this one lost the unique constraint. Do not re-send."""
    message = str(getattr(exc, "orig", exc))
    with session_scope() as session:
        existing = (
            session.query(Execution)
            .filter(Execution.idempotency_key == verified.idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            session.expunge_all()
            return existing, dict(existing.result or {}), True

    if "token_nonces" in message or "nonce" in message:
        raise Refusal(
            CHECKS_BY_NUMBER[3],
            "This approval token has already been used to execute a request.",
            http_status=409,
            passed=list(verified.checks[:2]),
            proposal_id=verified.proposal.id,
        ) from exc
    raise exc


def build_stripe_sender(api_key: str):  # pragma: no cover - exercised in Phase 3
    """Step 4, only when ENABLE_STRIPE_INVOICE_SEND is on. Off by default."""
    from gateway.stripe_client.write import send_invoice

    def sender(invoice_id: str) -> dict[str, Any]:
        return send_invoice(api_key, invoice_id)

    return sender
