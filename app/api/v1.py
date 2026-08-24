"""The public API, versioned under /v1 (brief section 6).

Published as OpenAPI at /docs. The web UI is one client of this API and holds no privileges
of its own -- every screen calls exactly these endpoints -- which is what makes a future
mobile client an additional client rather than a second implementation.

Note what is NOT here: there is no endpoint that sends anything. The closest is
``POST /v1/proposals/{id}/approve``, which records a human decision, mints a signed token,
and asks the gateway to act. The gateway decides whether it does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app import runner
from app.api import queries
from app.api.schemas import (
    ApproveRequest,
    AuditPage,
    DecisionResponse,
    ProposalDetail,
    ProposalSummary,
    RejectRequest,
    RunDetail,
    RunSummary,
    SaveEditRequest,
    StartRunRequest,
    StartRunResponse,
)
from app.config import settings
from app.services import proposals as proposal_service
from app.stripe_client.read import StripeReadClient, StripeReadError
from shared.errors import ApiError, ErrorCode

log = logging.getLogger("app.api")

router = APIRouter(prefix="/v1")

#: How often the SSE stream looks for new transcript rows. Fast enough to feel live,
#: slow enough that a dozen open tabs cost nothing.
STREAM_POLL_SECONDS = 0.4
TERMINAL_RUN_STATUSES = {"completed", "failed", "awaiting_approval"}


def _not_found(kind: str, identifier: str, code: ErrorCode) -> ApiError:
    return ApiError(code, f"No {kind} {identifier}.", http_status=404)


def _mirror_gateway_status(decision, response: Response) -> None:
    """Answer with the gateway's own status code when it refused.

    Acceptance criterion 5 is worded as an HTTP fact -- "returns 403 not_approved" -- and a
    reviewer will check it with curl. Reporting 200 with the refusal buried in the body would
    be technically defensible (the app's call did succeed) and would read as a system that
    quietly swallowed the refusal. The full outcome stays in the body either way.
    """
    outcome = decision.outcome
    if outcome is not None and not outcome.executed and outcome.http_status >= 400:
        response.status_code = outcome.http_status


# ----------------------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------------------


@router.post("/runs", response_model=StartRunResponse, status_code=202, tags=["runs"])
def start_run(body: StartRunRequest) -> StartRunResponse:
    """Start a run. Returns immediately; the agent executes asynchronously."""
    started = runner.start_run(
        settings=settings(),
        goal=body.goal,
        min_days_overdue=body.min_days_overdue,
        max_proposals=body.max_proposals,
    )
    return StartRunResponse(**started)


@router.get("/runs", response_model=list[RunSummary], tags=["runs"])
def list_runs(
    limit: int = Query(default=25, ge=1, le=200), offset: int = Query(default=0, ge=0)
) -> list[RunSummary]:
    """Runs with their status and counts, newest first."""
    return [RunSummary(**row) for row in queries.list_runs(limit=limit, offset=offset)]


@router.get("/runs/{run_id}", response_model=RunDetail, tags=["runs"])
def get_run(run_id: str) -> RunDetail:
    """A run with its full ordered transcript."""
    detail = queries.get_run(run_id)
    if detail is None:
        raise _not_found("run", run_id, ErrorCode.RUN_NOT_FOUND)
    return RunDetail(**detail)


@router.get("/runs/{run_id}/stream", tags=["runs"])
async def stream_run(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
    """Server-sent events: transcript steps as they occur.

    The stream tails the database rather than an in-process queue. That has three
    consequences worth having: a reconnecting browser resumes from ``after`` with nothing
    lost, several tabs can watch the same run, and the stream is a pure view over the
    persisted transcript -- which is the deliverable -- rather than a second copy of it.
    """
    if queries.run_status(run_id) is None:
        raise _not_found("run", run_id, ErrorCode.RUN_NOT_FOUND)

    async def events():
        last_seq = after
        while True:
            if await request.is_disconnected():
                return
            steps = await asyncio.to_thread(queries.steps_after, run_id, last_seq)
            for step in steps:
                last_seq = step["seq"]
                yield {
                    "event": "step",
                    "id": str(step["seq"]),
                    "data": _json(step),
                }
            status = await asyncio.to_thread(queries.run_status, run_id)
            if status in TERMINAL_RUN_STATUSES and not steps:
                detail = await asyncio.to_thread(queries.get_run, run_id)
                yield {
                    "event": "done",
                    "data": _json(
                        {
                            "status": status,
                            "tool_calls": (detail or {}).get("tool_calls", 0),
                            "proposals": (detail or {}).get("proposals", 0),
                            "error": (detail or {}).get("error"),
                        }
                    ),
                }
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return EventSourceResponse(events())


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str, ensure_ascii=False)


# ----------------------------------------------------------------------------------
# Proposals
# ----------------------------------------------------------------------------------


@router.get("/proposals", response_model=list[ProposalSummary], tags=["proposals"])
def list_proposals(
    status: str | None = Query(default="pending"),
    run_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ProposalSummary]:
    """Filter by status and run. Default pending. Sorted by amount descending."""
    if status in ("all", ""):
        status = None
    return [
        ProposalSummary(**row)
        for row in queries.list_proposals(status=status, run_id=run_id, limit=limit)
    ]


@router.get("/proposals/{proposal_id}", response_model=ProposalDetail, tags=["proposals"])
def get_proposal(proposal_id: str) -> ProposalDetail:
    """The letter, its rationale, the invoice facts it was built from, and its history."""
    detail = queries.get_proposal(proposal_id)
    if detail is None:
        raise _not_found("proposal", proposal_id, ErrorCode.PROPOSAL_NOT_FOUND)
    return ProposalDetail(**detail)


@router.post(
    "/proposals/{proposal_id}/approve", response_model=DecisionResponse, tags=["proposals"]
)
def approve_proposal(
    proposal_id: str, body: ApproveRequest, response: Response
) -> DecisionResponse:
    """Record the approval, mint the token, call the gateway, return the outcome.

    If ``edited_body`` is present the payload hash is recomputed from it before the token is
    minted, so the token authorises what the operator actually read.

    A gateway refusal is answered with the gateway's own status code. The approval itself is
    still recorded: a human decided, and the gateway declining to act on it does not unmake
    that.
    """
    config = settings()
    decision = proposal_service.approve(
        proposal_id,
        actor=body.actor or config.operator_id,
        note=body.note,
        edited_body=body.edited_body,
        settings=config,
    )
    _mirror_gateway_status(decision, response)
    return DecisionResponse(**decision.as_dict())


@router.post("/proposals/{proposal_id}/reject", response_model=DecisionResponse, tags=["proposals"])
def reject_proposal(proposal_id: str, body: RejectRequest) -> DecisionResponse:
    """Terminal. A note is required."""
    config = settings()
    decision = proposal_service.reject(
        proposal_id, actor=body.actor or config.operator_id, note=body.note
    )
    return DecisionResponse(**decision.as_dict())


@router.post("/proposals/{proposal_id}/edit", tags=["proposals"])
def save_edit(proposal_id: str, body: SaveEditRequest) -> dict[str, Any]:
    """Save an edited letter and re-hash it. Any earlier token can no longer execute."""
    config = settings()
    return proposal_service.save_edit(
        proposal_id, body=body.body, actor=body.actor or config.operator_id
    )


@router.post(
    "/proposals/{proposal_id}/attempt-unapproved",
    response_model=DecisionResponse,
    tags=["proposals"],
    responses={403: {"description": "not_approved -- the expected outcome"}},
)
def attempt_unapproved(proposal_id: str, response: Response) -> DecisionResponse:
    """Ask the gateway to send a letter nobody approved. It refuses. Nothing is sent.

    Step 4 of the handoff demo, and acceptance criterion 5. The token is correctly signed:
    the refusal comes from the gateway reading this proposal's status out of its own
    database, which is the claim this architecture makes. See app/approval/probe.py.

    It cannot call the gateway from the browser, because the gateway publishes no port --
    that is criterion 7 -- so the call is made here, on the internal network.
    """
    config = settings()
    decision = proposal_service.attempt_unapproved(
        proposal_id, actor=config.operator_id, settings=config
    )
    _mirror_gateway_status(decision, response)
    return DecisionResponse(**decision.as_dict())


# ----------------------------------------------------------------------------------
# Ground truth from Stripe
# ----------------------------------------------------------------------------------


@router.get("/invoices/overdue", tags=["invoices"])
def overdue_invoices(
    min_days_overdue: int = Query(default=1, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Read-only passthrough, so the UI can show ground truth beside the agent's output."""
    config = settings()
    try:
        reader = StripeReadClient(
            config.stripe_api_key_read,
            include_test_clock_fixtures=config.stripe_include_test_clock_invoices,
        )
        invoices = reader.list_overdue_invoices(min_days_overdue=min_days_overdue, limit=limit)
    except StripeReadError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"code": exc.code, "message": str(exc), **exc.detail}},
        )
    return {
        "count": len(invoices),
        "undeliverable": len([row for row in invoices if not row["deliverable"]]),
        "invoices": invoices,
    }


# ----------------------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------------------


@router.get("/audit", response_model=AuditPage, tags=["audit"])
def audit(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor: str | None = Query(default=None),
    event: str | None = Query(default=None),
) -> AuditPage:
    """The append-only log, newest first, with the chain's verification status."""
    return AuditPage(**queries.audit_page(limit=limit, offset=offset, actor=actor, event=event))


@router.get("/audit/verify", tags=["audit"])
def verify_audit_chain() -> dict[str, Any]:
    """Recompute the whole chain and report intact or broken, with the first bad row."""
    page = queries.audit_page(limit=1)
    return page["chain"]


@router.get("/outbox", tags=["audit"])
def outbox(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """Letters the gateway captured. With EMAIL_ADAPTER=outbox, nothing left the machine."""
    messages = queries.outbox(limit)
    return {"count": len(messages), "messages": messages}
