"""The public API and the four screens (brief sections 6 and 9).

The approval path is the subject of most of this file, because it is the one place a human
decision turns into a request that could cause a send. In particular:

* the edit-then-approve path must mint the token from the EDITED text, never the draft;
* a refusal must reach the operator with its code intact;
* the "try to send without approval" button must produce 403 not_approved and send nothing.

The gateway is stubbed here so these tests are about the app's behaviour. The gateway's own
seven checks are covered against the real service in tests/test_boundary_refusals.py, and the
two are wired together for real by scripts/demo_boundary.py.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.gateway_client import GatewayOutcome
from shared.approval_token import decode
from shared.db import session_scope
from shared.hashing import hash_payload
from shared.models import Approval, AuditEvent, Proposal
from tests.factories import letter_payload, make_proposal

SECRET = "x" * 64


class StubGateway:
    """Records what the app sent, and answers however the test needs.

    Recording the token is the point: several tests decode it to prove what the app
    actually authorised.
    """

    def __init__(self, outcome: GatewayOutcome | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcome = outcome or GatewayOutcome(
            http_status=200,
            body={
                "status": "executed",
                "proposal_id": "",
                "execution_id": 1,
                "idempotency_key": "k",
                "checks": [
                    {"number": n, "name": name, "passed": True, "question": "?"}
                    for n, name in enumerate(
                        [
                            "hmac_signature_valid",
                            "token_not_expired",
                            "nonce_unused",
                            "proposal_is_approved",
                            "approval_matches_approver",
                            "payload_hash_matches",
                            "idempotency_key_unused",
                        ],
                        start=1,
                    )
                ],
            },
        )

    def execute(self, *, token, proposal_id, payload_hash, idempotency_key=None):
        self.calls.append(
            {
                "token": token,
                "proposal_id": proposal_id,
                "payload_hash": payload_hash,
                "claims": decode(token, SECRET),
            }
        )
        return self.outcome


def refusal(code: str, *, status: int, failed_check: int | None = None) -> GatewayOutcome:
    return GatewayOutcome(
        http_status=status,
        body={
            "error": {
                "code": code,
                "message": f"refused: {code}",
                "failed_check": failed_check,
                "checks_passed": ["hmac_signature_valid", "token_not_expired"],
            }
        },
    )


@pytest.fixture()
def api(tmp_path):
    """The real app on a fresh database, with credentials that do not reach anything."""
    from app import config as app_config
    from shared import db as db_module

    values = {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
        "APPROVAL_SIGNING_SECRET": SECRET,
        "STRIPE_API_KEY_READ": "",
        "ANTHROPIC_API_KEY": "",
        "OPERATOR_ID": "operator@servicia.ai",
        "GATEWAY_URL": "http://gateway:9000",
    }
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    app_config.settings.cache_clear()
    db_module.reset_engine_for_tests()

    from app.main import app as fastapi_app

    with TestClient(fastapi_app, raise_server_exceptions=False) as client:
        yield client

    app_config.settings.cache_clear()
    db_module.reset_engine_for_tests()
    for key, previous in saved.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@pytest.fixture()
def pending(api):
    """One pending proposal, created directly so these tests are about the API."""
    with session_scope() as session:
        proposal = make_proposal(session, status="pending")
        return proposal.id


def stub_into(monkeypatch, gateway: StubGateway) -> StubGateway:
    monkeypatch.setattr("app.services.proposals.GatewayClient", lambda *a, **k: gateway)
    return gateway


# ----------------------------------------------------------------------------------
# Shape of the API
# ----------------------------------------------------------------------------------


def test_the_openapi_document_publishes_every_endpoint_section_6_names(api):
    spec = api.get("/openapi.json").json()
    documented = {(method.upper(), path) for path, item in spec["paths"].items() for method in item}
    for required in [
        ("POST", "/v1/runs"),
        ("GET", "/v1/runs"),
        ("GET", "/v1/runs/{run_id}"),
        ("GET", "/v1/runs/{run_id}/stream"),
        ("GET", "/v1/proposals"),
        ("GET", "/v1/proposals/{proposal_id}"),
        ("POST", "/v1/proposals/{proposal_id}/approve"),
        ("POST", "/v1/proposals/{proposal_id}/reject"),
        ("GET", "/v1/invoices/overdue"),
        ("GET", "/v1/audit"),
        ("GET", "/healthz"),
    ]:
        assert required in documented, f"section 6 requires {required[0]} {required[1]}"


def test_there_is_no_endpoint_that_sends_anything(api):
    """The closest thing to a send is /approve, which asks the gateway to decide."""
    spec = api.get("/openapi.json").json()
    for path in spec["paths"]:
        assert "send" not in path.lower(), f"{path} looks like it sends something"


def test_healthz_reports_each_dependency_separately(api):
    body = api.get("/healthz").json()
    assert set(body["checks"]) == {"database", "stripe", "anthropic", "gateway"}
    assert body["checks"]["anthropic"]["status"] == "not_configured"


def test_a_missing_proposal_returns_the_section_6_error_envelope(api):
    body = api.get("/v1/proposals/nope").json()
    assert body["error"]["code"] == "proposal_not_found"
    assert "message" in body["error"]


# ----------------------------------------------------------------------------------
# Reading proposals
# ----------------------------------------------------------------------------------


def test_the_queue_defaults_to_pending_and_sorts_by_amount_descending(api):
    with session_scope() as session:
        make_proposal(session, payload=letter_payload(invoice_id="in_small"), status="pending")
        payload = letter_payload(invoice_id="in_big")
        payload["amount_due_minor"] = 900000
        payload["amount_display"] = "$9,000.00"
        make_proposal(session, payload=payload, status="pending")

    rows = api.get("/v1/proposals").json()
    assert [row["amount_display"] for row in rows] == ["$9,000.00", "$250.00"]
    assert all(row["status"] == "pending" for row in rows)


def test_a_proposal_carries_both_the_letter_and_the_facts_it_was_built_from(api, pending):
    """Section 8: "so the operator can see both"."""
    detail = api.get(f"/v1/proposals/{pending}").json()
    assert detail["body"].startswith("Dear Acme Industries")
    assert detail["invoice_facts"]["amount_display"] == "$250.00"
    assert detail["invoice_facts"]["hosted_invoice_url"].startswith("https://")
    assert detail["payload_hash"].startswith("sha256:")
    assert detail["rationale"]


def test_the_amount_is_published_as_both_minor_units_and_a_display_string(api, pending):
    detail = api.get(f"/v1/proposals/{pending}").json()
    assert detail["amount_due"] == 25000
    assert detail["amount_display"] == "$250.00"


# ----------------------------------------------------------------------------------
# Approval
# ----------------------------------------------------------------------------------


def test_approving_records_the_decision_mints_a_token_and_calls_the_gateway(
    api, pending, monkeypatch
):
    gateway = stub_into(monkeypatch, StubGateway())

    body = api.post(f"/v1/proposals/{pending}/approve", json={"note": "looks right"}).json()

    assert body["decision"] == "approve"
    assert body["gateway"]["executed"] is True
    assert len(body["gateway"]["checks"]) == 7

    assert len(gateway.calls) == 1
    claims = gateway.calls[0]["claims"]
    assert claims.proposal_id == pending
    assert claims.approver == "operator@servicia.ai"
    assert claims.action_type == "send_collection_letter"

    with session_scope() as session:
        approval = session.query(Approval).one()
        assert approval.decision == "approve"
        assert approval.note == "looks right"
        assert approval.token_nonce == claims.nonce


def test_the_token_is_minted_from_the_edited_text_not_the_draft(api, pending, monkeypatch):
    """Section 6: "Do not mint the token from the original draft."

    The strongest single assertion in this file. If the token carried the draft's hash, the
    operator would be approving text the customer would never receive.
    """
    gateway = stub_into(monkeypatch, StubGateway())
    with session_scope() as session:
        original_hash = session.get(Proposal, pending).payload_hash

    edited = "Dear Acme Industries,\n\nA shorter note. INV-1001, $250.00, due 2026-08-01."
    api.post(
        f"/v1/proposals/{pending}/approve",
        json={"edited_body": edited, "note": "trimmed it"},
    )

    claims = gateway.calls[0]["claims"]
    with session_scope() as session:
        proposal = session.get(Proposal, pending)
        expected = hash_payload(proposal.payload)
        assert proposal.payload["body"] == edited

    assert claims.payload_hash == expected
    assert claims.payload_hash != original_hash, "the token must not carry the draft's hash"


def test_an_edit_is_recorded_on_the_approval_record(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/approve", json={"edited_body": "Edited text."})
    with session_scope() as session:
        assert session.query(Approval).one().edited_body == "Edited text."


def test_approving_twice_is_refused(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    first = api.post(f"/v1/proposals/{pending}/approve", json={})
    second = api.post(f"/v1/proposals/{pending}/approve", json={})
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "proposal_not_pending"


def test_an_expired_proposal_cannot_be_approved(api, monkeypatch):
    """Stale approvals are worse than no approvals (section 4)."""
    stub_into(monkeypatch, StubGateway())
    with session_scope() as session:
        proposal = make_proposal(session, status="pending", ttl_seconds=-60)
        proposal_id = proposal.id

    response = api.post(f"/v1/proposals/{proposal_id}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "proposal_not_pending"


# ----------------------------------------------------------------------------------
# Refusals reach the operator with the code intact
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,status,check",
    [
        ("not_approved", 403, 4),
        ("payload_modified", 409, 6),
        ("token_replayed", 409, 3),
        ("invalid_signature", 401, 1),
    ],
)
def test_a_gateway_refusal_surfaces_unchanged(api, pending, monkeypatch, code, status, check):
    """Section 6: "Gateway refusal codes surface to the UI unchanged.""" ""
    stub_into(monkeypatch, StubGateway(refusal(code, status=status, failed_check=check)))

    body = api.post(f"/v1/proposals/{pending}/approve", json={}).json()

    assert body["gateway"]["executed"] is False
    assert body["gateway"]["error_code"] == code
    assert body["gateway"]["http_status"] == status
    assert body["gateway"]["failed_check"] == check
    assert body["gateway"]["checks_passed"] == ["hmac_signature_valid", "token_not_expired"]


