"""The agent loop (brief section 4).

Run against a scripted model, which is what makes the loop's guarantees checkable without a
key: the caps, the clean failure with a partial transcript, the persistence of every step in
order, and the fact that a run ends in `awaiting_approval` and never sends anything.

What these tests deliberately do NOT assert is a particular sequence of tool calls. Section
1: "do not hardcode the sequence... Let the model choose... That variability is the proof."
The loop must work whichever order the model picks, so the scripts here use several.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.agent.llm import LLMUnavailable, ScriptedLLM, turn
from app.agent.loop import RunOutcome, run_agent
from app.agent.tools.registry import build_registry
from app.config import AppSettings
from app.store.repositories import RunStore
from shared.db import session_scope
from shared.models import AuditEvent, OutboxMessage, Proposal, Run, RunStep
from tests.test_agent_tools import FakeReader, invoice

GOOD_BODY = (
    "Dear Acme Industries,\n\n"
    "Invoice INV-1001 for $250.00, originally due on 2026-08-01, is now 9 days past due.\n\n"
    "You can settle it here: https://invoice.stripe.com/i/test_1001\n\n"
    "If this is in error, reply to this email and we will look into it.\n\n"
    "Kind regards,\nServicia Collections"
)


def draft_args(**overrides: Any) -> dict[str, Any]:
    base = {
        "invoice_id": "in_1001",
        "subject": "Invoice INV-1001 is 9 days past due",
        "body": GOOD_BODY,
        "tone": "friendly",
        "rationale": "Nine days late with a clean payment history, so a courteous reminder.",
    }
    base.update(overrides)
    return base


LIST_ARGS = {"min_days_overdue": 1, "limit": 25, "min_amount_cents": None}


def settings(**overrides: Any) -> AppSettings:
    values = {
        "stripe_api_key_read": "rk_test_x",
        "anthropic_api_key": "sk-ant-x",
        "approval_signing_secret": "x" * 64,
        "max_tool_calls_per_run": 25,
        "max_proposals_per_run": 10,
        "proposal_ttl_hours": 72,
        "operator_id": "operator@servicia.ai",
    }
    values.update(overrides)
    return AppSettings(**values)


@pytest.fixture()
def harness(db_path):
    """A run, a store, a fake Stripe reader and a registry, all wired together."""

    def build(invoices: list[dict[str, Any]] | None = None, **setting_overrides):
        config = settings(**setting_overrides)
        run_id = RunStore.create(
            goal="Collect overdue invoices",
            operator_id=config.operator_id,
            params={"min_days_overdue": 1, "max_proposals": config.max_proposals_per_run},
        )
        store = RunStore(run_id)
        reader = FakeReader(invoices)
        registry = build_registry(
            reader=reader,
            store=store,
            max_proposals=config.max_proposals_per_run,
            ttl_hours=config.proposal_ttl_hours,
        )
        return run_id, store, registry, reader, config

    return build


def drive(harness_build, turns, *, invoices=None, **setting_overrides) -> tuple[RunOutcome, Any]:
    run_id, store, registry, _reader, config = harness_build(invoices, **setting_overrides)
    llm = ScriptedLLM(turns)
    outcome = run_agent(
        run_id=run_id,
        goal="Collect overdue invoices",
        llm=llm,
        registry=registry,
        store=store,
        settings=config,
    )
    return outcome, llm


def steps_of(run_id: str) -> list[RunStep]:
    with session_scope() as session:
        return list(
            session.execute(
                select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq)
            ).scalars()
        )


# ----------------------------------------------------------------------------------
# The happy path
# ----------------------------------------------------------------------------------


def test_a_run_that_drafts_one_letter_ends_awaiting_approval(harness):
    outcome, _ = drive(
        harness,
        [
            turn(
                thinking=["Acme is nine days late. Let me look at the overdue list."],
                tools=[("list_overdue_invoices", LIST_ARGS)],
            ),
            turn(tools=[("get_payment_history", {"customer_id": "cus_acme", "limit": None})]),
            turn(
                thinking=["Clean history, so a courteous reminder rather than a firm notice."],
                tools=[("propose_collection_letter", draft_args())],
            ),
            turn(text=["I drafted one friendly reminder for Acme Industries."]),
        ],
    )

    assert outcome.status == "awaiting_approval"
    assert outcome.proposals == 1
    assert outcome.tool_calls == 3
    assert "friendly reminder" in outcome.summary


def test_nothing_is_ever_sent_by_a_run(harness):
    """The agent's terminal capability is a database row. There is no send path."""
    drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(text=["Done."]),
        ],
    )
    with session_scope() as session:
        assert session.query(OutboxMessage).count() == 0
        assert session.query(Proposal).one().status == "pending"


