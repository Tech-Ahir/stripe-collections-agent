"""The seven checks (brief section 5).

The gateway performs all seven, in order, and refuses on the first failure. Nothing here
may be skipped, reordered or short-circuited: the order is the contract, and
``tests/test_boundary_refusals.py`` asserts both the order and that a refusal at check N
reports exactly N-1 passes.

    #  CHECK                                        REFUSAL                THREAT ANSWERED
    1  HMAC signature valid                         401 invalid_signature  a forged request
    2  Token not expired                            401 token_expired      a stale approval
    3  Nonce not seen before                        409 token_replayed     one approval, two sends
    4  Proposal exists and status is approved        403 not_approved       an unapproved send
    5  Approval record exists, is an approve, and    403 approval_mismatch  a fabricated approver
       matches the token's approver
    6  Recomputed payload hash equals the token's    409 payload_modified   approve one letter,
       payload_hash                                                        send another
    7  Idempotency key unused                        200, original result   a duplicate send

**Check 4 is the one that matters.** The status is read from the database by this process.
It is never taken from the request body, which is why a forged or replayed request cannot
cause a send: the gateway's answer comes from state the caller does not control.

**Nothing in the request body is used in any decision.** Section 5 says the gateway trusts
only the signature, its own record of the proposal, and the idempotency key -- so this
module reads the proposal id and the payload hash from the *token*, not from the body. The
body's copies are recorded in the audit log for forensics and are otherwise inert. A caller
who sends someone else's proposal id in the body changes nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.approval_token import (
    ApprovalTokenClaims,
    InvalidSignature,
    MalformedToken,
    decode,
)
from shared.clock import now_utc
from shared.errors import ApiError, ErrorCode
from shared.hashing import hash_payload, hashes_equal
from shared.models import Approval, Execution, Proposal, TokenNonce

# ----------------------------------------------------------------------------------
# The registry. Section 5's table, in code.
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Check:
    number: int
    name: str
    #: The refusal code on failure. ``None`` for check 7, which short-circuits with 200.
    refusal_code: str | None
    question: str
    threat: str


CHECKS: tuple[Check, ...] = (
    Check(
        1,
        "hmac_signature_valid",
        ErrorCode.INVALID_SIGNATURE,
        "Was this token minted with the shared signing secret?",
        "A forged request. Without the secret, a caller cannot produce a token at all.",
    ),
    Check(
        2,
        "token_not_expired",
        ErrorCode.TOKEN_EXPIRED,
        "Is the token still inside its fifteen minute life?",
        "A stale approval replayed long after the operator moved on.",
    ),
    Check(
        3,
        "nonce_unused",
        ErrorCode.TOKEN_REPLAYED,
        "Has this token already been used to execute a different request?",
        "One approval turned into two sends.",
    ),
    Check(
        4,
        "proposal_is_approved",
        ErrorCode.NOT_APPROVED,
        "Does the proposal exist, and does THIS DATABASE say it is approved?",
        "A send with no human approval behind it. The answer comes from state the "
        "caller does not control.",
    ),
    Check(
        5,
        "approval_matches_approver",
        ErrorCode.APPROVAL_MISMATCH,
        "Is there an approve record, and does it name the token's approver and nonce?",
        "An approved status with no approval behind it, or a token attributed to "
        "someone who never approved anything.",
    ),
    Check(
        6,
        "payload_hash_matches",
        ErrorCode.PAYLOAD_MODIFIED,
        "Does the stored letter still hash to what the operator approved?",
        "Approving one letter and sending a different one.",
    ),
    Check(
        7,
        "idempotency_key_unused",
        None,
        "Has this exact request already been executed?",
        "A retried request causing a second send. Returns the original result instead.",
    ),
)

CHECKS_BY_NUMBER = {check.number: check for check in CHECKS}


# ----------------------------------------------------------------------------------
# Outcomes
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    number: int
    name: str
    passed: bool
    question: str

    def as_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "name": self.name,
            "passed": self.passed,
            "question": self.question,
        }


class Refusal(ApiError):
    """A refusal at a specific check. Carries which check failed and which had passed."""

    def __init__(
        self,
        check: Check,
        message: str,
        *,
        http_status: int,
        passed: list[CheckOutcome],
        proposal_id: str | None = None,
        **extra: object,
    ) -> None:
        assert check.refusal_code is not None, "check 7 does not refuse"
        super().__init__(
            check.refusal_code,
            message,
            http_status=http_status,
            proposal_id=proposal_id,
            failed_check=check.number,
            checks_passed=[outcome.name for outcome in passed],
            **extra,
        )
        self.check = check
        self.passed = passed


@dataclass(slots=True)
class Verified:
    """Everything the executor needs, and nothing it should have to look up again."""

    claims: ApprovalTokenClaims
    proposal: Proposal
    approval: Approval
    idempotency_key: str
    checks: list[CheckOutcome] = field(default_factory=list)
    #: Set when check 7 finds this exact request already executed.
    existing_execution: Execution | None = None
    #: True when the untrusted body disagreed with the token. Recorded, never acted on.
    body_disagreed_with_token: bool = False

    @property
    def idempotent_replay(self) -> bool:
        return self.existing_execution is not None


# ----------------------------------------------------------------------------------
# The verification sequence
# ----------------------------------------------------------------------------------


def verify_execution_request(
    session: Session,
    *,
    raw_token: str | None,
    idempotency_key: str | None,
    body_proposal_id: str | None,
    body_payload_hash: str | None,
    secret: str,
    now: datetime | None = None,
) -> Verified:
    """Run the seven checks in order. Raise ``Refusal`` on the first failure.

    Read this top to bottom: the checks appear in exactly the order section 5 specifies,
    each one guarded by nothing but the ones above it.
    """
    at = now or now_utc()
    at_epoch = int(at.timestamp())
    passed: list[CheckOutcome] = []

    def record(number: int) -> None:
        check = CHECKS_BY_NUMBER[number]
        passed.append(CheckOutcome(check.number, check.name, True, check.question))

    def refuse(number: int, message: str, *, http_status: int, **extra: object) -> Refusal:
        return Refusal(
            CHECKS_BY_NUMBER[number],
            message,
            http_status=http_status,
            passed=list(passed),
            **extra,
        )

    # ---- Request validity, before any of the seven can be meaningful ----------------
    #
    # Not a check: without an idempotency key there is no way to make check 7 safe, so the
    # request is malformed rather than refused.
    if not idempotency_key or not idempotency_key.strip():
        raise ApiError(
            ErrorCode.MISSING_IDEMPOTENCY_KEY,
            "The X-Idempotency-Key header is required.",
            http_status=400,
        )
    idempotency_key = idempotency_key.strip()

    # ---- CHECK 1: HMAC signature valid --------------------------------------------
    try:
        claims = decode(raw_token or "", secret)
    except InvalidSignature as exc:
        raise refuse(1, str(exc), http_status=401) from exc
    except MalformedToken as exc:
        # Same check, a distinguishable cause: the token was not even well formed, so
        # there was nothing to verify a signature over.
        raise ApiError(
            ErrorCode.MALFORMED_TOKEN,
            str(exc),
            http_status=401,
            failed_check=1,
            checks_passed=[],
        ) from exc
    record(1)

    # ---- CHECK 2: token not expired -----------------------------------------------
    if claims.is_expired(at=at_epoch):
        raise refuse(
            2,
            f"Approval token expired at {claims.exp} (now {at_epoch}).",
            http_status=401,
            proposal_id=claims.proposal_id,
        )
    record(2)

    # ---- CHECK 3: nonce not seen before -------------------------------------------
    #
    # The nonce is consumed at execution time, bound to the idempotency key that consumed
    # it. A different key means the same approval is being used for a NEW request, which is
    # a replay. The same key means this is the same logical request retried, which check 7
    # answers with the original result. Both of section 5's required tests only hold under
    # that distinction.
    consumed = session.get(TokenNonce, claims.nonce)
    if consumed is not None and consumed.idempotency_key != idempotency_key:
        raise refuse(
            3,
            "This approval token has already been used to execute a request.",
            http_status=409,
            proposal_id=claims.proposal_id,
        )
    record(3)

    # ---- CHECK 4: the proposal exists and THIS DATABASE says it is approved ---------
    #
    # The check that matters. Note the source of truth: session.get, not the request body.
    proposal = session.get(Proposal, claims.proposal_id)
    if proposal is None:
        raise refuse(
            4,
            "No such proposal.",
            http_status=403,
            proposal_id=claims.proposal_id,
        )

    # A retry of a request THIS gateway already executed is not an unapproved send: the
    # proposal left 'approved' precisely because of this idempotency key. Section 5
    # requires both "replay a valid token -> 409" and "repeat the same idempotency key ->
    # the original result", and the second is only reachable if check 4 recognises its own
    # completed work. Any other status, or the same status with a different key, refuses.
    already_executed_by_this_request = session.execute(
        select(Execution).where(
            Execution.idempotency_key == idempotency_key,
            Execution.proposal_id == proposal.id,
        )
    ).scalar_one_or_none()

    if proposal.status != "approved" and already_executed_by_this_request is None:
        raise refuse(
            4,
            f"Proposal is in status '{proposal.status}'.",
            http_status=403,
            proposal_id=proposal.id,
            proposal_status=proposal.status,
        )
    if (
        proposal.expires_at is not None
        and proposal.expires_at <= at
        and already_executed_by_this_request is None
    ):
        raise refuse(
            4,
            "Proposal has expired. Stale approvals are not executed.",
            http_status=403,
            proposal_id=proposal.id,
            reason="expired",
        )
    record(4)

    # ---- CHECK 5: an approve record exists and matches the token's approver ---------
    approval = session.execute(
        select(Approval)
        .where(Approval.proposal_id == proposal.id, Approval.decision == "approve")
        .order_by(Approval.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if approval is None:
        raise refuse(
            5,
            "The proposal is marked approved but carries no approval record.",
            http_status=403,
            proposal_id=proposal.id,
        )
    if approval.actor != claims.approver:
        raise refuse(
            5,
            "The token's approver does not match the recorded approver.",
            http_status=403,
            proposal_id=proposal.id,
        )
    if approval.token_nonce != claims.nonce:
        raise refuse(
            5,
            "The token was not the one minted for this approval.",
            http_status=403,
            proposal_id=proposal.id,
        )
    record(5)

    # ---- CHECK 6: the stored letter still hashes to what was approved --------------
    #
    # Recomputed from this process's own copy of the payload, and compared in constant
    # time against the hash inside the signed token. Strict: no trimming, no normalising.
    recomputed = hash_payload(proposal.payload)
    if not hashes_equal(recomputed, claims.payload_hash):
        raise refuse(
            6,
            "The letter has changed since it was approved.",
            http_status=409,
            proposal_id=proposal.id,
        )
    record(6)

    # ---- CHECK 7: idempotency key unused ------------------------------------------
    #
    # The only row of section 5's table whose outcome is not a refusal: a used key returns
    # the original result and does not re-send.
    existing = (
        already_executed_by_this_request
        or session.execute(
            select(Execution).where(Execution.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
    )
    record(7)

    disagreed = bool(
        (body_proposal_id and body_proposal_id != claims.proposal_id)
        or (body_payload_hash and body_payload_hash != claims.payload_hash)
    )

    return Verified(
        claims=claims,
        proposal=proposal,
        approval=approval,
        idempotency_key=idempotency_key,
        checks=passed,
        existing_execution=existing,
        body_disagreed_with_token=disagreed,
    )
