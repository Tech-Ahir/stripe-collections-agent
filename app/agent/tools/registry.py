"""The agent's toolset, assembled per run (brief section 4).

The registry is what the model is given and the only thing it can act through. It exposes
exactly five tools -- four READ and one DRAFT -- and there is no code path by which a sixth
appears, no flag that enables one, and no ACTION tool anywhere in the service.

It also does the work that makes "facts are injected by the tool layer, never recalled by
the model" true. As the agent reads invoices, the projected facts are captured here, keyed
by invoice id. When the agent later drafts a letter, the figures in the stored payload come
from this capture -- not from anything the model wrote -- and the guardrails check the
letter's text against it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.tools.base import ToolClass, ToolFailure, ToolSpec
from app.agent.tools.draft_tools import build_draft_tool
from app.agent.tools.read_tools import build_read_tools
from app.store.repositories import RunStore
from app.stripe_client.read import StripeReadClient

log = logging.getLogger("app.agent.tools")


class UnknownTool(ToolFailure):
    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(
            {
                "error": "unknown_tool",
                "message": (
                    f"There is no tool called {name!r}. Available tools: {', '.join(available)}."
                ),
                "recoverable": True,
            }
        )


def letter_facts(projected: dict[str, Any]) -> dict[str, Any]:
    """The canonical fact set for one invoice.

    The read layer's field names follow Stripe (``number``, ``amount_due_display``); the
    letter payload and the guardrails use letter-shaped names. Translating once, here,
    keeps both sides honest and gives exactly one definition of "the facts for this
    invoice".
    """
    return {
        "invoice_id": projected.get("id"),
        "invoice_number": projected.get("number"),
        "customer_id": projected.get("customer_id"),
        "customer_name": projected.get("customer_name"),
        "customer_email": projected.get("customer_email"),
        "amount_due": projected.get("amount_due"),
        "amount_display": projected.get("amount_due_display"),
        "currency": projected.get("currency"),
        "due_date": projected.get("due_date"),
        "days_overdue": projected.get("days_overdue"),
        "hosted_invoice_url": projected.get("hosted_invoice_url"),
        "deliverable": projected.get("deliverable", False),
        "not_deliverable_reason": projected.get("not_deliverable_reason"),
    }


def _looks_like_an_invoice(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and "amount_due" in value
        and "days_overdue" in value
    )


class ToolRegistry:
    """The five tools, plus the facts gathered while using them."""

    def __init__(self, tools: list[ToolSpec]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self.facts_by_invoice: dict[str, dict[str, Any]] = {}

    # -- what the model is given ----------------------------------------------------

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def anthropic_schemas(self) -> list[dict[str, Any]]:
        """Exactly what goes in the `tools` parameter of the Messages API request."""
        return [tool.to_anthropic() for tool in self._tools.values()]

    def classification(self) -> dict[str, str]:
        """name -> READ | DRAFT. The UI renders these as chips; there is no ACTION chip."""
        return {name: str(tool.tool_class) for name, tool in self._tools.items()}

    def tool_class(self, name: str) -> ToolClass | None:
        tool = self._tools.get(name)
        return tool.tool_class if tool else None

    # -- execution ------------------------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownTool(name, self.names)
        try:
            result = tool.handler(**(arguments or {}))
        except ToolFailure:
            raise
        except TypeError as exc:
            # A schema/handler mismatch. Correctable by the model, and loud in the log.
            log.warning("bad arguments for %s: %s", name, exc)
            raise ToolFailure(
                {
                    "error": "bad_arguments",
                    "message": f"{name} rejected those arguments: {exc}",
                    "recoverable": True,
                }
            ) from exc
        if tool.tool_class is ToolClass.READ:
            self._harvest(result)
        return result

    def _harvest(self, result: Any) -> None:
        """Capture the facts of every invoice the agent has actually looked at."""
        for candidate in self._walk(result):
            facts = letter_facts(candidate)
            if facts["invoice_id"]:
                self.facts_by_invoice[facts["invoice_id"]] = facts

    def _walk(self, value: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 4:
            return []
        found: list[dict[str, Any]] = []
        if _looks_like_an_invoice(value):
            found.append(value)
        if isinstance(value, dict):
            for nested in value.values():
                found.extend(self._walk(nested, depth + 1))
        elif isinstance(value, list):
            for nested in value:
                found.extend(self._walk(nested, depth + 1))
        return found


def build_registry(
    *,
    reader: StripeReadClient,
    store: RunStore,
    max_proposals: int,
    ttl_hours: int,
    min_days_overdue_floor: int = 1,
) -> ToolRegistry:
    """Assemble one run's toolset. Four READ tools and one DRAFT tool. Nothing else."""
    registry = ToolRegistry(build_read_tools(reader, min_days_overdue_floor=min_days_overdue_floor))
    registry._tools["propose_collection_letter"] = build_draft_tool(
        store=store,
        facts_by_invoice=registry.facts_by_invoice,
        max_proposals=max_proposals,
        ttl_hours=ttl_hours,
    )
    return registry
