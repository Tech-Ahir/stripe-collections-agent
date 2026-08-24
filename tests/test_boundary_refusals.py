"""The approval boundary (brief section 5). THIS SUITE MUST STAY GREEN AT ALL TIMES.

If a change makes a refusal test fail, the change is wrong -- not the test.

The gateway performs seven checks, in order, and refuses on the first failure. Read the
test names against the table in section 5 of the brief:

    #  CHECK                                            REFUSAL
    1  HMAC signature valid                             401 invalid_signature
    2  Token not expired                                401 token_expired
    3  Nonce not seen before                            409 token_replayed
    4  Proposal exists and status is approved           403 not_approved
    5  Approval record exists, is an approve, and       403 approval_mismatch
       matches the token's approver
    6  Recomputed payload hash equals the token's       409 payload_modified
       payload_hash
    7  Idempotency key unused                           200 with the original result

Check 4 is the one that matters: the gateway reads the proposal's status from the database
itself and never from the request body, which is why a forged or replayed request cannot
cause a send -- the gateway's answer comes from state the caller does not control.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest

from shared.hashing import hash_payload
from shared.models import Execution, OutboxMessage, Proposal
from tests.factories import (
    letter_payload,
    make_proposal,
    mint_claims,
    mint_token,
    record_approval,
)

EXECUTE_PATH = "/internal/actions/execute"


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------


def _sent_count(session) -> int:
    return session.query(OutboxMessage).count()


def _post(client, token: str, body: dict, *, idempotency_key: str | None = None):
    return client.post(
        EXECUTE_PATH,
        json=body,
        headers={
            "X-Approval-Token": token,
            "X-Idempotency-Key": idempotency_key or str(uuid.uuid4()),
        },
    )


def _body(proposal: Proposal, *, payload_hash: str | None = None) -> dict:
    return {
        "proposal_id": proposal.id,
        "payload_hash": payload_hash or proposal.payload_hash,
    }


def _error(response) -> dict:
    payload = response.json()
    assert "error" in payload, f"expected the section 6 error envelope, got {payload}"
    return payload["error"]


# ----------------------------------------------------------------------------------
# The happy path, so the refusals mean something
# ----------------------------------------------------------------------------------


def test_an_approved_proposal_executes_and_all_seven_checks_pass(gateway_client, gw_session):
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "executed"
    assert [check["name"] for check in result["checks"]] == [
        "hmac_signature_valid",
        "token_not_expired",
        "nonce_unused",
        "proposal_is_approved",
        "approval_matches_approver",
        "payload_hash_matches",
        "idempotency_key_unused",
    ], "all seven checks must be reported, in order"
    assert all(check["passed"] for check in result["checks"])

    gw_session.expire_all()
    assert gw_session.get(Proposal, proposal.id).status == "executed"
    assert _sent_count(gw_session) == 1


# ----------------------------------------------------------------------------------
# Check 1 -- HMAC signature valid -> 401 invalid_signature
# ----------------------------------------------------------------------------------


def test_check_1_a_forged_token_is_refused_with_invalid_signature(gateway_client, gw_session):
    """ "Execute with a forged token -> 401 invalid_signature." Nothing is sent."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()

    forged = mint_token(proposal, approval, "an-attackers-secret-which-is-not-ours")

    response = _post(gateway_client, forged, _body(proposal))

    assert response.status_code == 401
    assert _error(response)["code"] == "invalid_signature"
    assert _sent_count(gw_session) == 0