def test_a_run_that_proposes_nothing_completes_rather_than_waiting(harness):
    """An empty queue is not something to await approval on."""
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(
                text=[
                    "Every overdue invoice is only days old and all customers pay on "
                    "time. I propose nothing today."
                ]
            ),
        ],
    )
    assert outcome.status == "completed"
    assert outcome.proposals == 0


def test_the_order_of_tool_calls_is_the_models_choice(harness):
    """Two different orders, both must work. The loop prescribes nothing.

    The two runs use different invoices deliberately: "one pending proposal per invoice"
    holds across runs, not merely within one, so reusing the invoice would collide.
    """
    history_first, _ = drive(
        harness,
        [
            turn(tools=[("get_payment_history", {"customer_id": "cus_acme", "limit": None})]),
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(text=["Done."]),
        ],
    )
    straight_to_it, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args(invoice_id="in_2002"))]),
            turn(text=["Done."]),
        ],
        invoices=[invoice(id="in_2002")],
    )
    assert history_first.status == straight_to_it.status == "awaiting_approval"
    assert history_first.proposals == straight_to_it.proposals == 1


def test_parallel_tool_calls_in_one_turn_are_all_executed_and_answered_together(harness):
    """All results for one assistant turn go back in a single user message."""
    outcome, llm = drive(
        harness,
        [
            turn(
                tools=[
                    ("list_overdue_invoices", LIST_ARGS),
                    ("get_customer", {"customer_id": "cus_acme"}),
                ]
            ),
            turn(text=["Looked at both."]),
        ],
    )
    assert outcome.tool_calls == 2
    final_request = llm.requests[-1]["messages"]
    results = final_request[-1]["content"]
    assert isinstance(results, list) and len(results) == 2
    assert all(block["type"] == "tool_result" for block in results)


# ----------------------------------------------------------------------------------
# The transcript is a deliverable
# ----------------------------------------------------------------------------------


def test_every_tool_call_and_result_is_persisted_in_order(harness):
    run_id, store, registry, _reader, config = harness(None)
    llm = ScriptedLLM(
        [
            turn(
                thinking=["First, the overdue list."], tools=[("list_overdue_invoices", LIST_ARGS)]
            ),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(text=["One letter drafted."]),
        ]
    )
    run_agent(run_id=run_id, goal="g", llm=llm, registry=registry, store=store, settings=config)

    recorded = [(step.seq, step.type, step.tool_name) for step in steps_of(run_id)]
    assert recorded == [
        (1, "thought", None),
        (2, "tool_call", "list_overdue_invoices"),
        (3, "tool_result", "list_overdue_invoices"),
        (4, "tool_call", "propose_collection_letter"),
        (5, "tool_result", "propose_collection_letter"),
        (6, "message", None),
    ]


def test_each_tool_step_carries_its_read_or_draft_classification(harness):
    run_id, store, registry, _reader, config = harness(None)
    run_agent(
        run_id=run_id,
        goal="g",
        llm=ScriptedLLM(
            [
                turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
                turn(tools=[("propose_collection_letter", draft_args())]),
                turn(text=["Done."]),
            ]
        ),
        registry=registry,
        store=store,
        settings=config,
    )
    classes = {
        step.tool_name: step.payload["tool_class"]
        for step in steps_of(run_id)
        if step.type == "tool_call"
    }
    assert classes == {
        "list_overdue_invoices": "READ",
        "propose_collection_letter": "DRAFT",
    }
    assert "ACTION" not in classes.values()


def test_reasoning_is_persisted_as_prose_for_the_transcript(harness):
    """Section 9 renders reasoning as prose. It has to be captured to be rendered."""
    run_id, store, registry, _reader, config = harness(None)
    run_agent(
        run_id=run_id,
        goal="g",
        llm=ScriptedLLM(
            [
                turn(
                    thinking=["Acme is nine days late with a clean history."],
                    tools=[("list_overdue_invoices", LIST_ARGS)],
                ),
                turn(text=["Nothing to propose."]),
            ]
        ),
        registry=registry,
        store=store,
        settings=config,
    )
    thoughts = [step.payload["text"] for step in steps_of(run_id) if step.type == "thought"]
    assert thoughts == ["Acme is nine days late with a clean history."]


