"""The agent's toolset (brief section 4, CLAUDE.md rule 4).

    "READ THIS TWICE. send_collection_letter is not a tool the agent has and is refused. It
    is absent from the agent's tool schema entirely. [...] Refusal at runtime is a weaker
    design than absence at definition time, and the client is evaluating exactly this."

The first block of tests is that assertion, made mechanical. Section 12 warns that an AI
coding tool will tend to "give the agent a send_collection_letter tool that checks approval
status", and notes that "the reviewer will look at the schema" -- so these tests look at the
schema too, including the exact JSON that would go over the wire.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent.tools.base import ToolClass, ToolFailure, ToolSpec
from app.agent.tools.read_tools import build_read_tools
from app.agent.tools.registry import build_registry, letter_facts
from app.store.repositories import RunStore
from tests.factories import make_run

EXPECTED_TOOLS = {
    "list_overdue_invoices": "READ",
    "get_invoice": "READ",
    "get_customer": "READ",
    "get_payment_history": "READ",
    "propose_collection_letter": "DRAFT",
}

#: Any of these appearing in the agent's schema means the boundary has been dismantled.
SEND_WORDS = (
    "send",
    "email",
    "deliver",
    "dispatch",
    "execute",
    "transmit",
    "mail",
    "notify",
    "approve",
)


class FakeReader:
    """A stand-in for StripeReadClient. No network, no key."""

    def __init__(self, invoices: list[dict[str, Any]] | None = None) -> None:
        self.invoices = invoices if invoices is not None else [invoice()]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_overdue_invoices(self, **kwargs):
        self.calls.append(("list_overdue_invoices", kwargs))
        rows = self.invoices
        floor = kwargs.get("min_days_overdue") or 0
        rows = [row for row in rows if (row["days_overdue"] or 0) >= floor]
        return rows[: kwargs.get("limit") or 25]

    def get_invoice(self, invoice_id):
        self.calls.append(("get_invoice", {"invoice_id": invoice_id}))
        found = [row for row in self.invoices if row["id"] == invoice_id]
        if not found:
            raise LookupError(invoice_id)
        return found[0]

    def get_customer(self, customer_id):
        self.calls.append(("get_customer", {"customer_id": customer_id}))
        return {"id": customer_id, "name": "Acme Industries", "email": "ap@acme.test"}

    def get_payment_history(self, customer_id, *, limit=10):
        self.calls.append(("get_payment_history", {"customer_id": customer_id}))
        return [
            {
                "invoice_id": "in_old",
                "number": "INV-0900",
                "status": "paid",
                "amount_due": 120000,
                "amount_due_display": "$1,200.00",
                "amount_paid": 120000,
                "amount_paid_display": "$1,200.00",
                "currency": "usd",
                "due_date": "2025-09-01",
                "paid_at": "2025-08-30",
                "days_late_when_paid": 0,
                "outcome": "paid on time",
            }
        ]


def invoice(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "in_1001",
        "number": "INV-1001",
        "status": "open",
        "customer_id": "cus_acme",
        "customer_name": "Acme Industries",
        "customer_email": "ap@acme.test",
        "amount_due": 25000,
        "amount_due_display": "$250.00",
        "currency": "usd",
        "due_date": "2026-08-01",
        "days_overdue": 9,
        "hosted_invoice_url": "https://invoice.stripe.com/i/test_1001",
        "collection_method": "send_invoice",
        "attempt_count": 0,
        "finalized_at": "2026-07-02",
        "deliverable": True,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def registry(db_path):
    from shared.db import session_scope

    with session_scope() as session:
        run = make_run(session)
        run_id = run.id
    reader = FakeReader()
    built = build_registry(reader=reader, store=RunStore(run_id), max_proposals=10, ttl_hours=72)
    built.reader = reader  # type: ignore[attr-defined]
    built.run_id = run_id  # type: ignore[attr-defined]
    return built


# ----------------------------------------------------------------------------------
# There is no send tool. Not a gated one. Not a disabled one. None.
# ----------------------------------------------------------------------------------


def test_the_registry_exposes_exactly_the_five_declared_tools(registry):
    assert set(registry.names) == set(EXPECTED_TOOLS)


def test_every_tool_is_read_or_draft_and_none_is_action(registry):
    assert registry.classification() == EXPECTED_TOOLS
    assert "ACTION" not in set(registry.classification().values())


def test_no_tool_in_the_schema_can_reach_the_outside_world(registry):
    """The reviewer will look at the schema. So does this test."""
    for tool in registry.anthropic_schemas():
        for word in SEND_WORDS:
            assert word not in tool["name"].lower(), (
                f"tool {tool['name']!r} looks like it can act. The agent's toolset must "
                "contain no send capability of any kind."
            )


def test_the_serialised_schema_contains_no_send_collection_letter(registry):
    """The exact JSON that would go over the wire, searched for the forbidden name."""
    wire = json.dumps(registry.anthropic_schemas())
    assert "send_collection_letter" not in wire, (
        "send_collection_letter must be absent from the agent's tool schema entirely -- "
        "it lives in the gateway and is invocable only by an approved proposal."
    )


def test_declaring_an_action_tool_is_rejected_at_definition_time(registry):
    """Absence at definition time, not refusal at runtime. Construction itself fails."""
    with pytest.raises(ValueError, match="ACTION"):
        ToolSpec(
            name="send_collection_letter",
            tool_class=ToolClass.ACTION,
            description="send a letter",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: None,
        )


def test_the_action_class_exists_so_the_classification_is_complete():
    """Section 4 defines three kinds; a reviewer should find all three named."""
    assert {member.value for member in ToolClass} == {"READ", "DRAFT", "ACTION"}


def test_the_agent_package_cannot_import_the_token_minter_or_gateway_client():
    """Contract 6: the agent must not be able to mint its own permission slip."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    probe = (
        "import sys, app.agent.loop, app.agent.tools.registry\n"
        "leaked = sorted(m for m in sys.modules if m.startswith('app.approval') "
        "or m == 'gateway' or m.startswith('gateway.'))\n"
        "print('LEAKED=' + ','.join(leaked))\n"
    )
    from tests.conftest import minimal_subprocess_env

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        capture_output=True,
        text=True,
        env=minimal_subprocess_env(DATABASE_URL="sqlite:///./_probe2.db"),
    )
    assert result.returncode == 0, result.stderr
    line = [x for x in result.stdout.splitlines() if x.startswith("LEAKED=")][0]
    assert line == "LEAKED=", f"the agent loaded something it must not: {line}"


