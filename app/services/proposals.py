"""The approval path (brief section 6).

The one place a human decision becomes an action. Everything about it is deliberate:

* **The edit is applied before the token is minted.** Section 6: "If the operator edits the
  body before approving, the edited text is stored on the approval record and the payload
  hash is recomputed from the edited content before the token is minted. The operator
  approves what they actually read. Do not mint the token from the original draft."
* **The database work commits before the network call.** The approval is a fact the moment
  the human makes it, and it must survive the gateway being slow, unreachable, or refusing.
  No write lock is held across the HTTP request.
* **A refusal is returned, not swallowed.** The proposal stays `approved` and the caller
  gets the gateway's code verbatim. Approving something is not undone by the gateway
  declining to act on it, and hiding the code would hide the feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.approval.probe import mint_probe_token_for_demonstration
from app.approval.token import mint_for_approval, new_nonce
from app.config import AppSettings
from app.gateway_client import GatewayClient, GatewayOutcome
from app.store.repositories import expire_stale_proposals
from shared import audit
from shared.clock import now_utc
from shared.db import session_scope
from shared.errors import ApiError, ErrorCode
from shared.hashing import hash_payload
from shared.models import Approval, Execution, Proposal

log = logging.getLogger("app.services.proposals")


@dataclass(slots=True)
class Decision:
    proposal_id: str
    status: str
    decision: str
    outcome: GatewayOutcome | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "proposal_id": self.proposal_id,
            "decision": self.decision,
            "status": self.status,
        }
        if self.outcome is not None:
            body["gateway"] = self.outcome.as_dict()
        return body


def _load_pending(session, proposal_id: str) -> Proposal:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise ApiError(
            ErrorCode.PROPOSAL_NOT_FOUND,
            f"No proposal {proposal_id}.",
            http_status=404,
            proposal_id=proposal_id,
        )
    if proposal.status != "pending":
        raise ApiError(
            ErrorCode.PROPOSAL_NOT_PENDING,
            f"Proposal is in status '{proposal.status}' and can no longer be decided.",
            http_status=409,
            proposal_id=proposal_id,
            proposal_status=proposal.status,
        )
    if proposal.expires_at is not None and proposal.expires_at <= now_utc():
        # Belt and braces with expire_stale_proposals: a proposal must not become
        # approvable merely because no sweep has run since it lapsed.
        raise ApiError(
            ErrorCode.PROPOSAL_NOT_PENDING,
            "Proposal has expired. Stale approvals are worse than no approvals.",
            http_status=409,
            proposal_id=proposal_id,
            proposal_status="expired",
        )
    return proposal


def save_edit(proposal_id: str, *, body: str, actor: str) -> dict[str, Any]:
    """Store an edited letter, re-hash it, and say so.

    Section 9: "Editing after saving invalidates any prior hash, which the UI states." Any
    token minted before this edit can no longer execute, because check 6 recomputes the hash.
    """
    with session_scope() as session:
        expire_stale_proposals(session)
        proposal = _load_pending(session, proposal_id)
        payload = dict(proposal.payload)
        previous_hash = proposal.payload_hash
        payload["body"] = body
        proposal.payload = payload
        proposal.payload_hash = hash_payload(payload)
        audit.append(
            session,
            actor="operator",
            event=audit.PROPOSAL_EDITED,
            subject_type="proposal",
            subject_id=proposal.id,
            detail={
                "actor": actor,
                "previous_payload_hash": previous_hash,
                "payload_hash": proposal.payload_hash,
            },
        )
        return {
            "proposal_id": proposal.id,
            "payload_hash": proposal.payload_hash,
            "previous_payload_hash": previous_hash,
            "note": (
                "The letter was saved and its hash recomputed. Any approval token minted "
                "before this edit can no longer execute."
            ),
        }


def reject(proposal_id: str, *, actor: str, note: str) -> Decision:
    """Terminal. Section 6 requires a note."""
    if not (note or "").strip():
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "A note is required when rejecting a proposal.",
            http_status=422,
            proposal_id=proposal_id,
        )
    with session_scope() as session:
        expire_stale_proposals(session)
        proposal = _load_pending(session, proposal_id)
        session.add(
            Approval(
                proposal_id=proposal.id,
                decision="reject",
                actor=actor,
                note=note,
                decided_at=now_utc(),
            )
        )
        proposal.status = "rejected"
        audit.append(
            session,
            actor="operator",
            event=audit.APPROVAL_REJECTED,
            subject_type="proposal",
            subject_id=proposal.id,
            detail={"actor": actor, "note": note},
        )
        return Decision(proposal_id=proposal.id, status="rejected", decision="reject")


def approve(
    proposal_id: str,
    *,
    actor: str,
    note: str | None,
    edited_body: str | None,
    settings: AppSettings,
    gateway: GatewayClient | None = None,
) -> Decision:
    """Record the approval, mint the token, call the gateway, return the outcome."""
    secret = settings.require_signing_secret()

    # --- everything that must be durable, in one transaction, before any network call ---
    with session_scope() as session:
        expire_stale_proposals(session)
        proposal = _load_pending(session, proposal_id)

        if edited_body is not None and edited_body != proposal.payload.get("body"):
            payload = dict(proposal.payload)
            previous_hash = proposal.payload_hash
            payload["body"] = edited_body
            proposal.payload = payload
            proposal.payload_hash = hash_payload(payload)
            audit.append(
                session,
                actor="operator",
                event=audit.PROPOSAL_EDITED,
                subject_type="proposal",
                subject_id=proposal.id,
                detail={
                    "actor": actor,
                    "previous_payload_hash": previous_hash,
                    "payload_hash": proposal.payload_hash,
                    "reason": "edited at approval time",
                },
            )

        approval = Approval(
            proposal_id=proposal.id,
            decision="approve",
            actor=actor,
            note=note,
            edited_body=edited_body,
            token_nonce=new_nonce(),
            decided_at=now_utc(),
        )
        proposal.status = "approved"
        session.add(approval)
        session.flush()

        audit.append(
            session,
            actor="operator",
            event=audit.APPROVAL_GRANTED,
            subject_type="proposal",
            subject_id=proposal.id,
            detail={
                "actor": actor,
                "note": note,
                "edited": edited_body is not None,
                "payload_hash": proposal.payload_hash,
            },
        )

        # Minted from the CURRENT payload, so it authorises exactly what was read.
        token = mint_for_approval(
            proposal,
            approval,
            secret=secret,
            ttl_seconds=settings.approval_token_ttl_seconds,
        )
        audit.append(
            session,
            actor="operator",
            event=audit.APPROVAL_TOKEN_MINTED,
            subject_type="proposal",
            subject_id=proposal.id,
            detail={
                "approver": actor,
                "nonce": approval.token_nonce,
                "expires_in_seconds": settings.approval_token_ttl_seconds,
            },
        )
        payload_hash = proposal.payload_hash

    # --- the crossing point, outside the transaction --------------------------------
    client = gateway or GatewayClient(
        settings.gateway_url, timeout=settings.gateway_timeout_seconds
    )
    outcome = client.execute(token=token, proposal_id=proposal_id, payload_hash=payload_hash)

    with session_scope() as session:
        current = session.get(Proposal, proposal_id)
        status = current.status if current else "unknown"

    return Decision(proposal_id=proposal_id, status=status, decision="approve", outcome=outcome)


def attempt_unapproved(
    proposal_id: str,
    *,
    actor: str,
    settings: AppSettings,
    gateway: GatewayClient | None = None,
) -> Decision:
    """Step 4 of the handoff demo: ask the gateway to send something nobody approved.

    The token is real and correctly signed. The refusal comes from the gateway's own reading
    of the proposal's status, which is the claim this architecture makes.
    """
    if not settings.enable_unapproved_attempt_demo:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The unapproved-send demonstration is disabled (ENABLE_UNAPPROVED_ATTEMPT_DEMO=false).",
            http_status=403,
        )
    secret = settings.require_signing_secret()

    with session_scope() as session:
        # Deliberately NOT _load_pending: attempting a rejected proposal is section 11's
        # step 6, and the probe itself refuses the only status that could execute.
        proposal = session.get(Proposal, proposal_id)
        if proposal is None:
            raise ApiError(
                ErrorCode.PROPOSAL_NOT_FOUND,
                f"No proposal {proposal_id}.",
                http_status=404,
                proposal_id=proposal_id,
            )
        token = mint_probe_token_for_demonstration(
            proposal, actor=actor, secret=secret, ttl_seconds=300
        )
        payload_hash = proposal.payload_hash

    client = gateway or GatewayClient(
        settings.gateway_url, timeout=settings.gateway_timeout_seconds
    )
    outcome = client.execute(token=token, proposal_id=proposal_id, payload_hash=payload_hash)

    with session_scope() as session:
        current = session.get(Proposal, proposal_id)
        status = current.status if current else "unknown"
        sent = (
            session.execute(select(Execution).where(Execution.proposal_id == proposal_id))
            .scalars()
            .all()
        )

    log.info(
        "unapproved-send attempt on %s -> %s (%s); executions now: %d",
        proposal_id,
        outcome.http_status,
        outcome.error_code,
        len(sent),
    )
    return Decision(
        proposal_id=proposal_id,
        status=status,
        decision="attempt_unapproved",
        outcome=outcome,
    )