def test_the_assistant_turn_is_echoed_back_unchanged(harness):
    """Thinking blocks must be returned to the model verbatim, not reconstructed."""
    _outcome, llm = drive(
        harness,
        [
            turn(thinking=["A thought."], tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(text=["Done."]),
        ],
    )
    assistant_turns = [
        message for message in llm.requests[-1]["messages"] if message["role"] == "assistant"
    ]
    blocks = assistant_turns[0]["content"]
    assert any(block["type"] == "thinking" for block in blocks)


# ----------------------------------------------------------------------------------
# Guardrails: the caps of section 4
# ----------------------------------------------------------------------------------


def test_exceeding_the_tool_call_cap_fails_the_run_cleanly(harness):
    (
        """"Maximum 25 tool calls per run. Exceeding it fails the run cleanly with a partial
    transcript."""
        ""
    )
    outcome, _ = drive(
        harness,
        [turn(tools=[("list_overdue_invoices", LIST_ARGS)]) for _ in range(6)],
        max_tool_calls_per_run=3,
    )

    assert outcome.status == "failed"
    assert outcome.tool_calls == 3, "the cap is the cap"
    assert "limit of 3 tool calls" in outcome.error

    with session_scope() as session:
        assert session.get(Run, outcome.run_id).status == "failed"
    kept = steps_of(outcome.run_id)
    assert len(kept) >= 6, "the partial transcript survives"
    assert kept[-1].type == "message"
    assert "Run stopped" in kept[-1].payload["text"]


def test_a_capped_run_keeps_the_proposals_it_already_made(harness):
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(tools=[("get_customer", {"customer_id": "cus_acme"})]),
            turn(tools=[("get_customer", {"customer_id": "cus_acme"})]),
        ],
        max_tool_calls_per_run=3,
    )
    assert outcome.status == "failed"
    assert outcome.proposals == 1
    with session_scope() as session:
        assert session.query(Proposal).count() == 1


def test_the_proposal_cap_is_refused_by_the_tool_not_the_prompt(harness):
    """A cap breach is a correctable tool error, so the run continues and summarises."""
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(tools=[("propose_collection_letter", draft_args(invoice_id="in_1001"))]),
            turn(text=["I hit the proposal limit after one letter."]),
        ],
        max_proposals_per_run=1,
    )

    assert outcome.status == "awaiting_approval"
    assert outcome.proposals == 1
    errors = [
        step.payload["result"]
        for step in steps_of(outcome.run_id)
        if step.type == "tool_result" and step.payload.get("is_error")
    ]
    assert errors and errors[0]["error"] == "proposal_cap_reached"


def test_a_second_proposal_for_the_same_invoice_is_refused_by_the_store(harness):
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(tools=[("propose_collection_letter", draft_args(tone="firm"))]),
            turn(text=["Only one letter per invoice."]),
        ],
    )
    assert outcome.proposals == 1
    errors = [
        step.payload["result"]
        for step in steps_of(outcome.run_id)
        if step.type == "tool_result" and step.payload.get("is_error")
    ]
    assert errors[0]["error"] == "duplicate_pending_proposal"


# ----------------------------------------------------------------------------------
# Refusals the agent can recover from
# ----------------------------------------------------------------------------------


def test_a_letter_that_breaks_the_guardrails_is_refused_and_can_be_rewritten(harness):
    """The whole point of a correctable error: the agent fixes it inside the same run."""
    bad = draft_args(body=GOOD_BODY + "\n\nWe will commence legal action.")
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", bad)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(text=["Rewrote the letter without the threat."]),
        ],
    )

    assert outcome.status == "awaiting_approval"
    assert outcome.proposals == 1, "the rejected draft was not stored"
    results = [step.payload for step in steps_of(outcome.run_id) if step.type == "tool_result"]
    rejected = [r for r in results if r.get("is_error")]
    assert rejected[0]["result"]["error"] == "letter_rejected"
    assert any(p["code"] == "legal_action" for p in rejected[0]["result"]["problems"])


def test_proposing_for_an_undeliverable_invoice_is_refused(harness):
    """The seeded Delta Fabrication case: no email, so no letter."""
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args(invoice_id="in_nomail"))]),
            turn(text=["Skipped the invoice with no email address."]),
        ],
        invoices=[
            invoice(
                id="in_nomail",
                customer_email=None,
                deliverable=False,
                not_deliverable_reason="This customer has no email address on file.",
            )
        ],
    )
    assert outcome.proposals == 0
    assert outcome.status == "completed"
    errors = [
        step.payload["result"]
        for step in steps_of(outcome.run_id)
        if step.type == "tool_result" and step.payload.get("is_error")
    ]
    assert errors[0]["error"] == "not_deliverable"


