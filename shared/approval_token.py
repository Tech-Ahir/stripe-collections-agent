"""The approval token (brief section 5).

A compact HMAC-SHA256 signed token, minted only by the public API's approval endpoint at
the moment a human approves, and verified only by the gateway. Two segments::

    base64url(claims_json) "." base64url(hmac_sha256(secret, first_segment_bytes))

The signature covers the *encoded* first segment, so verification never has to
re-serialise the claims to check them -- which removes any question of whether the
verifier and the minter agree on JSON formatting.

Deliberate choices, because this file is the commercial value of the trial:

* **No JWT library.** A hand-rolled 60 lines that a reviewer can read end to end beats a
  dependency whose defaults have to be audited. There is one algorithm, it is not
  negotiable, and there is no `alg` field for an attacker to set to `none`.
* **`extra="forbid"` on the claims.** A token carrying an unexpected field is refused
  rather than silently accepted with the extra ignored.
* **Expiry is not checked here.** Section 5 makes signature validity check 1 and expiry
  check 2, with different refusal codes, and the gateway must be able to report which one
  failed. Mixing them into one function would collapse two distinct answers into one.
* **Constant-time comparison**, so the signature cannot be discovered a byte at a time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.clock import now_utc

ALGORITHM = "HMAC-SHA256"


class TokenError(Exception):
    """Base class for a token that cannot be trusted."""


class MalformedToken(TokenError):
    """The token is not two base64url segments carrying valid claims JSON."""


class InvalidSignature(TokenError):
    """The signature does not match. The token was not minted with our secret."""


class ApprovalTokenClaims(BaseModel):
    """Exactly the fields of section 5's token, and nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    #: Binds the token to exact content. See shared/hashing.py.
    payload_hash: str
    action_type: str
    approver: str
    #: Single use.
    nonce: str = Field(default_factory=lambda: str(uuid.uuid4()))
    iat: int
    exp: int

    def is_expired(self, *, at: int | None = None) -> bool:
        return (at if at is not None else int(now_utc().timestamp())) >= self.exp


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a malformed token
        raise MalformedToken("token segment is not valid base64url") from exc


def _sign(segment: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), segment.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def mint(claims: ApprovalTokenClaims, secret: str) -> str:
    """Sign a set of claims. Called only by the approval endpoint, never by the agent."""
    if not secret:
        raise ValueError("refusing to mint an approval token with an empty secret")
    segment = _b64encode(
        json.dumps(claims.model_dump(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return segment + "." + _sign(segment, secret)


def decode(raw: str, secret: str) -> ApprovalTokenClaims:
    """Check 1: verify the signature, then and only then parse the claims.

    Raises ``InvalidSignature`` or ``MalformedToken``. Never returns partially trusted
    data: nothing inside the token is parsed as meaningful until the signature holds.
    """
    if not secret:
        raise ValueError("refusing to verify an approval token with an empty secret")
    if not raw or not isinstance(raw, str):
        raise MalformedToken("no approval token supplied")

    parts = raw.split(".")
    if len(parts) != 2 or not all(parts):
        raise MalformedToken(f"expected 2 token segments, got {len(parts)}")

    segment, signature = parts
    expected = _sign(segment, secret)
    if not hmac.compare_digest(signature, expected):
        raise InvalidSignature("approval token signature does not verify")

    try:
        claims = json.loads(_b64decode(segment))
    except json.JSONDecodeError as exc:
        raise MalformedToken("token claims are not valid JSON") from exc
    if not isinstance(claims, dict):
        raise MalformedToken("token claims are not an object")

    try:
        return ApprovalTokenClaims.model_validate(claims)
    except Exception as exc:  # noqa: BLE001 - pydantic raises ValidationError
        raise MalformedToken(f"token claims do not match the contract: {exc}") from exc