# ----------------------------------------------------------------------------------
# The schemas themselves
# ----------------------------------------------------------------------------------


def test_every_schema_is_closed_and_fully_required(registry):
    """Strict mode's contract: no unexpected properties, nothing implicitly optional."""
    for tool in registry.anthropic_schemas():
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False, tool["name"]
        assert set(schema["required"]) == set(schema["properties"]), tool["name"]
        assert tool["strict"] is True, tool["name"]


def test_optional_parameters_are_nullable_rather_than_omitted(registry):
    schema = next(
        tool for tool in registry.anthropic_schemas() if tool["name"] == "list_overdue_invoices"
    )["input_schema"]
    assert schema["properties"]["limit"]["type"] == ["integer", "null"]
    assert schema["properties"]["min_amount_cents"]["type"] == ["integer", "null"]


def test_the_tone_parameter_is_constrained_to_the_three_tones(registry):
    schema = next(
        tool for tool in registry.anthropic_schemas() if tool["name"] == "propose_collection_letter"
    )["input_schema"]
    assert schema["properties"]["tone"]["enum"] == ["friendly", "firm", "final"]


def test_the_minor_units_trap_is_spelled_out_in_the_schema(registry):
    """The model is told what 2500 means where it would otherwise have to guess."""
    schema = next(
        tool for tool in registry.anthropic_schemas() if tool["name"] == "list_overdue_invoices"
    )["input_schema"]
    assert "MINOR units" in schema["properties"]["min_amount_cents"]["description"]
    assert "2500 means $25.00" in schema["properties"]["min_amount_cents"]["description"]


