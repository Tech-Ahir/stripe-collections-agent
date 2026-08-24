"""The email adapter interface (brief section 8).

    send(to, subject, body, meta) -> DeliveryResult

Three implementations, selected by ``EMAIL_ADAPTER``. The default is ``outbox``, so a
reviewer who clones this repository cannot email a real person by accident.

This package lives in the gateway and nothing under ``app/`` may import it. That is
contract 2 in ``.importlinter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What the adapter did, in a shape the audit log and the UI can both use."""

    adapter: str
    accepted: bool
    provider_message_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "accepted": self.accepted,
            "provider_message_id": self.provider_message_id,
            **self.detail,
        }


class DeliveryError(RuntimeError):
    """The provider refused or was unreachable. The execution is recorded as failed."""

    def __init__(self, message: str, *, adapter: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.adapter = adapter
        self.detail = detail or {}


@runtime_checkable
class EmailAdapter(Protocol):
    name: str

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        meta: dict[str, Any],
    ) -> DeliveryResult: ...
