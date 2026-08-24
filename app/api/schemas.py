"""Public API request and response shapes (brief section 6).

Declared explicitly rather than inferred, because the generated OpenAPI document at /docs is
a named deliverable and a client's mobile team will read it. Section 6: "The UI is one client
of this API and holds no privileges of its own, which is what allows the client's future
mobile app to be an additional client rather than a second implementation."
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ----------------------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str | None = Field(
        default=None,
        description="Free text, or null for the default collections goal.",
        max_length=4000,
    )
    min_days_overdue: int = Field(default=1, ge=0, le=3650)
    max_proposals: int | None = Field(default=None, ge=1, le=100)


class StartRunResponse(BaseModel):
    run_id: str
    status: str
    goal: str


class RunSummary(BaseModel):
    id: str
    goal: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    operator_id: str
    error: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    tool_calls: int = 0
    proposals: int = 0
    pending_proposals: int = 0


class TranscriptStep(BaseModel):
    seq: int
    type: Literal["thought", "tool_call", "tool_result", "message"]
    tool_name: str | None = None
    #: READ or DRAFT on tool steps. There is no ACTION, and its absence is the point.
    tool_class: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RunDetail(RunSummary):
    transcript: list[TranscriptStep] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------------
# Proposals
# ----------------------------------------------------------------------------------


class ProposalSummary(BaseModel):
    id: str
    run_id: str
    action_type: str
    status: str
    customer_name: str | None = None
    customer_email: str
    invoice_number: str | None = None
    stripe_invoice_id: str
    amount_due: int = Field(description="Minor units. 2500 means $25.00.")
    amount_display: str = Field(description="Preformatted. Use this in any human-facing text.")
    currency: str
    days_overdue: int
    tone: str | None = None
    rationale: str
    subject: str | None = None
    created_at: datetime
    expires_at: datetime


class ProposalDetail(ProposalSummary):
    body: str
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str
    invoice_facts: dict[str, Any] = Field(default_factory=dict)
    payment_history: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    executions: list[dict[str, Any]] = Field(default_factory=list)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str | None = Field(default=None, description="Defaults to the operator identity.")
    note: str | None = Field(default=None, max_length=4000)
    edited_body: str | None = Field(
        default=None,
        max_length=100000,
        description=(
            "If present, this text is stored on the approval record and the payload hash is "
            "recomputed from it BEFORE the token is minted. The operator approves what they "
            "actually read."
        ),
    )


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str | None = None
    note: str = Field(min_length=1, max_length=4000, description="Required.")


class SaveEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str | None = None
    body: str = Field(min_length=1, max_length=100000)


class GatewayReport(BaseModel):
    executed: bool
    http_status: int
    idempotency_key: str
    error_code: str | None = None
    error_message: str | None = None
    failed_check: int | None = None
    checks_passed: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    gateway_response: dict[str, Any] = Field(default_factory=dict)


class DecisionResponse(BaseModel):
    proposal_id: str
    decision: str
    status: str
    gateway: GatewayReport | None = None


# ----------------------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------------------


class AuditRow(BaseModel):
    id: int
    ts: datetime
    actor: str
    event: str
    subject_type: str
    subject_id: str
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str


class ChainReport(BaseModel):
    intact: bool
    length: int
    head_hash: str | None = None
    broken_at_id: int | None = None
    reason: str | None = None


class AuditPage(BaseModel):
    events: list[AuditRow]
    total: int
    limit: int
    offset: int
    chain: ChainReport