def test_a_refusal_does_not_undo_the_human_decision(api, pending, monkeypatch):
    """The approval happened. The gateway declining to act does not unmake it."""
    stub_into(monkeypatch, StubGateway(refusal("payload_modified", status=409, failed_check=6)))
    api.post(f"/v1/proposals/{pending}/approve", json={})
    with session_scope() as session:
        assert session.get(Proposal, pending).status == "approved"
        assert session.query(Approval).one().decision == "approve"


def test_an_unreachable_gateway_is_reported_as_such_and_not_as_a_refusal(api, pending, monkeypatch):
    class Dead:
        def execute(self, **_kwargs):
            import httpx

            from app.gateway_client import GatewayClient

            real = GatewayClient("http://gateway:9000")

            # Force the real transport-error path rather than faking its output.
            def boom(*_a, **_k):
                raise httpx.ConnectError("no route to host")

            monkeypatch.setattr(httpx, "post", boom)
            return real.execute(token="t", proposal_id="p", payload_hash="h")

    monkeypatch.setattr("app.services.proposals.GatewayClient", lambda *a, **k: Dead())

    body = api.post(f"/v1/proposals/{pending}/approve", json={}).json()
    assert body["gateway"]["error_code"] == "gateway_unreachable"
    assert "Nothing was sent" in body["gateway"]["error_message"]


