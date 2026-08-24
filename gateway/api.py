"""The internal action API -- the only crossing point (brief section 5).

    POST /internal/actions/execute
      X-Approval-Token: <base64 HMAC-SHA256 signed token>
      X-Idempotency-Key: <uuid>
      { "proposal_id": "...", "payload_hash": "sha256:..." }

One route, no others. There is no "resend", no "force", no "override" and no route that
takes an action without a token, because every such convenience would be a way to reach the
outside world without an approval.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from gateway import executor
from gateway.config import settings
from gateway.email_adapter.factory import build_adapter
from gateway.verify import CHECKS, verify_execution_request
from shared.db import new_session
from shared.errors import ApiError
from shared.schemas import ExecuteActionRequest, ExecuteActionResponse

log = logging.getLogger("gateway.api")

router = APIRouter()


@router.post(
    "/internal/actions/execute",
    response_model=ExecuteActionResponse,
    tags=["internal"],
    summary="Execute an approved action against a signed approval",
    responses={
        401: {"description": "invalid_signature | token_expired | malformed_token"},
        403: {"description": "not_approved | approval_mismatch"},
        409: {"description": "token_replayed | payload_modified"},
        502: {"description": "execution_failed"},
    },
)
def execute_action(
    body: ExecuteActionRequest,
    x_approval_token: str | None = Header(default=None, alias="X-Approval-Token"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> ExecuteActionResponse:
    config = settings()
    secret = config.require_signing_secret()

    session = new_session()
    try:
        verified = verify_execution_request(
            session,
            raw_token=x_approval_token,
            idempotency_key=x_idempotency_key,
            body_proposal_id=body.proposal_id,
            body_payload_hash=body.payload_hash,
            secret=secret,
        )
        # Detach: the external call must not happen with a session holding rows open.
        session.expunge_all()
    finally:
        session.close()

    if verified.body_disagreed_with_token:
        log.warning(
            "request body disagreed with the signed token for proposal %s; the token is "
            "authoritative and the body was ignored",
            verified.claims.proposal_id,
        )

    adapter = build_adapter(config)
    execution, result, replayed = executor.execute(
        verified,
        adapter=adapter,
        stripe_send=config.enable_stripe_invoice_send,
        stripe_sender=(
            executor.build_stripe_sender(config.stripe_api_key_write)
            if config.enable_stripe_invoice_send
            else None
        ),
    )

    return ExecuteActionResponse(
        status="already_executed" if replayed else "executed",
        proposal_id=verified.proposal.id,
        execution_id=execution.id,
        idempotency_key=verified.idempotency_key,
        idempotent_replay=replayed,
        result=result,
        checks=[outcome.as_dict() for outcome in verified.checks],
    )


@router.get(
    "/internal/checks",
    tags=["internal"],
    summary="The seven checks this gateway performs, in order",
)
def describe_checks() -> dict:
    """Self-describing, so the boundary guide and the UI cannot drift from the code."""
    return {
        "checks": [
            {
                "number": check.number,
                "name": check.name,
                "refusal_code": check.refusal_code,
                "question": check.question,
                "threat": check.threat,
            }
            for check in CHECKS
        ]
    }


def install_error_handling(app) -> None:
    """Refusals surface as the section 6 envelope, with the code intact.

    Every refusal is also appended to the audit log before the response is written: a
    refused attempt is exactly what a reviewer wants to see in the record.
    """

    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        executor.record_refusal(exc)
        log.info("refused: %s (%s)", exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.envelope())
