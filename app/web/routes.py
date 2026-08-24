"""The web test UI (brief section 9).

Four screens. Jinja and HTMX, no Node build, no frontend framework.

    1 Dashboard      /             connection status, counters, start a run, recent runs
    2 Run detail     /runs/{id}    live transcript over SSE, proposals as they appear
    3 Approval queue /proposals    review, edit, approve, reject, and try without approval
    4 Audit log      /audit        reverse-chronological, filterable, chain verification

"The UI's job is to make the approval architecture legible to someone evaluating it, so
surface the boundary in the interface rather than in the logs."

On the UI holding no privileges of its own: every read here goes through ``app.api.queries``
-- the same read models ``/v1`` publishes -- and every write goes through
``app.services.proposals``, the same functions ``/v1`` calls. The privilege boundary is the
service layer, not the transport, so these screens can do nothing a ``/v1`` client cannot.
That is what makes a future mobile client an additional client rather than a second
implementation.

The fragment endpoints under ``/ui`` exist because HTMX swaps HTML, not JSON. They are thin:
call a service, render a partial.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api import queries
from app.config import settings
from app.runner import start_run
from app.services import proposals as proposal_service
from shared.errors import ApiError

log = logging.getLogger("app.web")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)


def _base(request: Request, **extra: Any) -> dict[str, Any]:
    config = settings()
    context = {
        "request": request,
        "counters": queries.proposal_counters(),
        "operator": config.operator_id,
        "model": config.anthropic_model,
        "unapproved_demo": config.enable_unapproved_attempt_demo,
    }
    context.update(extra)
    return context


# ----------------------------------------------------------------------------------
# 1. Dashboard
# ----------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        _base(
            request,
            runs=queries.list_runs(limit=15),
            max_proposals=settings().max_proposals_per_run,
            # Section 9's fourth counter. None when Stripe could not be asked, which the
            # template renders as a dash rather than a misleading zero.
            overdue=queries.overdue_invoice_count(),
        ),
    )


@router.get("/ui/health", response_class=HTMLResponse)
async def health_fragment(request: Request) -> HTMLResponse:
    """Polled by the header. Stripe, Anthropic and the gateway, from /healthz."""
    from app.main import healthz

    return TEMPLATES.TemplateResponse(
        request, "partials/health.html", {"request": request, "health": await healthz()}
    )


@router.post("/ui/runs")
def start_run_form(
    goal: str = Form(default=""),
    min_days_overdue: int = Form(default=1),
    max_proposals: int = Form(default=0),
) -> RedirectResponse:
    started = start_run(
        settings=settings(),
        goal=goal,
        min_days_overdue=max(0, min_days_overdue),
        max_proposals=max_proposals or None,
    )
    return RedirectResponse(url=f"/runs/{started['run_id']}", status_code=303)


# ----------------------------------------------------------------------------------
# 2. Run detail
# ----------------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str) -> HTMLResponse:
    detail = queries.get_run(run_id)
    if detail is None:
        return TEMPLATES.TemplateResponse(
            request, "not_found.html", _base(request, what=f"run {run_id}"), status_code=404
        )
    proposals = queries.list_proposals(status=None, run_id=run_id)
    return TEMPLATES.TemplateResponse(
        request, "run_detail.html", _base(request, run=detail, proposals=proposals)
    )


@router.get("/ui/runs/{run_id}/proposals", response_class=HTMLResponse)
def run_proposals_fragment(request: Request, run_id: str) -> HTMLResponse:
    """Proposals appear inline as the run makes them (section 9)."""
    return TEMPLATES.TemplateResponse(
        request,
        "partials/run_proposals.html",
        {"request": request, "proposals": queries.list_proposals(status=None, run_id=run_id)},
    )


# ----------------------------------------------------------------------------------
# 3. Approval queue
# ----------------------------------------------------------------------------------


@router.get("/proposals", response_class=HTMLResponse)
def proposal_queue(request: Request, selected: str | None = None, status: str = "pending"):
    pending = queries.list_proposals(status=None if status == "all" else status)
    chosen = selected or (pending[0]["id"] if pending else None)
    detail = queries.get_proposal(chosen) if chosen else None
    return TEMPLATES.TemplateResponse(
        request,
        "proposals.html",
        _base(request, queue=pending, detail=detail, status_filter=status),
    )


def _decision_fragment(request: Request, proposal_id: str, result: dict[str, Any]) -> HTMLResponse:
    detail = queries.get_proposal(proposal_id)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/decision.html",
        {
            "request": request,
            "result": result,
            "detail": detail,
            "counters": queries.proposal_counters(),
        },
    )


@router.post("/ui/proposals/{proposal_id}/approve", response_class=HTMLResponse)
def ui_approve(
    request: Request,
    proposal_id: str,
    note: str = Form(default=""),
    body: str = Form(default=""),
) -> HTMLResponse:
    config = settings()
    try:
        decision = proposal_service.approve(
            proposal_id,
            actor=config.operator_id,
            note=note or None,
            edited_body=body or None,
            settings=config,
        )
        return _decision_fragment(request, proposal_id, decision.as_dict())
    except ApiError as exc:
        return _decision_fragment(request, proposal_id, exc.envelope())


@router.post("/ui/proposals/{proposal_id}/reject", response_class=HTMLResponse)
def ui_reject(request: Request, proposal_id: str, note: str = Form(default="")) -> HTMLResponse:
    config = settings()
    try:
        decision = proposal_service.reject(proposal_id, actor=config.operator_id, note=note)
        return _decision_fragment(request, proposal_id, decision.as_dict())
    except ApiError as exc:
        return _decision_fragment(request, proposal_id, exc.envelope())


@router.post("/ui/proposals/{proposal_id}/edit", response_class=HTMLResponse)
def ui_save_edit(request: Request, proposal_id: str, body: str = Form(...)) -> HTMLResponse:
    config = settings()
    try:
        result = proposal_service.save_edit(proposal_id, body=body, actor=config.operator_id)
        return _decision_fragment(request, proposal_id, {"edit": result})
    except ApiError as exc:
        return _decision_fragment(request, proposal_id, exc.envelope())


@router.post("/ui/proposals/{proposal_id}/attempt-unapproved", response_class=HTMLResponse)
def ui_attempt_unapproved(request: Request, proposal_id: str) -> HTMLResponse:
    """Step 4 of the handoff demo. Expect 403 not_approved, and nothing sent."""
    config = settings()
    try:
        decision = proposal_service.attempt_unapproved(
            proposal_id, actor=config.operator_id, settings=config
        )
        return _decision_fragment(request, proposal_id, decision.as_dict())
    except ApiError as exc:
        return _decision_fragment(request, proposal_id, exc.envelope())


# ----------------------------------------------------------------------------------
# 4. Audit log
# ----------------------------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit_log(
    request: Request,
    actor: str | None = None,
    event: str | None = None,
    offset: int = 0,
) -> HTMLResponse:
    page = queries.audit_page(limit=100, offset=max(0, offset), actor=actor, event=event)
    return TEMPLATES.TemplateResponse(
        request,
        "audit.html",
        _base(
            request,
            page=page,
            filters=queries.audit_filters(),
            actor=actor,
            event=event,
        ),
    )


@router.get("/ui/audit/verify", response_class=HTMLResponse)
def ui_verify_chain(request: Request) -> HTMLResponse:
    """Recompute the hash chain and report intact or broken, with the first bad row."""
    return TEMPLATES.TemplateResponse(
        request,
        "partials/chain.html",
        {"request": request, "chain": queries.audit_page(limit=1)["chain"], "verified": True},
    )


@router.get("/outbox", response_class=HTMLResponse)
def outbox_view(request: Request) -> HTMLResponse:
    """What the gateway captured. With the default adapter, nothing left this machine."""
    return TEMPLATES.TemplateResponse(
        request, "outbox.html", _base(request, messages=queries.outbox(50))
    )
