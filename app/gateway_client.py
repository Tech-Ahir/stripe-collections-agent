"""The app's side of the only crossing point (brief sections 2 and 5).

One function, one URL, one method. The app can ask the gateway to execute an approved
action and can do nothing else to it -- there is no other endpoint on the gateway that acts.

``.importlinter`` contract 6 forbids ``app.agent`` from importing this module, so the agent
subsystem cannot reach the gateway even indirectly. Only the approval path can.

Refusals are returned, not raised, and their codes are passed through untouched. Section 6:
"Gateway refusal codes surface to the UI unchanged. When a refusal happens the UI must show
the code, because the refusal is the feature."
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("app.gateway")

EXECUTE_PATH = "/internal/actions/execute"


@dataclass(slots=True)
class GatewayOutcome:
    """What the gateway said, whether it acted or refused."""

    http_status: int
    body: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    transport_error: str | None = None

    @property
    def executed(self) -> bool:
        return self.http_status == 200 and not self.transport_error

    @property
    def error_code(self) -> str | None:
        if self.transport_error:
            return "gateway_unreachable"
        if self.executed:
            return None
        return (self.body.get("error") or {}).get("code")

    @property
    def error_message(self) -> str | None:
        if self.transport_error:
            return self.transport_error
        return (self.body.get("error") or {}).get("message")

    @property
    def failed_check(self) -> int | None:
        return (self.body.get("error") or {}).get("failed_check")

    @property
    def checks_passed(self) -> list[str]:
        if self.executed:
            return [check["name"] for check in self.body.get("checks", [])]
        return list((self.body.get("error") or {}).get("checks_passed") or [])

    @property
    def checks(self) -> list[dict[str, Any]]:
        """The seven checks as evaluated, for the UI to show verbatim on approval."""
        return list(self.body.get("checks") or [])

    def as_dict(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "http_status": self.http_status,
            "idempotency_key": self.idempotency_key,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "failed_check": self.failed_check,
            "checks_passed": self.checks_passed,
            "checks": self.checks,
            "gateway_response": self.body,
        }


class GatewayClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def execute(
        self,
        *,
        token: str,
        proposal_id: str,
        payload_hash: str,
        idempotency_key: str | None = None,
    ) -> GatewayOutcome:
        key = idempotency_key or str(uuid.uuid4())
        try:
            response = httpx.post(
                self.base_url + EXECUTE_PATH,
                json={"proposal_id": proposal_id, "payload_hash": payload_hash},
                headers={"X-Approval-Token": token, "X-Idempotency-Key": key},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            # The gateway is on the internal network only, so this is a real deployment
            # fault rather than a refusal. Say which.
            log.error("gateway unreachable at %s: %s", self.base_url, exc)
            return GatewayOutcome(
                http_status=0,
                idempotency_key=key,
                transport_error=(
                    f"The action gateway at {self.base_url} could not be reached "
                    f"({type(exc).__name__}). Nothing was sent."
                ),
            )

        try:
            body = response.json()
        except ValueError:
            body = {"error": {"code": "unreadable_response", "message": response.text[:500]}}

        outcome = GatewayOutcome(http_status=response.status_code, body=body, idempotency_key=key)
        if outcome.executed:
            log.info("gateway executed proposal %s", proposal_id)
        else:
            log.info(
                "gateway refused proposal %s: %s (check %s)",
                proposal_id,
                outcome.error_code,
                outcome.failed_check,
            )
        return outcome