def test_check_1_tampering_with_the_claims_invalidates_the_signature(gateway_client, gw_session):
    """Editing the payload_hash inside the token breaks the signature over it."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    claims_segment, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(claims_segment + "=="))
    claims["payload_hash"] = "sha256:" + "0" * 64
    rewritten = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + "." + signature
    )

    response = _post(gateway_client, rewritten, _body(proposal))

    assert response.status_code == 401
    assert _error(response)["code"] == "invalid_signature"
    assert _sent_count(gw_session) == 0


@pytest.mark.parametrize("raw", ["", "not-a-token", "only-one-segment", "a.b.c", "!!!.???"])
def test_check_1_a_malformed_token_is_refused_and_never_parsed_as_trusted(
    gateway_client, gw_session, raw
):
    proposal = make_proposal(gw_session, status="approved")
    gw_session.commit()

    response = _post(gateway_client, raw, _body(proposal))

    assert response.status_code == 401
    assert _error(response)["code"] in ("invalid_signature", "malformed_token")
    assert _sent_count(gw_session) == 0


# ----------------------------------------------------------------------------------
# Check 2 -- token not expired -> 401 token_expired
# ----------------------------------------------------------------------------------


def test_check_2_an_expired_token_is_refused(gateway_client, gw_session):
    """Section 5 gives the token a 15 minute life. A stale one cannot cause a send."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    stale = mint_token(
        proposal, approval, "test-secret-" + "0" * 40, ttl_seconds=900, iat_offset=-1800
    )

    response = _post(gateway_client, stale, _body(proposal))

    assert response.status_code == 401
    assert _error(response)["code"] == "token_expired"
    assert _sent_count(gw_session) == 0


def test_check_2_runs_after_check_1_so_an_expired_forgery_reports_the_signature(
    gateway_client, gw_session
):
    """Order matters. A forged AND expired token must report the signature failure."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "wrong-secret", ttl_seconds=900, iat_offset=-1800)

    response = _post(gateway_client, token, _body(proposal))

    assert _error(response)["code"] == "invalid_signature"


# ----------------------------------------------------------------------------------
# Check 3 -- nonce not seen before -> 409 token_replayed
# ----------------------------------------------------------------------------------


def test_check_3_replaying_a_valid_token_is_refused(gateway_client, gw_session):
    """ "Replay a valid token -> 409 token_replayed." One approval, one send."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    first = _post(gateway_client, token, _body(proposal))
    assert first.status_code == 200

    # A replay is the SAME token used to cause a SECOND send, so a new idempotency key.
    replay = _post(gateway_client, token, _body(proposal))

    assert replay.status_code == 409
    assert _error(replay)["code"] == "token_replayed"
    assert _sent_count(gw_session) == 1, "the replay must not have caused a second send"


def test_check_3_distinguishes_a_replay_from_a_client_retry(gateway_client, gw_session):
    """A retry is not a replay, and the difference is the idempotency key.

    The brief requires both "replay a valid token -> 409" and "repeat the same
    idempotency key -> single send, original result returned". Both can only hold if a
    replay is defined as reuse of the token to cause a *new* request. So the consumed
    nonce is recorded against the idempotency key that consumed it: a different key is a
    replay, the same key is the same logical request and falls through to check 7.
    """
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)
    key = str(uuid.uuid4())

    first = _post(gateway_client, token, _body(proposal), idempotency_key=key)
    retry = _post(gateway_client, token, _body(proposal), idempotency_key=key)
    replay = _post(gateway_client, token, _body(proposal), idempotency_key=str(uuid.uuid4()))

    assert first.status_code == 200
    assert retry.status_code == 200, "same key is a retry, not a replay"
    assert replay.status_code == 409
    assert _error(replay)["code"] == "token_replayed"
    assert _sent_count(gw_session) == 1


# ----------------------------------------------------------------------------------
# Check 4 -- proposal exists and status is approved -> 403 not_approved
#
# The check that matters. Status comes from the database, never from the request body.
# ----------------------------------------------------------------------------------


def test_check_4_executing_a_pending_proposal_is_refused(gateway_client, gw_session):
    """ "Execute a pending proposal -> 403 not_approved, nothing sent."

    This is step 4 of the handoff demo and acceptance criterion 5. The signature here is
    valid: the refusal comes from the gateway's own reading of the proposal's status.
    """
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    proposal.status = "pending"  # the operator has not actually approved
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 403
    error = _error(response)
    assert error["code"] == "not_approved"
    assert "pending" in error["message"]
    assert error["proposal_id"] == proposal.id
    assert _sent_count(gw_session) == 0


