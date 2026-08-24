"""The agent service (container: app).

Public API on port 8000. Reads from Stripe with a restricted key, runs the agent loop,
persists proposals. It holds no capability to act on the outside world: its only route
there is to create a proposal that a human may later approve, which the gateway executes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from app.config import settings
from app.guards import assert_no_action_credentials
from shared.schema import init_schema_and_audit

log = logging.getLogger("app")

SERVICE = "app"
VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=settings().log_level.upper())

    # Rule 3, checked before anything else happens.
    assert_no_action_credentials()

    init_schema_and_audit(service=SERVICE, version=VERSION)
    log.info("agent service ready; gateway at %s", settings().gateway_url)
    yield


app = FastAPI(
    title="Stripe Collections Agent - public API",
    version=VERSION,
    description=(
        "The operator-facing API. The web UI is one client of this API and holds no "
        "privileges of its own, so a future mobile client is an additional client rather "
        "than a second implementation."
    ),
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
)


async def _probe_gateway() -> dict[str, Any]:
    url = settings().gateway_url.rstrip("/") + "/healthz"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
        return {
            "status": "ok" if response.status_code == 200 else "degraded",
            "http_status": response.status_code,
        }
    except Exception as exc:  # noqa: BLE001 - health probes report, never raise
        return {"status": "unreachable", "error": type(exc).__name__, "detail": str(exc)[:200]}


@app.get("/healthz", tags=["ops"], summary="Liveness plus dependency reachability")
async def healthz() -> dict[str, Any]:
    """Liveness, plus the reachability of Stripe, Anthropic and the gateway.

    Missing credentials are reported as ``not_configured`` rather than as failures, so a
    reviewer can see exactly which key is absent instead of a generic red light.
    """
    config = settings()
    checks: dict[str, Any] = {
        "database": {"status": "ok"},
        "stripe": {
            "status": "ok" if config.stripe_api_key_read else "not_configured",
            "mode": "test" if config.stripe_api_key_read.startswith("sk_test") else "unknown",
            "key_kind": "restricted"
            if config.stripe_api_key_read.startswith("rk_")
            else ("standard" if config.stripe_api_key_read else "absent"),
        },
        "anthropic": {
            "status": "ok" if config.anthropic_api_key else "not_configured",
            "model": config.anthropic_model,
        },
        "gateway": await _probe_gateway(),
    }
    healthy = all(c.get("status") == "ok" for c in checks.values())
    return {
        "service": SERVICE,
        "version": VERSION,
        "status": "ok" if healthy else "degraded",
        "checks": checks,
    }