# ----------------------------------------------------------------------------------
# Rejection
# ----------------------------------------------------------------------------------


def test_rejecting_requires_a_note_and_is_terminal(api, pending):
    without = api.post(f"/v1/proposals/{pending}/reject", json={"note": ""})
    assert without.status_code == 422

    with_note = api.post(f"/v1/proposals/{pending}/reject", json={"note": "Already paid."})
    assert with_note.status_code == 200
    assert with_note.json()["status"] == "rejected"

    again = api.post(f"/v1/proposals/{pending}/reject", json={"note": "again"})
    assert again.status_code == 409


def test_a_rejected_proposal_cannot_then_be_approved(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/reject", json={"note": "no"})
    response = api.post(f"/v1/proposals/{pending}/approve", json={})
    assert response.status_code == 409


# ----------------------------------------------------------------------------------
# Saving an edit invalidates the prior hash
# ----------------------------------------------------------------------------------


def test_saving_an_edit_rehashes_and_says_so(api, pending):
    before = api.get(f"/v1/proposals/{pending}").json()["payload_hash"]
    body = api.post(
        f"/v1/proposals/{pending}/edit", json={"body": "A different letter entirely."}
    ).json()

    assert body["previous_payload_hash"] == before
    assert body["payload_hash"] != before
    assert "can no longer execute" in body["note"]


def test_an_edit_is_appended_to_the_audit_log(api, pending):
    api.post(f"/v1/proposals/{pending}/edit", json={"body": "Edited."})
    with session_scope() as session:
        events = [row.event for row in session.query(AuditEvent).all()]
    assert "proposal.edited" in events


# ----------------------------------------------------------------------------------
# "Try to send without approval" -- step 4 of the handoff demo
# ----------------------------------------------------------------------------------


def test_the_unapproved_attempt_signs_a_real_token_for_a_pending_proposal(
    api, pending, monkeypatch
):
    """The weak demo sends a forgery and gets a 401. This one signs properly.

    Every cryptographic check passes, and the gateway refuses anyway because it reads the
    status from its own database. That is acceptance criterion 5.
    """
    gateway = stub_into(
        monkeypatch, StubGateway(refusal("not_approved", status=403, failed_check=4))
    )

    body = api.post(f"/v1/proposals/{pending}/attempt-unapproved").json()

    assert body["decision"] == "attempt_unapproved"
    assert body["gateway"]["error_code"] == "not_approved"
    assert body["gateway"]["failed_check"] == 4

    # The token really was valid: it decodes under our secret and names this proposal.
    claims = gateway.calls[0]["claims"]
    assert claims.proposal_id == pending
    assert claims.is_expired() is False


def test_the_unapproved_attempt_leaves_the_proposal_pending_and_unapproved(
    api, pending, monkeypatch
):
    stub_into(monkeypatch, StubGateway(refusal("not_approved", status=403, failed_check=4)))
    api.post(f"/v1/proposals/{pending}/attempt-unapproved")
    with session_scope() as session:
        assert session.get(Proposal, pending).status == "pending"
        assert session.query(Approval).count() == 0, "no approval record was fabricated"


def test_the_probe_refuses_to_target_anything_but_a_pending_proposal(api, pending, monkeypatch):
    """If it could target an approved proposal it would be a way around the approval
    endpoint, which is the hole this system exists to close."""
    from app.approval.probe import ProbeNotPermitted, mint_probe_token_for_demonstration

    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/approve", json={})

    with session_scope() as session:
        proposal = session.get(Proposal, pending)
        assert proposal.status == "approved"
        with pytest.raises(ProbeNotPermitted):
            mint_probe_token_for_demonstration(proposal, actor="x", secret=SECRET)


def test_the_demonstration_can_be_switched_off(api, pending, monkeypatch):
    from app import config as app_config

    monkeypatch.setenv("ENABLE_UNAPPROVED_ATTEMPT_DEMO", "false")
    app_config.settings.cache_clear()
    try:
        response = api.post(f"/v1/proposals/{pending}/attempt-unapproved")
        assert response.status_code == 403
    finally:
        app_config.settings.cache_clear()


# ----------------------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------------------


def test_the_audit_endpoint_is_newest_first_with_the_chain_status(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/approve", json={})

    page = api.get("/v1/audit").json()
    ids = [row["id"] for row in page["events"]]
    assert ids == sorted(ids, reverse=True), "newest first"
    assert page["chain"]["intact"] is True
    assert page["chain"]["length"] == page["total"]


def test_the_audit_log_can_be_filtered_by_actor_and_event(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/approve", json={})

    operator_only = api.get("/v1/audit", params={"actor": "operator"}).json()
    assert operator_only["events"]
    assert all(row["actor"] == "operator" for row in operator_only["events"])

    granted = api.get("/v1/audit", params={"event": "approval.granted"}).json()
    assert len(granted["events"]) == 1


def test_the_whole_approval_path_is_in_the_chain_and_it_verifies(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/approve", json={"note": "ok"})

    events = [row["event"] for row in api.get("/v1/audit", params={"limit": 100}).json()["events"]]
    assert "approval.granted" in events
    assert "approval.token.minted" in events
    assert api.get("/v1/audit/verify").json()["intact"] is True


# ----------------------------------------------------------------------------------
# The four screens render
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/proposals", "/audit", "/outbox"])
def test_every_screen_renders(api, pending, path):
    response = api.get(path)
    assert response.status_code == 200
    assert "<html" in response.text


def test_the_queue_screen_shows_the_rationale_before_the_letter(api, pending):
    """Section 9: "the agent's rationale first, then invoice facts, then ... the letter"."""
    page = api.get("/proposals").text
    rationale_at = page.index("Why this invoice, this tone")
    facts_at = page.index("Invoice facts")
    letter_at = page.index("The letter")
    assert rationale_at < facts_at < letter_at


def test_the_run_screen_footnotes_the_absent_action_chip(api):
    (
        """Section 9: "There is no ACTION chip, and its absence is the point -- worth a one-line
    footnote in the UI itself."""
        ""
    )
    from app.store.repositories import RunStore

    run_id = RunStore.create(goal="g", operator_id="op", params={})
    page = api.get(f"/runs/{run_id}").text
    assert "no ACTION chip" in page
    assert "READ" in page and "DRAFT" in page


def test_the_queue_screen_offers_the_unapproved_send_button(api, pending):
    page = api.get("/proposals").text
    assert "Try to send without approval" in page
    assert f"/ui/proposals/{pending}/attempt-unapproved" in page


def test_the_header_flags_a_non_restricted_stripe_key(api, monkeypatch):
    """The read-only split is a stated deliverable, so the gap is visible, not hidden."""
    from app import config as app_config

    monkeypatch.setenv("STRIPE_API_KEY_READ", "sk_test_not_restricted")
    app_config.settings.cache_clear()
    try:
        assert "read key is not restricted" in api.get("/ui/health").text
    finally:
        app_config.settings.cache_clear()


def test_a_scripted_run_is_badged_so_it_cannot_pass_for_a_model_run(api):
    from app.store.repositories import RunStore

    RunStore.create(goal="[SCRIPTED FIXTURE]", operator_id="op", params={"scripted": True})
    assert "scripted fixture" in api.get("/").text


# ----------------------------------------------------------------------------------
# The HTMX fragments, which are where a decision is actually reported
# ----------------------------------------------------------------------------------
#
# These are the panels a reviewer reads immediately after clicking Approve or Reject, so
# they carry the whole weight of section 6's "the refusal is the feature". Rendering them
# in tests also means a broken template shows up here rather than in front of a client:
# every other screen is server-rendered on a GET, but these four only exist after a POST.


def test_the_panel_after_a_send_lists_the_seven_checks(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    panel = api.post(f"/ui/proposals/{pending}/approve", data={"note": "", "body": ""}).text

    assert "The seven checks that had to pass" in panel
    for number, name in enumerate(
        [
            "hmac_signature_valid",
            "token_not_expired",
            "nonce_unused",
            "proposal_is_approved",
            "approval_matches_approver",
            "payload_hash_matches",
            "idempotency_key_unused",
        ],
        start=1,
    ):
        assert f"{number}. {name}" in panel


def test_the_panel_after_a_refusal_shows_the_code_and_how_far_it_got(api, pending, monkeypatch):
    """Section 6: the code surfaces unchanged, and prominently."""
    stub_into(monkeypatch, StubGateway(refusal("not_approved", status=403, failed_check=4)))
    panel = api.post(f"/ui/proposals/{pending}/approve", data={"note": "", "body": ""}).text

    assert "not_approved" in panel
    assert "nothing was sent" in panel
    assert "refused at check 4" in panel
    assert "Checks that passed before the refusal" in panel
    assert "4. refused here" in panel


def test_the_unapproved_attempt_panel_makes_the_point_of_the_architecture(
    api, pending, monkeypatch
):
    stub_into(monkeypatch, StubGateway(refusal("not_approved", status=403, failed_check=4)))
    panel = api.post(f"/ui/proposals/{pending}/attempt-unapproved").text

    assert "This is the point of the architecture." in panel
    assert "correctly" in panel and "signed" in panel


def test_the_panel_after_an_edit_shows_both_hashes(api, pending):
    panel = api.post(f"/ui/proposals/{pending}/edit", data={"body": "Rewritten by hand."}).text

    assert "Edit saved." in panel
    assert "was" in panel and "now" in panel


def test_the_panel_after_a_rejection_says_it_is_terminal(api, pending):
    panel = api.post(f"/ui/proposals/{pending}/reject", data={"note": "already paid"}).text

    assert "recorded" in panel
    assert "not_approved" in panel


def test_the_verify_chain_fragment_renders_the_answer(api, pending):
    panel = api.get("/ui/audit/verify").text

    assert "intact" in panel
    assert "events recomputed" in panel


def test_the_run_proposal_fragment_renders(api):
    from tests.factories import make_run

    with session_scope() as session:
        run = make_run(session)
        run_id = run.id
        make_proposal(session, run=run, status="pending")

    panel = api.get(f"/ui/runs/{run_id}/proposals").text
    assert "awaiting approval" in panel
    assert "review" in panel


# ----------------------------------------------------------------------------------
# The refusal is answered with the gateway's own status code
# ----------------------------------------------------------------------------------


def test_an_unapproved_attempt_answers_with_403_not_200(api, pending, monkeypatch):
    """Acceptance criterion 5 is worded as an HTTP fact, and a reviewer checks it with curl.

    Answering 200 with the refusal buried in the body would read as a system that quietly
    swallowed it.
    """
    stub_into(monkeypatch, StubGateway(refusal("not_approved", status=403, failed_check=4)))

    response = api.post(f"/v1/proposals/{pending}/attempt-unapproved")

    assert response.status_code == 403
    assert response.json()["gateway"]["error_code"] == "not_approved"


def test_an_approval_the_gateway_refuses_answers_with_the_gateways_status(
    api, pending, monkeypatch
):
    stub_into(monkeypatch, StubGateway(refusal("payload_modified", status=409, failed_check=6)))

    response = api.post(f"/v1/proposals/{pending}/approve", json={})

    assert response.status_code == 409
    assert response.json()["gateway"]["error_code"] == "payload_modified"
    with session_scope() as session:
        assert session.get(Proposal, pending).status == "approved", (
            "the human decision still stands; the gateway declining does not unmake it"
        )


def test_a_successful_approval_answers_200(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    assert api.post(f"/v1/proposals/{pending}/approve", json={}).status_code == 200


# ----------------------------------------------------------------------------------
# Section 11 step 6: reject one, then attempt execution, and show the refusal
# ----------------------------------------------------------------------------------


def test_a_rejected_proposal_can_be_attempted_and_the_gateway_refuses_it(api, pending, monkeypatch):
    """The attempt has to reach the gateway to be worth showing.

    The probe's restriction is not "pending only" -- it is "never the one status that could
    execute". A rejected proposal is safe to attempt and produces exactly the refusal
    section 11 step 6 asks for.
    """
    gateway = stub_into(
        monkeypatch, StubGateway(refusal("not_approved", status=403, failed_check=4))
    )
    api.post(f"/v1/proposals/{pending}/reject", json={"note": "customer disputes it"})

    response = api.post(f"/v1/proposals/{pending}/attempt-unapproved")

    assert response.status_code == 403
    assert response.json()["gateway"]["error_code"] == "not_approved"
    assert len(gateway.calls) == 1, "the request really did reach the gateway"
    with session_scope() as session:
        assert session.get(Proposal, pending).status == "rejected"


def test_the_probe_still_refuses_the_one_status_that_could_execute(api, pending, monkeypatch):
    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/approve", json={})

    from app.approval.probe import ProbeNotPermitted, mint_probe_token_for_demonstration

    with session_scope() as session:
        proposal = session.get(Proposal, pending)
        assert proposal.status == "approved"
        with pytest.raises(ProbeNotPermitted, match="never target an approved proposal"):
            mint_probe_token_for_demonstration(proposal, actor="x", secret=SECRET)


def test_attempting_a_proposal_that_does_not_exist_is_a_404(api):
    assert api.post("/v1/proposals/nope/attempt-unapproved").status_code == 404


# ----------------------------------------------------------------------------------
# The dashboard's counters (section 9), and the fields behind them
# ----------------------------------------------------------------------------------


def test_the_counters_are_the_four_section_9_asks_for(api, pending, monkeypatch):
    """ "Counters: overdue invoices, pending proposals, approved today, sent today."

    The first version shipped all-time totals and no overdue counter, which reads as a
    different thing entirely: "3 sent" says nothing about whether anything happened today.
    """
    from app.api import queries

    counters = queries.proposal_counters()
    for key in ("pending", "approved_today", "sent_today"):
        assert key in counters, f"section 9 needs {key}"

    assert counters["pending"] == 1
    assert counters["approved_today"] == 0
    assert counters["sent_today"] == 0

    monkeypatch.setattr(queries, "overdue_invoice_count", lambda **_: 7)
    page = api.get("/").text
    assert "Overdue invoices" in page
    assert "Approved today" in page
    assert "Sent today" in page


def test_approving_moves_approved_today_but_not_a_stale_yesterday(api, pending, monkeypatch):
    from datetime import timedelta

    from app.api import queries
    from shared.clock import now_utc
    from shared.models import Approval as ApprovalModel

    stub_into(monkeypatch, StubGateway())
    api.post(f"/v1/proposals/{pending}/approve", json={})
    assert queries.proposal_counters()["approved_today"] == 1

    # Back-date it and it should drop out of "today".
    with session_scope() as session:
        session.query(ApprovalModel).one().decided_at = now_utc() - timedelta(days=2)
    assert queries.proposal_counters()["approved_today"] == 0


def test_sent_today_counts_executions_not_outbox_rows(api, pending, monkeypatch):
    """With EMAIL_ADAPTER=smtp or resend there is no outbox row at all.

    Counting outbox rows made the counter read zero while letters were going out.
    """
    from app.api import queries
    from shared.clock import now_utc
    from shared.models import Execution

    with session_scope() as session:
        session.add(
            Execution(
                proposal_id=pending,
                idempotency_key="k-today",
                status="succeeded",
                executed_at=now_utc(),
            )
        )

    counters = queries.proposal_counters()
    assert counters["sent_today"] == 1
    assert counters["sent_total"] == 0, "no outbox row exists, and that is the point"


def test_the_overdue_counter_degrades_to_none_rather_than_failing_the_page(api, monkeypatch):
    """A counter must never 500 the dashboard."""
    from app.api import queries
    from app.stripe_client.read import StripeReadError

    def unavailable(*_a, **_k):
        raise StripeReadError("Stripe is down", code="stripe_error")

    monkeypatch.setattr("app.stripe_client.read.StripeReadClient.__init__", unavailable)
    queries._OVERDUE_CACHE.update({"count": None, "at": 0.0})

    assert queries.overdue_invoice_count(force=True) is None
    assert api.get("/").status_code == 200


def test_the_overdue_counter_is_cached_so_the_dashboard_does_not_hammer_stripe(api, monkeypatch):
    from app.api import queries

    calls = {"n": 0}

    class Counting:
        def __init__(self, *_a, **_k):
            calls["n"] += 1

        def list_overdue_invoices(self, **_k):
            return [{"id": "in_1"}, {"id": "in_2"}]

    monkeypatch.setattr("app.stripe_client.read.StripeReadClient", Counting)
    queries._OVERDUE_CACHE.update({"count": None, "at": 0.0})

    assert queries.overdue_invoice_count(force=True) == 2
    assert queries.overdue_invoice_count() == 2
    assert queries.overdue_invoice_count() == 2
    assert calls["n"] == 1, "three reads, one Stripe call"


# ----------------------------------------------------------------------------------
# Payment history: declared in the API and the section 9 layout, and it used to be empty
# ----------------------------------------------------------------------------------


def _run_with_history(customer_id: str, history: list[dict]) -> tuple[str, str]:
    """A run whose transcript contains a get_payment_history call and its result."""
    from app.store.repositories import RunStore

    run_id = RunStore.create(goal="g", operator_id="op@x", params={})
    store = RunStore(run_id)
    store.append_step(
        type="tool_call",
        tool_name="get_payment_history",
        payload={"tool_use_id": "t1", "arguments": {"customer_id": customer_id}},
    )
    store.append_step(
        type="tool_result",
        tool_name="get_payment_history",
        payload={"tool_use_id": "t1", "is_error": False, "result": {"history": history}},
    )
    return run_id, "t1"


HISTORY = [
    {
        "invoice_id": "in_old",
        "number": "INV-0900",
        "status": "paid",
        "amount_due_display": "$1,200.00",
        "due_date": "2025-09-01",
        "outcome": "paid on time",
    }
]


def test_the_proposal_detail_shows_the_history_the_agent_actually_read(api):
    from shared.models import Run

    run_id, _ = _run_with_history("cus_acme", HISTORY)
    with session_scope() as session:
        run = session.get(Run, run_id)
        payload = letter_payload()
        payload["customer_id"] = "cus_acme"
        proposal = make_proposal(session, run=run, payload=payload)
        proposal_id = proposal.id

    detail = api.get(f"/v1/proposals/{proposal_id}").json()

    assert detail["payment_history"] == HISTORY
    assert "paid on time" in api.get(f"/proposals?selected={proposal_id}").text


def test_history_from_a_different_customer_is_not_attributed_to_this_one(api):
    """The transcript may hold several customers' histories. Pair them by tool_use_id."""
    from shared.models import Run

    run_id, _ = _run_with_history("cus_someone_else", HISTORY)
    with session_scope() as session:
        run = session.get(Run, run_id)
        payload = letter_payload()
        payload["customer_id"] = "cus_acme"
        proposal_id = make_proposal(session, run=run, payload=payload).id

    assert api.get(f"/v1/proposals/{proposal_id}").json()["payment_history"] == []


def test_a_proposal_whose_agent_never_looked_says_so(api, pending):
    """An empty panel is ambiguous; "it did not look" is information."""
    detail = api.get(f"/v1/proposals/{pending}").json()
    assert detail["payment_history"] == []
    assert "did not look up" in api.get(f"/proposals?selected={pending}").text


def test_a_failed_history_call_is_not_shown_as_history(api):
    from app.store.repositories import RunStore
    from shared.models import Run

    run_id = RunStore.create(goal="g", operator_id="op@x", params={})
    store = RunStore(run_id)
    store.append_step(
        type="tool_call",
        tool_name="get_payment_history",
        payload={"tool_use_id": "t1", "arguments": {"customer_id": "cus_acme"}},
    )
    store.append_step(
        type="tool_result",
        tool_name="get_payment_history",
        payload={"tool_use_id": "t1", "is_error": True, "result": {"error": "stripe_error"}},
    )
    with session_scope() as session:
        run = session.get(Run, run_id)
        payload = letter_payload()
        payload["customer_id"] = "cus_acme"
        proposal_id = make_proposal(session, run=run, payload=payload).id

    assert api.get(f"/v1/proposals/{proposal_id}").json()["payment_history"] == []


def test_history_is_paired_by_order_not_by_tool_use_id(api):
    """The bug this fixed: three customers, three calls, all carrying `toolu_0`.

    Keying the lookup on tool_use_id collapsed them to the last one, so every proposal but
    one showed an empty history panel. The loop already persists a call immediately before
    its result, so sequence order is the stronger signal.
    """
    from app.store.repositories import RunStore
    from shared.models import Run

    run_id = RunStore.create(goal="g", operator_id="op@x", params={})
    store = RunStore(run_id)
    wanted = [
        {
            "invoice_id": "in_w",
            "status": "paid",
            "amount_due_display": "$1.00",
            "outcome": "paid on time",
            "number": "INV-W",
            "due_date": "2025-01-01",
        }
    ]
    other = [
        {
            "invoice_id": "in_o",
            "status": "open",
            "amount_due_display": "$2.00",
            "outcome": "still unpaid",
            "number": "INV-O",
            "due_date": "2025-01-01",
        }
    ]

    # Every call deliberately reuses the SAME id, as the old scripted helper did.
    for customer, history in [("cus_first", other), ("cus_target", wanted), ("cus_last", other)]:
        store.append_step(
            type="tool_call",
            tool_name="get_payment_history",
            payload={"tool_use_id": "toolu_0", "arguments": {"customer_id": customer}},
        )
        store.append_step(
            type="tool_result",
            tool_name="get_payment_history",
            payload={"tool_use_id": "toolu_0", "is_error": False, "result": {"history": history}},
        )

    with session_scope() as session:
        run = session.get(Run, run_id)
        payload = letter_payload()
        payload["customer_id"] = "cus_target"
        proposal_id = make_proposal(session, run=run, payload=payload).id

    assert api.get(f"/v1/proposals/{proposal_id}").json()["payment_history"] == wanted


def test_scripted_tool_call_ids_are_unique_across_turns():
    """The helper was minting `toolu_0` in every turn, which the real API never would."""
    from app.agent.llm import turn

    first = turn(tools=[("get_customer", {}), ("get_invoice", {})])
    second = turn(tools=[("get_customer", {})])
    ids = [c.id for c in first.tool_calls] + [c.id for c in second.tool_calls]
    assert len(set(ids)) == len(ids), f"colliding scripted tool ids: {ids}"
