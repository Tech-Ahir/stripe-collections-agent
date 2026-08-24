"""Tool classification (brief section 4).

    "Every tool is one of three kinds. The classification is declared in code and is the
    thing a reviewer checks."

    CLASS   EFFECT                          TOOLS
    READ    No external effect.             list_overdue_invoices, get_invoice,
            Auto-executes.                  get_customer, get_payment_history
    DRAFT   Writes internal records only.   propose_collection_letter
            Auto-executes.
    ACTION  Reaches the outside world.      send_collection_letter -- lives in the gateway,
            NOT EXPOSED TO THE AGENT.       invocable only by an approved proposal

**READ THIS TWICE.** ``send_collection_letter`` is not a tool the agent has and is refused.
It is absent from the agent's tool schema entirely. The agent's only route to the outside
world is to create a proposal that a human may later approve. Refusal at runtime is a
weaker design than absence at definition time, and the client is evaluating exactly this.

``ToolClass.ACTION`` exists here because section 4 defines three kinds and a reviewer should
find all three named. What matters is that **no tool in this service is ever registered with
it**: ``tests/test_agent_tools.py`` asserts that the registry contains no ACTION tool and
that the serialised schema handed to the model contains no send capability of any kind.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ToolClass(StrEnum):
    #: No external effect. Auto-executes.
    READ = "READ"
    #: Writes internal records only. Auto-executes.
    DRAFT = "DRAFT"
    #: Reaches the outside world. Never registered in this service -- see the module
    #: docstring. It is named so the classification is complete, not so it can be used.
    ACTION = "ACTION"


class ToolFailure(Exception):
    """A tool failed in a way the agent should see and may be able to work around.

    Raised rather than returned so a handler cannot forget to signal failure. The loop
    converts it into a ``tool_result`` with ``is_error: true``, which keeps the run alive.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message") or payload.get("error") or "tool failed")
        self.payload = payload


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    tool_class: ToolClass
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    def __post_init__(self) -> None:
        if self.tool_class is ToolClass.ACTION:
            raise ValueError(
                f"{self.name!r} was declared as an ACTION tool. The agent service has no "
                "ACTION tools by design: its only route to the outside world is a proposal "
                "a human approves, which the gateway executes. See CLAUDE.md rule 4."
            )

    def to_anthropic(self) -> dict[str, Any]:
        """The tool as the Messages API receives it.

        ``strict`` guarantees the input validates exactly against the schema, so handlers
        need no defensive parsing. It requires ``additionalProperties: false`` and every
        property listed in ``required`` -- which is why optional parameters are typed as
        nullable and documented as "null means use the default" rather than omitted.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


def nullable(json_type: str) -> list[str]:
    """A strict-mode-compatible optional parameter."""
    return [json_type, "null"]


def schema(properties: dict[str, Any]) -> dict[str, Any]:
    """An object schema valid under strict mode: closed, and fully required."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
