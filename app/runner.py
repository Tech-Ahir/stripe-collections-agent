"""Starting and tracking agent runs (brief section 6).

``POST /v1/runs`` returns a ``run_id`` immediately and the run executes asynchronously. The
work happens on a small thread pool rather than in the event loop, for a plain reason: the
Stripe SDK and the Anthropic SDK used here are synchronous, and the transcript is written
through synchronous SQLAlchemy. A worker thread keeps all of that straightforward and keeps
the event loop free to serve the UI and the SSE stream while a run is in progress.

Nothing about a run's *result* lives in memory. Status, transcript and proposals are all in
the database, so a browser that reconnects, a second tab, or a restarted process all see the
same run. The thread pool is an execution detail, not state.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from app.agent.llm import AnthropicLLM, LLMClient, LLMUnavailable
from app.agent.loop import build_run_context, run_agent
from app.agent.prompts import DEFAULT_GOAL
from app.config import AppSettings
from app.store.repositories import RunStore

log = logging.getLogger("app.runner")

_pool: ThreadPoolExecutor | None = None
_in_flight: dict[str, Future] = {}


def pool(settings: AppSettings) -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(
            max_workers=max(1, settings.max_concurrent_runs), thread_name_prefix="run"
        )
    return _pool


def shutdown() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
    _in_flight.clear()


def active_run_ids() -> list[str]:
    return [run_id for run_id, future in _in_flight.items() if not future.done()]


def start_run(
    *,
    settings: AppSettings,
    goal: str | None = None,
    min_days_overdue: int = 1,
    max_proposals: int | None = None,
    operator_id: str | None = None,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Create the run row, hand the work to a worker, and return at once."""
    resolved_goal = (goal or "").strip() or DEFAULT_GOAL
    cap = max_proposals or settings.max_proposals_per_run
    operator = operator_id or settings.operator_id

    run_id = RunStore.create(
        goal=resolved_goal,
        operator_id=operator,
        params={
            "min_days_overdue": min_days_overdue,
            "max_proposals": cap,
            "max_tool_calls": settings.max_tool_calls_per_run,
            "model": settings.anthropic_model,
        },
    )

    future = pool(settings).submit(
        _execute,
        run_id=run_id,
        goal=resolved_goal,
        min_days_overdue=min_days_overdue,
        max_proposals=cap,
        settings=settings,
        llm=llm,
    )
    _in_flight[run_id] = future
    log.info("run %s queued: %s", run_id, resolved_goal)
    return {"run_id": run_id, "status": "queued", "goal": resolved_goal}


def _execute(
    *,
    run_id: str,
    goal: str,
    min_days_overdue: int,
    max_proposals: int,
    settings: AppSettings,
    llm: LLMClient | None,
) -> None:
    """The worker body. Never raises: a run always reaches a terminal status."""
    store = RunStore(run_id)
    try:
        model = llm
        if model is None:
            # A missing key is a configuration fact, not a crash. run_agent turns
            # LLMUnavailable into a failed run with a readable reason.
            model = AnthropicLLM(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    except LLMUnavailable as exc:
        log.warning("run %s cannot start: %s", run_id, exc)
        store.set_status("failed", error=str(exc))
        return

    try:
        store, registry = _context(run_id, settings)
        # The operator's floor for this run, applied by the tool's own default so the model
        # still chooses whether to widen it.
        registry.default_min_days_overdue = min_days_overdue  # type: ignore[attr-defined]
        outcome = run_agent(
            run_id=run_id,
            goal=(
                f"{goal}\n\nOnly consider invoices at least {min_days_overdue} day(s) "
                f"overdue. Propose at most {max_proposals} letters."
            ),
            llm=model,
            registry=registry,
            store=store,
            settings=settings,
        )
        log.info(
            "run %s finished: status=%s tool_calls=%d proposals=%d",
            run_id,
            outcome.status,
            outcome.tool_calls,
            outcome.proposals,
        )
    except Exception:  # noqa: BLE001 - the worker must never die silently
        log.exception("run %s crashed outside the loop", run_id)
        try:
            store.set_status("failed", error="the run crashed unexpectedly")
        except Exception:  # noqa: BLE001
            log.exception("could not even mark run %s failed", run_id)


def _context(run_id: str, settings: AppSettings):
    return build_run_context(run_id=run_id, settings=settings)
