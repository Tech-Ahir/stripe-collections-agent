"""The model client (brief section 3: "Anthropic Messages API, tool use, Claude Sonnet").

The loop talks to this interface rather than to the SDK directly, for one reason that
matters: the loop is the deliverable a reviewer reads, and it has to be testable without a
key and without a network. ``ScriptedLLM`` makes the loop's caps, its failure paths and its
transcript persistence all verifiable; ``AnthropicLLM`` is the real thing.

Two details of the real client that are easy to get wrong on Claude Sonnet 5:

* ``thinking`` takes ``{"type": "adaptive"}``. ``budget_tokens`` is rejected outright, and
  ``display`` defaults to ``"omitted"`` -- which would leave section 9's "reasoning as
  prose" transcript rows empty. ``display: "summarized"`` is set explicitly so the
  transcript shows the model's actual reasoning rather than narration invented around it.
* Thinking blocks must be echoed back **unchanged** in the assistant turn, so the loop
  appends ``response.content`` wholesale rather than reconstructing it.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("app.agent.llm")

#: Generous but bounded. A collections run is not a long-output task, and a non-streaming
#: request of this size stays comfortably inside the SDK's timeout.
MAX_TOKENS = 16000


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    """One assistant turn, in the shape the loop needs."""

    stop_reason: str | None
    #: Rendered thinking, when the model returned any. Persisted as `thought` steps.
    thinking: list[str] = field(default_factory=list)
    #: Visible prose. Persisted as `message` steps.
    text: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Echoed back verbatim as the assistant turn. Never reconstructed.
    assistant_content: Any = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse: ...


class LLMUnavailable(RuntimeError):
    """No model is configured, or the API refused. The run fails cleanly."""


# ----------------------------------------------------------------------------------
# The real client
# ----------------------------------------------------------------------------------


class AnthropicLLM:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int = MAX_TOKENS,
        timeout_seconds: float | None = None,
    ) -> None:
        if not api_key:
            raise LLMUnavailable(
                "ANTHROPIC_API_KEY is not configured, so the agent cannot run. Everything "
                "else in this system works without it: the boundary, the approval flow and "
                "the audit chain are all exercised by the test suite and by "
                "scripts/demo_boundary.py."
            )
        import anthropic

        # A ceiling on a single request. Without one, a wedged call holds a worker thread
        # indefinitely and its run sits at "running" until the process restarts -- which is
        # exactly the state the dashboard was found in.
        self._client = (
            anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds)
            if timeout_seconds
            else anthropic.Anthropic(api_key=api_key)
        )
        self._model = model
        self._max_tokens = max_tokens

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                # Adaptive thinking, with the summary made visible so the run transcript
                # shows real reasoning. Sonnet 5 rejects budget_tokens and defaults
                # display to "omitted".
                thinking={"type": "adaptive", "display": "summarized"},
            )
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"Anthropic returned HTTP {exc.status_code}: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMUnavailable(
                "Anthropic did not respond within this run's time budget "
                f"(RUN_TIMEOUT_SECONDS): {exc}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"Could not reach Anthropic: {exc}") from exc

        return self._normalise(response)

    @staticmethod
    def _normalise(response: Any) -> LLMResponse:
        thinking: list[str] = []
        text: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "thinking":
                rendered = (getattr(block, "thinking", "") or "").strip()
                if rendered:
                    thinking.append(rendered)
            elif kind == "text":
                rendered = (getattr(block, "text", "") or "").strip()
                if rendered:
                    text.append(rendered)
            elif kind == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        # Always parsed, never string-matched: escaping in tool inputs
                        # varies between models.
                        arguments=dict(block.input or {}),
                    )
                )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            stop_reason=getattr(response, "stop_reason", None),
            thinking=thinking,
            text=text,
            tool_calls=tool_calls,
            assistant_content=response.content,
            usage={
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }
            if usage
            else {},
        )


# ----------------------------------------------------------------------------------
# The scripted client, for tests
# ----------------------------------------------------------------------------------


class ScriptedLLM:
    """Replays a fixed list of turns. Test-only, and deliberately not selectable at runtime.

    There is no environment variable that swaps this in for the real client. A reviewer who
    runs the system either has an Anthropic key and sees a real agent, or has none and is
    told so plainly -- never a scripted transcript passed off as a model.
    """

    def __init__(self, turns: list[LLMResponse]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError(
                "the scripted model ran out of turns; the loop asked for more than the "
                "test provided"
            )
        turn = self._turns.pop(0)
        if turn.assistant_content is None:
            turn.assistant_content = _synthesise_content(turn)
        return turn

    @property
    def calls(self) -> int:
        return len(self.requests)


def _synthesise_content(turn: LLMResponse) -> list[dict[str, Any]]:
    """A content list shaped like the API's, so the loop's echo-back path is exercised."""
    content: list[dict[str, Any]] = []
    for thought in turn.thinking:
        content.append({"type": "thinking", "thinking": thought})
    for text in turn.text:
        content.append({"type": "text", "text": text})
    for call in turn.tool_calls:
        content.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return content


#: Scripted tool-call ids are minted from one counter for the whole process, not per turn.
#: Numbering them per turn gave every turn a `toolu_0`, which is wrong in the same way the
#: real API would never be: ids collided across turns, and code that keyed anything on them
#: silently matched the wrong call. Found via an empty payment-history panel.
_scripted_tool_call_seq = itertools.count(1)


def turn(
    *,
    thinking: list[str] | None = None,
    text: list[str] | None = None,
    tools: list[tuple[str, dict[str, Any]]] | None = None,
    stop_reason: str | None = None,
) -> LLMResponse:
    """Terse constructor for a scripted turn."""
    calls = [
        ToolCall(id=f"toolu_{next(_scripted_tool_call_seq):04d}", name=name, arguments=arguments)
        for name, arguments in (tools or [])
    ]
    return LLMResponse(
        stop_reason=stop_reason or ("tool_use" if calls else "end_turn"),
        thinking=list(thinking or []),
        text=list(text or []),
        tool_calls=calls,
    )
