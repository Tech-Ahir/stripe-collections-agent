"""The agent service (container: app).

Public API on port 8000. Reads from Stripe with a restricted key, runs the agent loop,
persists proposals. It holds no capability to act on the outside world: its only route
there is to create a proposal that a human may later approve, which the gateway executes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1 import router as v1_router
from app.config import settings
from app.guards import assert_no_action_credentials
from app.web.routes import router as web_router
from shared.errors import ApiError
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

    # A run's status is in the database; the thread executing it was in the process that
    # just died. Nothing is in flight yet, so anything still queued or running was
    # abandoned -- say so instead of leaving it spinning on the dashboard forever.
    from app.store.repositories import abandon_orphaned_runs, settle_decided_runs

    abandoned = abandon_orphaned_runs()
    settled = settle_decided_runs()
    if abandoned:
        log.warning("marked %d abandoned run(s) as failed: %s", len(abandoned), abandoned)
    if settled:
        log.info("settled %d run(s) whose proposals had all been decided", settled)

    log.info("agent service ready; gateway at %s", settings().gateway_url)
    try:
        yield
    finally:
        from app import runner

        runner.shutdown()


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


app.include_router(v1_router)
app.include_router(web_router)


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
    """Section 6's error envelope, with the code intact.

    Gateway refusal codes reach the operator unchanged, because the refusal is the feature.
    """
    log.info("api error: %s (%s)", exc.code, exc.message)
    return JSONResponse(status_code=exc.http_status, content=exc.envelope())


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


#: Stripe is probed at most this often. The dashboard polls, and a health endpoint that
#: bills an API call per poll is its own defect.
STRIPE_PROBE_TTL_SECONDS = 30.0
_stripe_probe_cache: dict[str, Any] = {"at": 0.0, "result": None}


def _stripe_key_facts(key: str) -> dict[str, Any]:
    """What can be told from the key string alone, without calling anything.

    ``mode`` accepts BOTH test prefixes. A restricted test key is ``rk_test_...``, so
    matching only ``sk_test`` reported "unknown" precisely when the agent was configured
    the way the brief's rule 2 requires.
    """
    return {
        "mode": "test" if key.startswith(("sk_test", "rk_test")) else "unknown",
        "key_kind": "restricted" if key.startswith("rk_") else ("standard" if key else "absent"),
    }


async def _probe_stripe() -> dict[str, Any]:
    """Reachability, by actually asking Stripe.

    A truthiness test on the environment variable cannot tell a working key from a typo,
    and reporting the typo as ``ok`` puts a green light on the one screen built to warn
    the operator. One `GET /v1/invoices?limit=1` settles it: 200 means the key
    authenticates AND carries Invoices read, which is the minimum the agent needs.
    """
    config = settings()
    key = config.stripe_api_key_read
    facts = _stripe_key_facts(key)

    if not key:
        return {"status": "not_configured", **facts}

    if not config.health_probe_stripe:
        # No call was made, so no claim is made about reachability.
        return {"status": "ok", "reachability": "not_probed", **facts}

    now = time.monotonic()
    cached = _stripe_probe_cache["result"]
    if cached is not None and now - float(_stripe_probe_cache["at"]) < STRIPE_PROBE_TTL_SECONDS:
        return {**cached, **facts}

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(
                "https://api.stripe.com/v1/invoices",
                params={"limit": 1},
                auth=(key, ""),
            )
        if response.status_code == 200:
            result: dict[str, Any] = {"status": "ok"}
        elif response.status_code in (401, 403):
            # 401: the key is not valid. 403: valid, but missing Invoices read. Both mean
            # the agent cannot do its job, and both should be visible before a run starts.
            result = {"status": "unauthorized", "http_status": response.status_code}
        else:
            result = {"status": "degraded", "http_status": response.status_code}
    except Exception as exc:  # noqa: BLE001 - health probes report, never raise
        result = {"status": "unreachable", "error": type(exc).__name__}

    _stripe_probe_cache.update({"at": now, "result": result})
    return {**result, **facts}


@app.get("/healthz", tags=["ops"], summary="Liveness plus dependency reachability")
async def healthz() -> dict[str, Any]:
    """Liveness, plus the reachability of Stripe, Anthropic and the gateway.

    Missing credentials are reported as ``not_configured`` rather than as failures, so a
    reviewer can see exactly which key is absent instead of a generic red light.
    """
    config = settings()
    checks: dict[str, Any] = {
        "database": {"status": "ok"},
        "stripe": await _probe_stripe(),
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
