"""Real delivery via Resend (brief section 8).

Documented, off by default, and requires an explicit key. Selecting this adapter is the
only way for this system to reach a real inbox, which is why it takes a deliberate act of
configuration to enable and why `EMAIL_ADAPTER=outbox` is the default everywhere.

Uses httpx directly rather than the `resend` SDK: one POST, one dependency fewer, and the
request shape stays visible to a reviewer.
"""

from __future__ import annotations

from typing import Any

import httpx

from gateway.email_adapter.base import DeliveryError, DeliveryResult

ENDPOINT = "https://api.resend.com/emails"


class ResendAdapter:
    name = "resend"

    def __init__(
        self,
        *,
        api_key: str,
        mail_from: str,
        from_name: str = "Servicia Collections",
        timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError(
                "EMAIL_ADAPTER=resend requires RESEND_API_KEY. Refusing to start with a "
                "real-delivery adapter and no credential."
            )
        self.api_key = api_key
        self.mail_from = mail_from
        self.from_name = from_name
        self.timeout = timeout

    def send(self, *, to: str, subject: str, body: str, meta: dict[str, Any]) -> DeliveryResult:
        payload = {
            "from": f"{self.from_name} <{self.mail_from}>",
            "to": [to],
            "subject": subject,
            "text": body,
        }
        try:
            response = httpx.post(
                ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(
                f"Resend was unreachable: {exc}",
                adapter=self.name,
                detail={"error": type(exc).__name__},
            ) from exc

        if response.status_code >= 400:
            raise DeliveryError(
                f"Resend refused the message with HTTP {response.status_code}.",
                adapter=self.name,
                detail={"http_status": response.status_code, "response": response.text[:500]},
            )

        data = response.json() if response.content else {}
        return DeliveryResult(
            adapter=self.name,
            accepted=True,
            provider_message_id=data.get("id"),
            detail={"http_status": response.status_code},
        )
