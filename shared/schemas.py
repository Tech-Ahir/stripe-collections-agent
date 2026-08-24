"""Wire contracts shared by the two services.

The execute request is the only message that crosses the boundary, so its shape is
declared once, here, and imported by both sides.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExecuteActionRequest(BaseModel):
    """The body of ``POST /internal/actions/execute``.

    ``extra="ignore"`` is deliberate. Unknown fields are not part of the contract and are
    not an error -- and, more importantly, a caller who adds ``"status": "approved"`` to
    the body must find that it changes nothing rather than that it produces a validation
    error which might read as "close to working".

    Neither field is used in any decision. The gateway reads the proposal id and the
    payload hash from the signed token. These are the caller's copies, kept for the audit
    record so a mismatch is visible after the fact.
    """

    model_config = ConfigDict(extra="ignore")

    proposal_id: str
    payload_hash: str


class CheckReport(BaseModel):
    number: int
    name: str
    passed: bool
    question: str


class ExecuteActionResponse(BaseModel):
    status: Literal["executed", "already_executed"]
    proposal_id: str
    execution_id: int
    idempotency_key: str
    #: True when check 7 short-circuited: the original result, and no second send.
    idempotent_replay: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    #: The seven checks, in order, as they were evaluated. The UI shows these on approval.
    checks: list[CheckReport] = Field(default_factory=list)
