"""The action gateway (container: gateway).

The only component that can act on the outside world, and the only one holding the
credentials to do so. It publishes no port: nothing outside the internal Docker network
can reach it, which is the architectural boundary made physical.

It trusts nothing in a request body except what it can verify -- the HMAC signature, its
own database record of the proposal, and the idempotency key. The execute endpoint and its
seven checks arrive in Phase 2; this module currently exposes liveness only, so that
nothing pretends to work before its refusal tests exist.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from gateway.config import settings
from shared import audit
from shared.db import init_db, run_in_transaction

log = logging.getLogger("gateway")

SERVICE = "gateway"
VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    config = settings()
    logging.basicConfig(level=config.log_level.upper())
    init_db()
    run_in_transaction(
        lambda session: audit.append(
            session,
            actor="system",
            event=audit.SYSTEM_STARTED,
            subject_type="service",
            subject_id=SERVICE,
            detail={
                "version": VERSION,
                "role": "action-gateway",
                "email_adapter": config.email_adapter,
                "stripe_invoice_send": config.enable_stripe_invoice_send,
            },
        )
    )
    log.info("action gateway ready; email adapter=%s", config.email_adapter)
    yield


app = FastAPI(
    title="Stripe Collections Agent - internal action gateway",
    version=VERSION,
    description=(
        "Internal only. Not reachable from the host: this service publishes no port. "
        "Executes an action solely against a signed approval it can verify."
    ),
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
)


@app.get("/healthz", tags=["ops"], summary="Liveness")
async def healthz() -> dict[str, Any]:
    config = settings()
    return {
        "service": SERVICE,
        "version": VERSION,
        "status": "ok",
        "checks": {
            "database": {"status": "ok"},
            "email_adapter": {"status": "ok", "adapter": config.email_adapter},
            "stripe_write": {
                "status": "ok" if config.stripe_api_key_write else "not_configured",
                "invoice_send_enabled": config.enable_stripe_invoice_send,
            },
        },
    }