def test_the_draft_tool_says_plainly_that_it_sends_nothing(registry):
    description = next(
        tool for tool in registry.anthropic_schemas() if tool["name"] == "propose_collection_letter"
    )["description"]
    assert "does not send" in description


# ----------------------------------------------------------------------------------
# Reading, and the fact capture that makes the letter honest
# ----------------------------------------------------------------------------------


def test_reading_invoices_captures_their_facts_for_later(registry):
    registry.execute("list_overdue_invoices", {"min_days_overdue": 1, "limit": 25})
    assert "in_1001" in registry.facts_by_invoice
    facts = registry.facts_by_invoice["in_1001"]
    assert facts["amount_display"] == "$250.00"
    assert facts["invoice_number"] == "INV-1001"


def test_get_invoice_also_captures_facts(registry):
    registry.execute("get_invoice", {"invoice_id": "in_1001"})
    assert registry.facts_by_invoice["in_1001"]["customer_name"] == "Acme Industries"


def test_null_arguments_fall_back_to_the_documented_defaults(registry):
    registry.execute(
        "list_overdue_invoices",
        {"min_days_overdue": None, "limit": None, "min_amount_cents": None},
    )
    _, kwargs = registry.reader.calls[-1]
    assert kwargs["min_days_overdue"] == 1
    assert kwargs["limit"] == 25
    assert kwargs["min_amount_cents"] is None


def test_a_limit_beyond_the_cap_is_clamped(registry):
    registry.execute(
        "list_overdue_invoices", {"min_days_overdue": 1, "limit": 5000, "min_amount_cents": None}
    )
    assert registry.reader.calls[-1][1]["limit"] == 100


def test_undeliverable_invoices_are_called_out_in_the_result(registry):
    registry._tools["list_overdue_invoices"] = build_read_tools(
        FakeReader([invoice(id="in_x", deliverable=False, customer_email=None)])
    )[0]
    result = registry.execute(
        "list_overdue_invoices", {"min_days_overdue": None, "limit": None, "min_amount_cents": None}
    )
    assert "cannot receive a letter" in result["note"]


def test_payment_history_is_summarised_so_the_tone_choice_is_easy(registry):
    result = registry.execute("get_payment_history", {"customer_id": "cus_acme", "limit": None})
    assert result["summary"] == {
        "prior_invoices": 1,
        "paid": 1,
        "paid_on_time": 1,
        "still_unpaid": 0,
    }


def test_an_unknown_tool_is_a_correctable_error_not_a_crash(registry):
    with pytest.raises(ToolFailure) as raised:
        registry.execute("send_collection_letter", {})
    assert raised.value.payload["error"] == "unknown_tool"
    assert raised.value.payload["recoverable"] is True


def test_a_stripe_failure_becomes_a_correctable_tool_error(registry):
    from app.stripe_client.read import StripeReadError

    class Broken(FakeReader):
        def list_overdue_invoices(self, **kwargs):
            raise StripeReadError("Stripe is down", code="stripe_error", detail={"op": "list"})

    registry._tools["list_overdue_invoices"] = build_read_tools(Broken())[0]
    with pytest.raises(ToolFailure) as raised:
        registry.execute(
            "list_overdue_invoices",
            {"min_days_overdue": None, "limit": None, "min_amount_cents": None},
        )
    assert raised.value.payload["error"] == "stripe_error"
    assert raised.value.payload["recoverable"] is True


def test_letter_facts_translates_stripe_names_to_letter_names():
    facts = letter_facts(invoice())
    assert facts["amount_display"] == "$250.00"
    assert facts["invoice_number"] == "INV-1001"
    assert facts["invoice_id"] == "in_1001"
