"""The agent loop (brief section 4).

    1. Operator starts a run with a goal, either the default or free text.
    2. System prompt establishes role, constraints, and the rule that it cannot send.
    3. Agent calls read tools to gather facts. It decides which and how many.
    4. For each invoice it judges worth pursuing, it drafts a letter and calls
       propose_collection_letter.
    5. Every tool call and result is persisted as a run_step and streamed to the UI.
    6. Agent produces a closing summary. Run status becomes awaiting_approval.
    7. The run is over. Approval happens on the operator's schedule, not inside the loop.

This is a genuine tool-use loop, written out rather than delegated to the SDK's tool runner,
and that is a deliberate choice. Three things the runner hides are exactly what this file
must expose: every tool call persisted as a numbered transcript row *as it happens*; the
guardrails of section 4 enforced mid-loop with a clean partial transcript when one trips;
and a control flow a reviewer can read top to bottom. The loop is a deliverable, not
plumbing.

Nothing here prescribes an order. The model is given a goal and a toolset and decides what
to call. Section 1: "That variability is the proof."
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.llm import LLMClient, LLMResponse, LLMUnavailable
from app.agent.prompts import build_system_prompt
from app.agent.tools.base import ToolClass, ToolFailure
from app.agent.tools.registry import ToolRegistry, build_registry
from app.config import AppSettings
from app.store.repositories import RunStore
from app.stripe_client.read import StripeReadClient

log = logging.getLogger("app.agent.loop")

#: A tool result larger than this is truncated before it goes back to the model. A single
#: enormous result would otherwise crowd out the rest of the run's context.
MAX_RESULT_CHARS = 20000


@dataclass(slots=True)
class RunOutcome:
    run_id: str
    status: str
    tool_calls: int
    proposals: int
    turns: int
    summary: str = ""
    error: str | None = None
    steps: list[int] = field(default_factory=list)


class ToolCallBudgetExceeded(RuntimeError):
    """Section 4: "Maximum 25 tool calls per run. Exceeding it fails the run cleanly.""" ""


def _serialise(result: Any) -> str:
    try:
        rendered = json.dumps(result, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(result)
    if len(rendered) > MAX_RESULT_CHARS:
        return rendered[:MAX_RESULT_CHARS] + f"... [truncated at {MAX_RESULT_CHARS} chars]"
    return rendered


def run_agent(
    *,
    run_id: str,
    goal: str,
    llm: LLMClient,
    registry: ToolRegistry,
    store: RunStore,
    settings: AppSettings,
    max_turns: int = 40,
) -> RunOutcome:
    """Drive one run to completion. Returns what happened; raises nothing on a tool error."""
    system = build_system_prompt(
        operator_id=settings.operator_id,
        max_proposals=settings.max_proposals_per_run,
        max_tool_calls=settings.max_tool_calls_per_run,
    )
    schemas = registry.anthropic_schemas()
    messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]

    store.set_status("running")
    tool_calls = 0
    turns = 0
    summary_parts: list[str] = []
    steps: list[int] = []

    try:
        while True:
            if turns >= max_turns:
                raise ToolCallBudgetExceeded(
                    f"the run exceeded {max_turns} model turns without finishing"
                )
            turns += 1

            response: LLMResponse = llm.create(system=system, messages=messages, tools=schemas)

            # --- the model's reasoning and prose, persisted before anything is executed
            for thought in response.thinking:
                steps.append(store.append_step(type="thought", payload={"text": thought}))
            for text in response.text:
                steps.append(store.append_step(type="message", payload={"text": text}))
                summary_parts.append(text)

            if not response.wants_tools:
                # Section 4 step 6: the agent has produced its closing summary.
                break

            # The assistant turn goes back verbatim, thinking blocks included.
            messages.append({"role": "assistant", "content": response.assistant_content})

            results: list[dict[str, Any]] = []
            for call in response.tool_calls:
                if tool_calls >= settings.max_tool_calls_per_run:
                    raise ToolCallBudgetExceeded(
                        f"the run reached its limit of {settings.max_tool_calls_per_run} tool calls"
                    )
                tool_calls += 1
                results.append(_execute_one(call, registry=registry, store=store, steps=steps))

            # All results for one assistant turn go back in a SINGLE user message.
            # Splitting them teaches the model to stop making parallel calls.
            messages.append({"role": "user", "content": results})

    except ToolCallBudgetExceeded as exc:
        # A clean failure with the partial transcript intact (section 4).
        message = str(exc)
        log.warning("run %s failed: %s", run_id, message)
        steps.append(
            store.append_step(
                type="message",
                payload={"text": f"Run stopped: {message}.", "kind": "guardrail"},
            )
        )
        store.set_status("failed", error=message)
        return RunOutcome(
            run_id=run_id,
            status="failed",
            tool_calls=tool_calls,
            proposals=store.proposal_count(),
            turns=turns,
            error=message,
            steps=steps,
        )
    except LLMUnavailable as exc:
        message = str(exc)
        log.warning("run %s could not reach the model: %s", run_id, message)
        store.set_status("failed", error=message)
        return RunOutcome(
            run_id=run_id,
            status="failed",
            tool_calls=tool_calls,
            proposals=store.proposal_count(),
            turns=turns,
            error=message,
            steps=steps,
        )
    except Exception as exc:  # noqa: BLE001 - a run must never leave a dangling status
        message = f"{type(exc).__name__}: {exc}"
        log.exception("run %s failed unexpectedly", run_id)
        store.set_status("failed", error=message)
        return RunOutcome(
            run_id=run_id,
            status="failed",
            tool_calls=tool_calls,
            proposals=store.proposal_count(),
            turns=turns,
            error=message,
            steps=steps,
        )

    proposals = store.proposal_count()
    # Section 4 step 6: awaiting_approval when there is something to approve. A run that
    # concluded no letter was warranted is completed, not left waiting on an empty queue.
    status = "awaiting_approval" if proposals else "completed"
    store.set_status(status)
    return RunOutcome(
        run_id=run_id,
        status=status,
        tool_calls=tool_calls,
        proposals=proposals,
        turns=turns,
        summary="\n\n".join(summary_parts).strip(),
        steps=steps,
    )


