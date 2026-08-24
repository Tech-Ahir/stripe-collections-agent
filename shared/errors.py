"""The error contract (brief section 6).

    { "error": { "code": "not_approved",
                 "message": "Proposal is in status 'pending'.",
                 "proposal_id": "..." } }

Gateway refusal codes surface to the UI unchanged, because the refusal is the feature. A
generic "something went wrong" toast would hide the single most important thing this system
does.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # The seven checks (section 5)
    INVALID_SIGNATURE = "invalid_signature"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_REPLAYED = "token_replayed"
    NOT_APPROVED = "not_approved"
    APPROVAL_MISMATCH = "approval_mismatch"
    PAYLOAD_MODIFIED = "payload_modified"

    # Request validity, before any check can run
    MALFORMED_TOKEN = "malformed_token"
    MISSING_IDEMPOTENCY_KEY = "missing_idempotency_key"

    # Execution
    EXECUTION_FAILED = "execution_failed"

    # Public API
    PROPOSAL_NOT_FOUND = "proposal_not_found"
    PROPOSAL_NOT_PENDING = "proposal_not_pending"
    RUN_NOT_FOUND = "run_not_found"
    DUPLICATE_PENDING_PROPOSAL = "duplicate_pending_proposal"
    AGENT_NOT_CONFIGURED = "agent_not_configured"
    VALIDATION_FAILED = "validation_failed"


class ApiError(Exception):
    """An error that carries its own HTTP status and serialises to the section 6 shape."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        http_status: int = 400,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.http_status = http_status
        self.extra = {key: value for key, value in extra.items() if value is not None}

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiError({self.code!r}, {self.message!r}, http_status={self.http_status})"


def envelope(code: ErrorCode | str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "error": {
            "code": str(code),
            "message": message,
            **{k: v for k, v in extra.items() if v is not None},
        }
    }
