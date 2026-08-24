"""Minting approval tokens (brief section 5).

A token is minted in exactly one circumstance: a human has just approved a specific
proposal, at the public API's approval endpoint. There is no other caller, and
``.importlinter`` contract 6 forbids ``app.agent`` from importing this module at all -- the
agent must not be able to mint its own permission slip even by accident.

The hash is **recomputed from the payload** rather than read from
``proposals.payload_hash``. The two must agree, and this function refuses to mint if they
do not: a stored hash that has drifted from its payload would mean the operator approved
one thing while the token authorised another, which is the precise failure check 6 exists
to prevent. Better to fail the approval than to mint a token nobody can trust.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from shared.approval_token import ApprovalTokenClaims, mint
from shared.clock import now_utc
from shared.hashing import hash_payload, hashes_equal
from shared.models import Approval, Proposal


class PayloadHashDrift(RuntimeError):
    """The proposal's stored hash does not match its payload. Refuse to mint."""


def new_nonce() -> str:
    """A single-use identifier, recorded on the approval and consumed by the gateway."""
    return str(uuid.uuid4())


def mint_for_approval(
    proposal: Proposal,
    approval: Approval,
    *,
    secret: str,
    ttl_seconds: int = 900,
    now: datetime | None = None,
) -> str:
    """Sign the approval of exactly this content, by exactly this person, once."""
    if approval.decision != "approve":
        raise ValueError("refusing to mint an approval token for a rejection")
    if not approval.token_nonce:
        raise ValueError("the approval record must carry a nonce before a token is minted")

    recomputed = hash_payload(proposal.payload)
    if not hashes_equal(recomputed, proposal.payload_hash):
        raise PayloadHashDrift(
            "proposals.payload_hash does not match proposals.payload for "
            f"{proposal.id}. Refusing to mint a token that would authorise content the "
            "operator did not approve."
        )

    issued_at = int((now or now_utc()).timestamp())
    claims = ApprovalTokenClaims(
        proposal_id=proposal.id,
        payload_hash=recomputed,
        action_type=proposal.action_type,
        approver=approval.actor,
        nonce=approval.token_nonce,
        iat=issued_at,
        exp=issued_at + ttl_seconds,
    )
    return mint(claims, secret)
