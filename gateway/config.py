"""Action-gateway settings.

This is the only class in the codebase that declares the write-capable Stripe key and the
email adapter configuration. The agent service's settings class has no such fields.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from shared.config import CommonSettings


class GatewaySettings(CommonSettings):
    #: Needed solely if ENABLE_STRIPE_INVOICE_SEND is on. Never present in the app.
    stripe_api_key_write: str = ""
    enable_stripe_invoice_send: bool = False

    #: outbox | smtp | resend. Default outbox, so cloning the repo cannot email anyone.
    email_adapter: Literal["outbox", "smtp", "resend"] = "outbox"
    outbox_dir: str = "/data/outbox"

    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False

    resend_api_key: str = ""

    #: From-address on outgoing letters, whichever adapter is selected.
    mail_from: str = "collections@servicia.ai"
    mail_from_name: str = "Servicia Collections"


@lru_cache(maxsize=1)
def settings() -> GatewaySettings:
    return GatewaySettings()
