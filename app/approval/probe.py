"""The "Try to send without approval" probe (brief section 9).

    "Required: a 'Try to send without approval' button on each pending proposal that calls
    the gateway directly and displays the 403 not_approved. Roughly an hour of work. Do not
    drop it -- it is step 4 of the handoff demo."

The button cannot literally call the gateway: the gateway publishes no port, so the browser
cannot reach it, which is acceptance criterion 7. It therefore routes through the app, which
makes the call on the internal network.

**Why this mints a real, validly signed token.** The weak version of this demo sends a
forged token and gets a 401 -- which proves only that the signature check works, something
any HMAC does. The version worth showing signs the token *properly* for a proposal that is
still `pending`, so every cryptographic check passes and the gateway refuses anyway, at
check 4, because it read the status from its own database. That is the actual claim this
architecture makes, and acceptance criterion 5 describes exactly it: "Calling the gateway
directly with an unapproved proposal returns 403 not_approved and sends nothing."

**Why this lives in its own module.** ``app/approval/token.py`` holds an invariant worth
keeping: it mints only from a real approval record, and refuses if the proposal's stored hash
has drifted. This function deliberately breaks that invariant, so it is not allowed to share
a file with it. It is also gated by ``ENABLE_UNAPPROVED_ATTEMPT_DEMO``, and it refuses to
target the one status that could actually execute -- ``approved``. Anything else is fair
game, which matters: section 11 step 6 is "reject one with a note, attempt execution, show
the refusal", and that attempt has to reach the gateway to be worth showing.

The security property does not depend on the app being unable to mint such a token. It
depends on the gateway reading the proposal's status from state the caller does not control.
That is the whole point, and this is how it gets shown.
"""

from __future__ import annotations

import logging
import uuid

from shared.approval_token import ApprovalTokenClaims, mint
from shared.clock import now_utc
from shared.hashing import hash_payload
from shared.models import Proposal

log = logging.getLogger("app.approval.probe")


class ProbeNotPermitted(RuntimeError):
    """The probe may never target a proposal that could actually execute."""


#: The only status the gateway will execute. The probe must never mint a token for it.
EXECUTABLE_STATUS = "approved"


def mint_probe_token_for_demonstration(
    proposal: Proposal,
    *,
    actor: str,
    secret: str,
    ttl_seconds: int = 300,
) -> str:
    """A correctly signed token for an UNAPPROVED proposal, so the gateway can refuse it.

    Raises ``ProbeNotPermitted`` for an approved proposal. That is the whole restriction, and
    it is the important one: if the probe could target an approved proposal it would be a way
    to execute one without going through the approval endpoint, which is exactly the hole this
    system exists to close. Pending, rejected, expired and executed are all safe to attempt,
    and the gateway refuses every one of them at check 4.
    """
    if proposal.status == EXECUTABLE_STATUS:
        raise ProbeNotPermitted(
            "The unapproved-send probe must never target an approved proposal: that is the "
            "one status the gateway will act on. Refusing to mint a token for it."
        )

    issued_at = int(now_utc().timestamp())
    claims = ApprovalTokenClaims(
        proposal_id=proposal.id,
        payload_hash=hash_payload(proposal.payload),
        action_type=proposal.action_type,
        approver=actor,
        nonce=str(uuid.uuid4()),
        iat=issued_at,
        exp=issued_at + ttl_seconds,
    )
    log.info(
        "minting a demonstration token for %s proposal %s; the gateway is expected to refuse "
        "it at check 4",
        proposal.status,
        proposal.id,
    )
    return mint(claims, secret)