def _execute_one(call, *, registry: ToolRegistry, store: RunStore, steps: list[int]) -> dict:
    """Persist the call, run it, persist the result, and return the tool_result block."""
    tool_class = registry.tool_class(call.name)
    steps.append(
        store.append_step(
            type="tool_call",
            tool_name=call.name,
            payload={
                "tool_use_id": call.id,
                "arguments": call.arguments,
                # The UI renders this as a READ or DRAFT chip. There is no ACTION chip,
                # and its absence is the point.
                "tool_class": str(tool_class) if tool_class else "UNKNOWN",
            },
        )
    )

    try:
        result = registry.execute(call.name, call.arguments)
    except ToolFailure as failure:
        steps.append(
            store.append_step(
                type="tool_result",
                tool_name=call.name,
                payload={
                    "tool_use_id": call.id,
                    "is_error": True,
                    "result": failure.payload,
                    "tool_class": str(tool_class) if tool_class else "UNKNOWN",
                },
            )
        )
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": _serialise(failure.payload),
            "is_error": True,
        }

    steps.append(
        store.append_step(
            type="tool_result",
            tool_name=call.name,
            payload={
                "tool_use_id": call.id,
                "is_error": False,
                "result": result,
                "tool_class": str(tool_class) if tool_class else "UNKNOWN",
            },
        )
    )
    if tool_class is ToolClass.DRAFT and isinstance(result, dict) and result.get("proposal_id"):
        log.info("run created proposal %s", result["proposal_id"])
    return {
        "type": "tool_result",
        "tool_use_id": call.id,
        "content": _serialise(result),
    }


def build_run_context(
    *,
    run_id: str,
    settings: AppSettings,
    reader: StripeReadClient | None = None,
) -> tuple[RunStore, ToolRegistry]:
    """One store and one toolset per run, sharing a single Stripe customer cache."""
    store = RunStore(run_id)
    client = reader or StripeReadClient(
        settings.stripe_api_key_read,
        include_test_clock_fixtures=settings.stripe_include_test_clock_invoices,
    )
    registry = build_registry(
        reader=client,
        store=store,
        max_proposals=settings.max_proposals_per_run,
        ttl_hours=settings.proposal_ttl_hours,
    )
    return store, registry
