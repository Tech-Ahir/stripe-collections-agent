"""The action gateway (container: gateway).

The only component that can act on the outside world, and the only one holding the
credentials to do so. It publishes no port: nothing outside the internal Docker network
can reach it, which is the architectural boundary made physical.

It trusts nothing in a request body except what it can verify -- the HMAC signature, its
own database record of the proposal, and the idempotency key.

There is exactly one route that can act: POST /internal/actions/execute, in gateway/api.py.
Read gateway/verify.py alongside it; between them they are the whole boundary.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from gateway.api import install_error_handling, router
from gateway.config import settings
from shared.schema import init_schema_and_audit

log = logging.getLogger("gateway")

SERVICE = "gateway"
VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    config = settings()
    logging.basicConfig(level=config.log_level.upper())
    init_schema_and_audit(service=SERVICE, version=VERSION)
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


app.include_router(router)
install_error_handling(app)


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