def test_proposing_for_an_invoice_never_read_is_refused(harness):
    """Facts are injected by the tool layer. An unread invoice has no facts to inject."""
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("propose_collection_letter", draft_args(invoice_id="in_unknown"))]),
            turn(text=["I need to read the invoice first."]),
        ],
    )
    assert outcome.proposals == 0
    errors = [
        step.payload["result"]
        for step in steps_of(outcome.run_id)
        if step.type == "tool_result" and step.payload.get("is_error")
    ]
    assert errors[0]["error"] == "invoice_not_read"


def test_a_proposal_with_no_rationale_is_refused(harness):
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args(rationale="   "))]),
            turn(text=["Added a rationale."]),
        ],
    )
    assert outcome.proposals == 0
    errors = [
        step.payload["result"]
        for step in steps_of(outcome.run_id)
        if step.type == "tool_result" and step.payload.get("is_error")
    ]
    assert errors[0]["error"] == "rationale_required"


def test_an_unknown_tool_does_not_end_the_run(harness):
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("send_collection_letter", {"invoice_id": "in_1001"})]),
            turn(text=["There is no such tool. I can only propose."]),
        ],
    )
    assert outcome.status == "completed"
    errors = [
        step.payload["result"]
        for step in steps_of(outcome.run_id)
        if step.type == "tool_result" and step.payload.get("is_error")
    ]
    assert errors[0]["error"] == "unknown_tool"


# ----------------------------------------------------------------------------------
# Failures that end the run
# ----------------------------------------------------------------------------------


def test_an_unreachable_model_fails_the_run_with_a_readable_reason(harness):
    run_id, store, registry, _reader, config = harness(None)

    class Dead:
        def create(self, **_kwargs):
            raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")

    outcome = run_agent(
        run_id=run_id, goal="g", llm=Dead(), registry=registry, store=store, settings=config
    )
    assert outcome.status == "failed"
    assert "ANTHROPIC_API_KEY" in outcome.error
    with session_scope() as session:
        assert session.get(Run, run_id).status == "failed"


def test_an_unexpected_exception_never_leaves_a_run_running(harness):
    run_id, store, registry, _reader, config = harness(None)

    class Exploding:
        def create(self, **_kwargs):
            raise ZeroDivisionError("boom")

    outcome = run_agent(
        run_id=run_id,
        goal="g",
        llm=Exploding(),
        registry=registry,
        store=store,
        settings=config,
    )
    assert outcome.status == "failed"
    assert "ZeroDivisionError" in outcome.error
    with session_scope() as session:
        assert session.get(Run, run_id).status == "failed"


def test_a_model_that_never_stops_is_bounded_by_max_turns(harness):
    outcome, _ = drive(
        harness,
        [
            turn(
                text=[f"thinking out loud {index}"],
                stop_reason="tool_use",
                tools=[("get_customer", {"customer_id": "cus_acme"})],
            )
            for index in range(60)
        ],
        max_tool_calls_per_run=100,
    )
    assert outcome.status == "failed"


# ----------------------------------------------------------------------------------
# The record
# ----------------------------------------------------------------------------------


def test_a_run_is_recorded_in_the_audit_log_from_start_to_finish(harness):
    outcome, _ = drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(text=["Done."]),
        ],
    )
    with session_scope() as session:
        events = [
            row.event
            for row in session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars()
        ]
    assert "run.started" in events
    assert "proposal.created" in events
    assert "run.completed" in events

    from shared.audit import verify_chain

    with session_scope() as session:
        assert verify_chain(session).intact is True


def test_the_stored_payload_holds_the_letter_and_the_facts_it_was_built_from(harness):
    (
        """Section 8: "the proposal record stores the letter as text plus the structured facts
    it was built from, so the operator can see both."""
        ""
    )
    drive(
        harness,
        [
            turn(tools=[("list_overdue_invoices", LIST_ARGS)]),
            turn(tools=[("propose_collection_letter", draft_args())]),
            turn(text=["Done."]),
        ],
    )
    with session_scope() as session:
        proposal = session.query(Proposal).one()
    payload = proposal.payload
    assert payload["body"] == GOOD_BODY
    assert payload["amount_display"] == "$250.00"
    assert payload["invoice_number"] == "INV-1001"
    assert payload["hosted_invoice_url"] == "https://invoice.stripe.com/i/test_1001"
    assert proposal.payload_hash.startswith("sha256:")
    assert proposal.rationale


def test_the_run_records_the_operators_own_parameters(harness):
    run_id, _store, _registry, _reader, _config = harness(None)
    with session_scope() as session:
        run = session.get(Run, run_id)
    assert run.params["min_days_overdue"] == 1
    assert run.operator_id == "operator@servicia.ai"
