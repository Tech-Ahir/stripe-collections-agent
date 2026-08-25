"""What the transcript screen shows for a tool call.

A call carries ids: `get_payment_history` is called with `cus_V88U8Ng76gSGrl`, and a real run
makes seven of those in a row. On screen they were indistinguishable. The names are already
in the transcript, because `list_overdue_invoices` returned them, so the read model resolves
them from the run's own results rather than going back to Stripe.

Nothing here changes what the agent did or what is stored -- these are read-model and
template tests. The point of each one is that the screen says something true.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.api import queries
from shared.db import session_scope
from shared.models import Run, RunStep

SECRET = "x" * 64

LISTING = {
    "count": 2,
    "invoices": [
        {
            "id": "in_ferrolux",
            "number": "JZBOGW8K-0001",
            "customer_id": "cus_ferrolux",
            "customer_name": "Ferrolux Metals",
            "amount_due_display": "$23,400.00",
            "days_overdue": 62,
        },
        {
            "id": "in_acme",
            "number": "EUXUOGRQ-0002",
            "customer_id": "cus_acme",
            "customer_name": "Acme Industries",
            "amount_due_display": "$250.00",
            "days_overdue": 3,
        },
    ],
}


@pytest.fixture()
def api(tmp_path):
    from app import config as app_config
    from shared import db as db_module

    values = {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'view.db').as_posix()}",
        "APPROVAL_SIGNING_SECRET": SECRET,
        "STRIPE_API_KEY_READ": "",
        "ANTHROPIC_API_KEY": "",
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


def _run_with_steps(steps: list[tuple[str, str | None, dict]]) -> str:
    with session_scope() as session:
        run = Run(goal="Chase the overdue ones", status="awaiting_approval", operator_id="op")
        session.add(run)
        session.flush()
        for seq, (type_, tool, payload) in enumerate(steps, start=1):
            session.add(
                RunStep(run_id=run.id, seq=seq, type=type_, tool_name=tool, payload=payload)
            )
        return run.id


def test_a_customer_id_in_a_call_is_shown_as_the_customer_name(api):
    run_id = _run_with_steps(
        [
            ("tool_call", "list_overdue_invoices", {"tool_class": "READ", "arguments": {}}),
            ("tool_result", "list_overdue_invoices", {"result": LISTING}),
            (
                "tool_call",
                "get_payment_history",
                {"tool_class": "READ", "arguments": {"customer_id": "cus_ferrolux", "limit": 10}},
            ),
        ]
    )

    detail = queries.get_run(run_id)
    assert detail["transcript"][2]["about"] == {"customer": "Ferrolux Metals"}

    page = api.get(f"/runs/{run_id}").text
    assert "Checked payment history" in page, "the plain name"
    assert "get_payment_history" in page, "and the identifier, still matchable against the code"
    assert "Ferrolux Metals" in page


def test_a_draft_call_says_which_letter_it_wrote(api):
    run_id = _run_with_steps(
        [
            ("tool_call", "list_overdue_invoices", {"tool_class": "READ", "arguments": {}}),
            ("tool_result", "list_overdue_invoices", {"result": LISTING}),
            (
                "tool_call",
                "propose_collection_letter",
                {
                    "tool_class": "DRAFT",
                    "arguments": {
                        "invoice_id": "in_ferrolux",
                        "tone": "final",
                        "subject": "Final Notice",
                        "body": "Dear Ferrolux Metals, ...",
                    },
                },
            ),
        ]
    )

    about = queries.get_run(run_id)["transcript"][2]["about"]
    assert about == {
        "customer": "Ferrolux Metals",
        "invoice": "JZBOGW8K-0001",
        "amount": "$23,400.00",
        "tone": "final",
    }

    page = api.get(f"/runs/{run_id}").text
    assert "Wrote a letter" in page
    assert "JZBOGW8K-0001" in page


def test_an_id_the_run_never_saw_is_still_shown_rather_than_dropped(api):
    """A resolver that hides what it cannot name would be worse than one that shows the id."""
    run_id = _run_with_steps(
        [
            (
                "tool_call",
                "get_payment_history",
                {"tool_class": "READ", "arguments": {"customer_id": "cus_unknown"}},
            ),
        ]
    )

    assert queries.get_run(run_id)["transcript"][0]["about"] == {"customer": "cus_unknown"}
    assert "cus_unknown" in api.get(f"/runs/{run_id}").text


def test_a_duplicate_refusal_reads_as_the_harmless_thing_it_is(api):
    """Five of these in a row is the normal shape of a re-run, not five failures."""
    run_id = _run_with_steps(
        [
            (
                "tool_result",
                "propose_collection_letter",
                {
                    "is_error": True,
                    "result": {
                        "error": "duplicate_pending_proposal",
                        "message": "A pending proposal already exists for invoice in_ferrolux.",
                        "recoverable": True,
                    },
                },
            ),
        ]
    )

    page = api.get(f"/runs/{run_id}").text
    assert "already queued" in page
    assert "duplicate_pending_proposal" in page, "the code stays on screen"


def test_an_unrecognised_refusal_is_not_softened_into_reassuring_words(api):
    run_id = _run_with_steps(
        [
            (
                "tool_result",
                "propose_collection_letter",
                {"is_error": True, "result": {"error": "something_new", "message": "boom"}},
            ),
        ]
    )

    page = api.get(f"/runs/{run_id}").text
    assert "the tool refused it" in page
    assert "something_new" in page


def test_the_closing_summary_renders_its_table(api):
    run_id = _run_with_steps(
        [
            (
                "message",
                None,
                {
                    "kind": "summary",
                    "text": "## Summary\n\n| Invoice | Tone |\n|---|---|\n| INV-1 | final |",
                },
            ),
        ]
    )

    page = api.get(f"/runs/{run_id}").text
    assert "<table" in page
    assert "| INV-1 |" not in page, "no raw pipes reach the operator"


def test_the_action_chip_footnote_survives_the_redesign(api):
    """Section 9 asks for it. It moved to the foot of the panel; it did not go away."""
    run_id = _run_with_steps(
        [("tool_call", "get_invoice", {"tool_class": "READ", "arguments": {}})]
    )

    page = api.get(f"/runs/{run_id}").text
    assert "no ACTION chip" in page
    assert "READ" in page and "DRAFT" in page


def test_a_result_is_folded_into_the_call_it_answers(api):
    """Seven lookups should be seven rows, not fourteen."""
    from app.web.routes import _fold_results_into_calls

    run_id = _run_with_steps(
        [
            ("tool_call", "get_payment_history", {"tool_class": "READ", "arguments": {}}),
            ("tool_result", "get_payment_history", {"result": {"summary": {}}}),
            ("tool_call", "get_invoice", {"tool_class": "READ", "arguments": {}}),
            ("tool_result", "get_invoice", {"result": {"invoice": {}}}),
        ]
    )

    published = queries.get_run(run_id)["transcript"]
    assert len(published) == 4, "/v1 still publishes every step the run recorded"

    rendered = _fold_results_into_calls(published)
    assert len(rendered) == 2
    assert all(row["type"] == "tool_call" and "answer" in row for row in rendered)


def test_a_result_with_no_call_above_it_still_renders(api):
    """A transcript read from the middle -- the SSE `after` cursor does exactly this."""
    from app.web.routes import _fold_results_into_calls

    orphan = [{"type": "tool_result", "tool_name": "get_invoice", "payload": {"result": {}}}]
    assert _fold_results_into_calls(orphan) == orphan


def test_a_second_result_for_the_same_tool_is_not_swallowed(api):
    """Two calls to one tool, then two results, must not collapse into one row."""
    from app.web.routes import _fold_results_into_calls

    steps = [
        {"type": "tool_call", "tool_name": "get_payment_history", "payload": {}},
        {"type": "tool_result", "tool_name": "get_payment_history", "payload": {}},
        {"type": "tool_result", "tool_name": "get_payment_history", "payload": {}},
    ]
    rendered = _fold_results_into_calls(steps)
    assert len(rendered) == 2, "the unpaired result keeps its own row rather than vanishing"


def test_a_later_result_cannot_erase_a_name_an_earlier_one_established(api):
    """`get_payment_history` returns invoice rows with no customer name on them.

    Assigning each row wholesale let those nulls overwrite the names `list_overdue_invoices`
    had already provided, and the draft rows lost their customer. Found on screen, not here.
    """
    run_id = _run_with_steps(
        [
            ("tool_call", "list_overdue_invoices", {"tool_class": "READ", "arguments": {}}),
            ("tool_result", "list_overdue_invoices", {"result": LISTING}),
            ("tool_call", "get_payment_history", {"tool_class": "READ", "arguments": {}}),
            (
                "tool_result",
                "get_payment_history",
                {
                    "result": {
                        "summary": {"prior_invoices": 1},
                        "history": [
                            {
                                "invoice_id": "in_ferrolux",
                                "number": "JZBOGW8K-0001",
                                "amount_due_display": "$23,400.00",
                            }
                        ],
                    }
                },
            ),
            (
                "tool_call",
                "propose_collection_letter",
                {
                    "tool_class": "DRAFT",
                    "arguments": {"invoice_id": "in_ferrolux", "tone": "final"},
                },
            ),
        ]
    )

    about = queries.get_run(run_id)["transcript"][4]["about"]
    assert about["customer"] == "Ferrolux Metals", "the name from the listing must survive"
