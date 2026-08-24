"""The approval boundary, demonstrated from a terminal in under a minute.

    docker compose exec app python scripts/demo_boundary.py

Section 5 of the brief says this is how the handoff call opens, so it is a shipped script
rather than a set of instructions. It talks to the *real* gateway over the internal Docker
network -- no test client, no mocks -- and prints what happened at each step.

Every refusal below is a validly signed request. That is the point: the gateway is not
refusing because the caller got the crypto wrong, it is refusing because it reads the
proposal's state from its own database.
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.approval.token import mint_for_approval, new_nonce  # noqa: E402
from shared.approval_token import ApprovalTokenClaims, mint  # noqa: E402
from shared.audit import verify_chain  # noqa: E402
from shared.clock import now_utc, seconds_from_now  # noqa: E402
from shared.db import session_scope  # noqa: E402
from shared.hashing import hash_payload  # noqa: E402
from shared.models import Approval, OutboxMessage, Proposal, Run  # noqa: E402
from shared.schema import ensure_schema  # noqa: E402

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:9000")
SECRET = os.environ["APPROVAL_SIGNING_SECRET"]
EXECUTE = GATEWAY.rstrip("/") + "/internal/actions/execute"

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

_step = 0


def heading(text: str) -> None:
    global _step
    _step += 1
    print(f"\n{BOLD}{_step}. {text}{RESET}")


def outcome(expected: str, response: httpx.Response) -> bool:
    body = response.json()
    actual = body.get("error", {}).get("code") or body.get("status") or "?"
    ok = actual == expected
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"   HTTP {response.status_code}  {actual:<18} expected {expected:<18} [{mark}]")
    if "error" in body:
        error = body["error"]
        if error.get("failed_check"):
            passed = ", ".join(error.get("checks_passed") or []) or "none"
            print(f"   {DIM}failed at check {error['failed_check']}; passed: {passed}{RESET}")
        print(f"   {DIM}{error.get('message', '')}{RESET}")
    return ok


def letter(invoice_id: str) -> dict:
    return {
        "action_type": "send_collection_letter",
        "invoice_id": invoice_id,
        "invoice_number": "INV-" + invoice_id[-4:],
        "customer_name": "Acme Industries",
        "customer_email": "ap@acme.test",
        "amount_display": "$250.00",
        "amount_due_minor": 25000,
        "currency": "usd",
        "due_date": "2026-08-01",
        "days_overdue": 9,
        "hosted_invoice_url": "https://invoice.stripe.com/i/test_" + invoice_id,
        "subject": "Invoice is 9 days past due",
        "body": "Dear Acme Industries,\n\nInvoice INV-1001 for $250.00 is 9 days past due.\n",
        "tone": "friendly",
    }


def make_proposal(session, invoice_id: str) -> Proposal:
    run = session.query(Run).first()
    if run is None:
        run = Run(
            goal="Boundary demonstration",
            status="awaiting_approval",
            operator_id="operator@servicia.ai",
            params={},
        )
        session.add(run)
        session.flush()
    payload = letter(invoice_id)
    proposal = Proposal(
        run_id=run.id,
        status="pending",
        payload=payload,
        payload_hash=hash_payload(payload),
        rationale="Nine days late with a clean history, so a courteous reminder.",
        stripe_invoice_id=invoice_id,
        customer_email=payload["customer_email"],
        amount_due=payload["amount_due_minor"],
        currency=payload["currency"],
        days_overdue=payload["days_overdue"],
        expires_at=seconds_from_now(3600),
    )
    session.add(proposal)
    session.flush()
    return proposal


def approve(session, proposal: Proposal) -> tuple[str, str]:
    approval = Approval(
        proposal_id=proposal.id,
        decision="approve",
        actor="operator@servicia.ai",
        token_nonce=new_nonce(),
    )
    proposal.status = "approved"
    session.add(approval)
    session.flush()
    return mint_for_approval(proposal, approval, secret=SECRET), proposal.payload_hash


def post(token: str, proposal_id: str, payload_hash: str, key: str | None = None):
    return httpx.post(
        EXECUTE,
        json={"proposal_id": proposal_id, "payload_hash": payload_hash},
        headers={
            "X-Approval-Token": token,
            "X-Idempotency-Key": key or str(uuid.uuid4()),
        },
        timeout=20.0,
    )


def main() -> int:
    ensure_schema()
    print(f"{BOLD}The approval boundary, against the live gateway at {GATEWAY}{RESET}")
    results: list[bool] = []
    suffix = uuid.uuid4().hex[:6]

    # --- an unapproved proposal ----------------------------------------------------
    heading("A pending proposal, executed with a VALID token -> 403 not_approved")
    with session_scope() as session:
        proposal = make_proposal(session, f"in_demo_a_{suffix}")
        approval = Approval(
            proposal_id=proposal.id,
            decision="approve",
            actor="operator@servicia.ai",
            token_nonce=new_nonce(),
        )
        session.add(approval)
        session.flush()
        token = mint_for_approval(proposal, approval, secret=SECRET)
        pid, phash = proposal.id, proposal.payload_hash
    results.append(outcome("not_approved", post(token, pid, phash)))

    # --- a forged token ------------------------------------------------------------
    heading("A forged token (signed with the wrong secret) -> 401 invalid_signature")
    forged = mint(
        ApprovalTokenClaims(
            proposal_id=pid,
            payload_hash=phash,
            action_type="send_collection_letter",
            approver="operator@servicia.ai",
            nonce=new_nonce(),
            iat=int(now_utc().timestamp()),
            exp=int(now_utc().timestamp()) + 900,
        ),
        "an-attackers-secret",
    )
    results.append(outcome("invalid_signature", post(forged, pid, phash)))

    # --- an expired token ----------------------------------------------------------
    heading("An expired token -> 401 token_expired")
    stale = mint(
        ApprovalTokenClaims(
            proposal_id=pid,
            payload_hash=phash,
            action_type="send_collection_letter",
            approver="operator@servicia.ai",
            nonce=new_nonce(),
            iat=int(now_utc().timestamp()) - 3600,
            exp=int(now_utc().timestamp()) - 1800,
        ),
        SECRET,
    )
    results.append(outcome("token_expired", post(stale, pid, phash)))

    # --- a rejected proposal -------------------------------------------------------
    heading("A REJECTED proposal, executed with a valid token -> 403 not_approved")
    with session_scope() as session:
        rejected = make_proposal(session, f"in_demo_b_{suffix}")
        approval = Approval(
            proposal_id=rejected.id,
            decision="approve",
            actor="operator@servicia.ai",
            token_nonce=new_nonce(),
        )
        session.add(approval)
        session.flush()
        token = mint_for_approval(rejected, approval, secret=SECRET)
        rejected.status = "rejected"
        rid, rhash = rejected.id, rejected.payload_hash
    results.append(outcome("not_approved", post(token, rid, rhash)))

    # --- approved, then edited -----------------------------------------------------
    heading("Approved, then the letter is altered -> 409 payload_modified")
    with session_scope() as session:
        edited = make_proposal(session, f"in_demo_c_{suffix}")
        token, _ = approve(session, edited)
        eid = edited.id
    with session_scope() as session:
        target = session.get(Proposal, eid)
        payload = dict(target.payload)
        payload["body"] = payload["body"].replace("$250.00", "$25,000.00")
        target.payload = payload
        target.payload_hash = hash_payload(payload)
        ehash = target.payload_hash
    results.append(outcome("payload_modified", post(token, eid, ehash)))

    # --- the happy path ------------------------------------------------------------
    heading("Properly approved and unmodified -> 200, all seven checks pass")
    with session_scope() as session:
        good = make_proposal(session, f"in_demo_d_{suffix}")
        token, ghash = approve(session, good)
        gid = good.id
    key = str(uuid.uuid4())
    response = post(token, gid, ghash, key)
    results.append(outcome("executed", response))
    if response.status_code == 200:
        for check in response.json()["checks"]:
            print(f"   {GREEN}v{RESET} {check['number']}. {check['name']}")

    # --- replay --------------------------------------------------------------------
    heading("The same token used again for a NEW request -> 409 token_replayed")
    results.append(outcome("token_replayed", post(token, gid, ghash)))

    # --- retry ---------------------------------------------------------------------
    heading("The same token AND the same idempotency key -> 200, original result, no re-send")
    results.append(outcome("already_executed", post(token, gid, ghash, key)))

    # --- the record ----------------------------------------------------------------
    heading("The audit log")
    with session_scope() as session:
        status = verify_chain(session)
        sent = session.query(OutboxMessage).count()
        print(
            f"   chain: {'INTACT' if status.intact else 'BROKEN at ' + str(status.broken_at_id)}"
            f"  ({status.length} events)"
        )
        print(f"   letters actually sent across all {_step - 1} attempts: {sent}")
        results.append(status.intact)

    passed = sum(1 for r in results if r)
    print(
        f"\n{BOLD}{passed}/{len(results)} steps as expected{RESET}"
        f"  {DIM}(one send, from the one properly approved proposal){RESET}\n"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
