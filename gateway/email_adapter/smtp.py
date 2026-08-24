"""Standard SMTP (brief section 8).

Point at Mailpit in compose for a realistic send with a real inbox and no external
delivery::

    docker compose --profile smtp up -d
    EMAIL_ADAPTER=smtp        # then restart the gateway

`smtplib` is imported here and nowhere else. Contract 7 in `.importlinter` forbids `app`
and `shared` from importing it at all.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from gateway.email_adapter.base import DeliveryError, DeliveryResult


class SmtpAdapter:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        starttls: bool = False,
        mail_from: str = "collections@servicia.ai",
        from_name: str = "Servicia Collections",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.starttls = starttls
        self.mail_from = mail_from
        self.from_name = from_name
        self.timeout = timeout

    def send(self, *, to: str, subject: str, body: str, meta: dict[str, Any]) -> DeliveryResult:
        message = EmailMessage()
        message["From"] = f"{self.from_name} <{self.mail_from}>"
        message["To"] = to
        message["Subject"] = subject
        if meta.get("proposal_id"):
            message["X-Proposal-Id"] = str(meta["proposal_id"])
        message.set_content(body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                if self.starttls:
                    server.starttls()
                if self.username:
                    server.login(self.username, self.password)
                refused = server.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise DeliveryError(
                f"SMTP delivery failed: {exc}",
                adapter=self.name,
                detail={"host": self.host, "port": self.port, "error": type(exc).__name__},
            ) from exc

        if refused:
            raise DeliveryError(
                f"SMTP server refused the recipient: {refused}",
                adapter=self.name,
                detail={"refused": {k: str(v) for k, v in refused.items()}},
            )

        return DeliveryResult(
            adapter=self.name,
            accepted=True,
            provider_message_id=message.get("Message-Id"),
            detail={"host": self.host, "port": self.port},
        )