def test_check_4_executing_a_rejected_proposal_is_refused(gateway_client, gw_session):
    """ "Execute a rejected proposal -> 403 not_approved." Step 6 of the handoff demo."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal, decision="reject")
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 403
    assert _error(response)["code"] == "not_approved"
    assert _sent_count(gw_session) == 0


def test_check_4_a_status_asserted_in_the_request_body_is_ignored(gateway_client, gw_session):
    """The gateway trusts nothing in the body. Claiming to be approved changes nothing."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    proposal.status = "pending"
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = gateway_client.post(
        EXECUTE_PATH,
        json={
            "proposal_id": proposal.id,
            "payload_hash": proposal.payload_hash,
            "status": "approved",  # a lie
            "approved": True,  # another one
        },
        headers={"X-Approval-Token": token, "X-Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 403
    assert _error(response)["code"] == "not_approved"
    assert _sent_count(gw_session) == 0


def test_check_4_a_token_for_a_nonexistent_proposal_is_refused(gateway_client, gw_session):
    """A validly signed token naming a proposal that does not exist sends nothing."""
    ghost = str(uuid.uuid4())
    token = mint_claims(
        "test-secret-" + "0" * 40,
        proposal_id=ghost,
        payload_hash="sha256:" + "1" * 64,
    )

    response = gateway_client.post(
        EXECUTE_PATH,
        json={"proposal_id": ghost, "payload_hash": "sha256:" + "1" * 64},
        headers={"X-Approval-Token": token, "X-Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 403
    assert _error(response)["code"] == "not_approved"
    assert _sent_count(gw_session) == 0


def test_a_proposal_id_in_the_body_cannot_redirect_the_send(gateway_client, gw_session):
    """The body is inert. The token decides which proposal is executed, and only that one.

    Section 5: the gateway trusts nothing in the body. So an attacker who holds a valid
    token for proposal A and asks for proposal B does not get a refusal -- they get
    proposal A, which is what they were authorised for. Nothing about B is touched.
    """
    target = make_proposal(gw_session, status="pending", payload=letter_payload(invoice_id="in_A"))
    approval = record_approval(gw_session, target)
    other = make_proposal(gw_session, status="pending", payload=letter_payload(invoice_id="in_B"))
    gw_session.commit()
    token = mint_token(target, approval, "test-secret-" + "0" * 40)

    response = gateway_client.post(
        EXECUTE_PATH,
        json={"proposal_id": other.id, "payload_hash": other.payload_hash},
        headers={"X-Approval-Token": token, "X-Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 200
    assert response.json()["proposal_id"] == target.id, "the token's proposal, not the body's"
    gw_session.expire_all()
    assert gw_session.get(Proposal, other.id).status == "pending", "B was never touched"
    assert gw_session.query(OutboxMessage).one().proposal_id == target.id


def test_check_4_an_expired_proposal_is_refused(gateway_client, gw_session):
    """Stale approvals are worse than no approvals (section 4)."""
    proposal = make_proposal(gw_session, status="pending", ttl_seconds=-60)
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 403
    assert _error(response)["code"] in ("not_approved", "proposal_expired")
    assert _sent_count(gw_session) == 0


# ----------------------------------------------------------------------------------
# Check 5 -- the approval record matches the token's approver -> 403 approval_mismatch
# ----------------------------------------------------------------------------------


def test_check_5_a_token_naming_a_different_approver_is_refused(gateway_client, gw_session):
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal, actor="operator@servicia.ai")
    gw_session.commit()
    token = mint_token(
        proposal, approval, "test-secret-" + "0" * 40, approver="someone.else@evil.test"
    )

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 403
    assert _error(response)["code"] == "approval_mismatch"
    assert _sent_count(gw_session) == 0


def test_check_5_a_proposal_approved_with_no_approval_record_is_refused(gateway_client, gw_session):
    """A status of 'approved' with no approval row behind it is not an approval."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.query(type(approval)).filter_by(id=approval.id).delete()
    proposal.status = "approved"
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 403
    assert _error(response)["code"] == "approval_mismatch"
    assert _sent_count(gw_session) == 0


# ----------------------------------------------------------------------------------
# Check 6 -- recomputed payload hash equals the token's -> 409 payload_modified
# ----------------------------------------------------------------------------------


def test_check_6_altering_the_letter_after_approval_is_refused(gateway_client, gw_session):
    """ "Approve, then alter the letter body, then execute -> 409 payload_modified."

    Acceptance criterion 6. Approving one letter cannot be used to send a different one.
    """
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    # The letter is edited after the token was minted.
    tampered = dict(proposal.payload)
    tampered["body"] = tampered["body"].replace("$250.00", "$25,000.00")
    proposal.payload = tampered
    proposal.payload_hash = hash_payload(tampered)
    gw_session.commit()

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 409
    assert _error(response)["code"] == "payload_modified"
    assert _sent_count(gw_session) == 0


def test_check_6_a_single_character_change_is_enough(gateway_client, gw_session):
    """The comparison is exact. There is no normalise-then-compare path."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    tampered = dict(proposal.payload)
    tampered["body"] = tampered["body"] + " "
    proposal.payload = tampered
    proposal.payload_hash = hash_payload(tampered)
    gw_session.commit()

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 409
    assert _error(response)["code"] == "payload_modified"


def test_check_6_a_hash_asserted_in_the_body_cannot_override_the_recomputation(
    gateway_client, gw_session
):
    """The body's payload_hash is untrusted. Only the DB recomputation counts."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    tampered = dict(proposal.payload)
    tampered["body"] = "Pay immediately or we will sue you."
    proposal.payload = tampered
    proposal.payload_hash = hash_payload(tampered)
    gw_session.commit()

    # The caller sends the ORIGINAL hash in the body, trying to look unmodified.
    response = _post(gateway_client, token, _body(proposal, payload_hash=token.split(".")[0]))

    assert response.status_code == 409
    assert _error(response)["code"] == "payload_modified"
    assert _sent_count(gw_session) == 0


def test_check_6_an_edit_before_approval_is_fine_because_the_hash_is_reminted(
    gateway_client, gw_session
):
    """The operator approves what they actually read (section 6).

    An edit followed by a fresh mint is the legitimate path and must succeed, otherwise
    check 6 would make editing impossible rather than merely binding.
    """
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(
        gw_session, proposal, edited_body="A shorter, friendlier reminder. INV-1001, $250.00."
    )
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = _post(gateway_client, token, _body(proposal))

    assert response.status_code == 200, response.text
    gw_session.expire_all()
    sent = gw_session.query(OutboxMessage).one()
    assert "shorter, friendlier" in sent.body, "the edited text is what goes out"


# ----------------------------------------------------------------------------------
# Check 7 -- idempotency key unused -> 200 with the original result, no re-send
# ----------------------------------------------------------------------------------


def test_check_7_repeating_an_idempotency_key_returns_the_original_result(
    gateway_client, gw_session
):
    """"Repeat the same idempotency key -> single send, original result returned.""" ""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)
    key = "fixed-idempotency-key-0001"

    first = _post(gateway_client, token, _body(proposal), idempotency_key=key)
    second = _post(gateway_client, token, _body(proposal), idempotency_key=key)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["result"] == first.json()["result"]
    assert _sent_count(gw_session) == 1, "exactly one send"
    assert gw_session.query(Execution).count() == 1, "exactly one execution row"


def test_check_7_a_missing_idempotency_key_is_rejected(gateway_client, gw_session):
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = gateway_client.post(
        EXECUTE_PATH,
        json=_body(proposal),
        headers={"X-Approval-Token": token},
    )

    assert response.status_code in (400, 422)
    assert _sent_count(gw_session) == 0


# ----------------------------------------------------------------------------------
# The checks run in order, and none is skipped
# ----------------------------------------------------------------------------------


def test_the_seven_checks_are_declared_in_order_and_none_is_missing():
    """The check registry is the contract. Section 5's table, in code."""
    from gateway.verify import CHECKS

    assert [(c.number, c.name, c.refusal_code) for c in CHECKS] == [
        (1, "hmac_signature_valid", "invalid_signature"),
        (2, "token_not_expired", "token_expired"),
        (3, "nonce_unused", "token_replayed"),
        (4, "proposal_is_approved", "not_approved"),
        (5, "approval_matches_approver", "approval_mismatch"),
        (6, "payload_hash_matches", "payload_modified"),
        (7, "idempotency_key_unused", None),
    ]


def test_a_refusal_reports_exactly_the_checks_that_ran_before_it(gateway_client, gw_session):
    """A failure at check N means N-1 passes were recorded. No check is skipped."""
    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    proposal.status = "pending"
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    response = _post(gateway_client, token, _body(proposal))
    error = _error(response)

    assert error["code"] == "not_approved"
    assert error["failed_check"] == 4
    assert error["checks_passed"] == [
        "hmac_signature_valid",
        "token_not_expired",
        "nonce_unused",
    ]


# ----------------------------------------------------------------------------------
# Nothing leaves the system on any refusal
# ----------------------------------------------------------------------------------


def test_no_refusal_path_ever_produces_a_send(gateway_client, gw_session):
    """One test, every refusal. If any of them sends, this fails."""
    secret = "test-secret-" + "0" * 40
    attempts = 0

    scenarios = [
        ("forged", lambda p, a: mint_token(p, a, "not-our-secret"), None),
        ("expired", lambda p, a: mint_token(p, a, secret, iat_offset=-3600), None),
        ("wrong approver", lambda p, a: mint_token(p, a, secret, approver="x@y.z"), None),
        (
            "bad hash",
            lambda p, a: mint_token(p, a, secret, payload_hash="sha256:" + "0" * 64),
            None,
        ),
    ]
    for index, (_name, mint_fn, _) in enumerate(scenarios):
        payload = letter_payload(invoice_id=f"in_neg_{index}")
        proposal = make_proposal(gw_session, status="pending", payload=payload)
        approval = record_approval(gw_session, proposal)
        gw_session.commit()
        response = _post(gateway_client, mint_fn(proposal, approval), _body(proposal))
        attempts += 1
        assert response.status_code >= 400, f"{_name} should have been refused"

    assert attempts == len(scenarios)
    assert _sent_count(gw_session) == 0, "no refusal may produce a send"
    assert gw_session.query(Execution).count() == 0, "no refusal may create an execution"


def test_every_refusal_is_recorded_in_the_audit_log(gateway_client, gw_session):
    """A refused attempt is exactly what a reviewer wants to see in the audit log."""
    from shared.audit import verify_chain
    from shared.models import AuditEvent

    proposal = make_proposal(gw_session, status="pending")
    approval = record_approval(gw_session, proposal)
    proposal.status = "pending"
    gw_session.commit()
    token = mint_token(proposal, approval, "test-secret-" + "0" * 40)

    _post(gateway_client, token, _body(proposal))

    gw_session.expire_all()
    refusals = gw_session.query(AuditEvent).filter(AuditEvent.event == "action.refused").all()
    assert len(refusals) == 1
    assert refusals[0].actor == "gateway"
    assert refusals[0].detail["code"] == "not_approved"
    assert refusals[0].detail["failed_check"] == 4
    assert verify_chain(gw_session).intact is True


# ----------------------------------------------------------------------------------
# Check 7 of the demo: the gateway is unreachable from the host
# ----------------------------------------------------------------------------------


def test_the_gateway_is_unreachable_from_the_host_by_construction():
    """ "Attempt to reach the gateway from the host -> connection refused."

    Asserted against the deployment configuration rather than by opening a socket, so it
    holds in CI and on a developer machine alike. The live socket attempt is step 1 of the
    handoff demo and is covered by the compose job in .github/workflows/ci.yml.
    """
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    )
    assert "ports" not in compose["services"]["gateway"]
