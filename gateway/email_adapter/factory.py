"""Adapter selection.

One place decides which adapter is live, and it fails loudly on an unknown value rather
than silently falling back -- a typo in `EMAIL_ADAPTER` must not quietly turn a real send
into a captured one, or the reverse.
"""

from __future__ import annotations

from gateway.config import GatewaySettings
from gateway.email_adapter.base import EmailAdapter
from gateway.email_adapter.outbox import OutboxAdapter
from gateway.email_adapter.resend import ResendAdapter
from gateway.email_adapter.smtp import SmtpAdapter

KNOWN = ("outbox", "smtp", "resend")


def build_adapter(settings: GatewaySettings) -> EmailAdapter:
    choice = (settings.email_adapter or "outbox").strip().lower()

    if choice == "outbox":
        return OutboxAdapter(settings.outbox_dir)
    if choice == "smtp":
        return SmtpAdapter(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            starttls=settings.smtp_starttls,
            mail_from=settings.mail_from,
            from_name=settings.mail_from_name,
        )
    if choice == "resend":
        return ResendAdapter(
            api_key=settings.resend_api_key,
            mail_from=settings.mail_from,
            from_name=settings.mail_from_name,
        )

    raise ValueError(f"unknown EMAIL_ADAPTER {choice!r}; expected one of {KNOWN}")
