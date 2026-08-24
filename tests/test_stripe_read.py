"""The Stripe read layer (brief section 7).

Projections are pure functions over mappings, so almost all of this runs with no network
and no key. The live fixture is exercised separately by
``scripts/seed_stripe_test_data.py --list``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import stripe

from app.stripe_client.read import (
    StripeReadClient,
    StripeReadError,
    days_overdue,
    project_customer,
    project_invoice,
    project_invoice_detail,
    project_payment_history_entry,
)
from shared.clock import now_utc, to_epoch

NOW = now_utc()


def epoch_days_ago(days: float) -> int:
    return to_epoch(NOW - timedelta(days=days))


def invoice_fixture(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "in_1001",
        "number": "INV-1001",
        "status": "open",
        "currency": "usd",
        "amount_due": 25000,
        "amount_remaining": 25000,
        "due_date": epoch_days_ago(9),
        "hosted_invoice_url": "https://invoice.stripe.com/i/test_1001",
        "collection_method": "send_invoice",
        "attempt_count": 0,
        "customer_email": "ap@acme.test",
        "status_transitions": {"finalized_at": epoch_days_ago(39)},
        "customer": {
            "id": "cus_acme",
            "name": "Acme Industries",
            "email": "ap@acme.test",
            "created": epoch_days_ago(400),
            "delinquent": False,
            "currency": "usd",
            "metadata": {"segment": "smb"},
        },
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------------------
# days_overdue is derived in Python, never by the model
# ----------------------------------------------------------------------------------


def test_days_overdue_is_whole_days_past_the_due_date():
    assert days_overdue(epoch_days_ago(9), now=NOW) == 9
    assert days_overdue(epoch_days_ago(95), now=NOW) == 95


def test_an_invoice_due_today_is_not_yet_overdue():
    assert days_overdue(epoch_days_ago(0.4), now=NOW) == 0


def test_a_future_due_date_is_clamped_to_zero_not_negative():
    assert days_overdue(to_epoch(NOW + timedelta(days=5)), now=NOW) == 0


def test_no_due_date_means_unknown_rather_than_zero():
    """charge_automatically invoices carry no due date. None is the honest answer."""
    assert days_overdue(None) is None


# ----------------------------------------------------------------------------------
# The invoice projection: section 7's field table
# ----------------------------------------------------------------------------------


def test_the_projection_carries_every_field_section_7_lists():
    projected = project_invoice(invoice_fixture(), now=NOW)
    for field in (
        "id",
        "number",
        "customer_id",
        "customer_name",
        "customer_email",
        "amount_due",
        "currency",
        "due_date",
        "days_overdue",
        "hosted_invoice_url",
        "attempt_count",
        "collection_method",
        "finalized_at",
    ):
        assert field in projected, f"section 7 requires {field}"


def test_every_amount_arrives_with_a_preformatted_display_string():
    """So the model is never handed a number it has to divide."""
    projected = project_invoice(invoice_fixture(amount_due=2340000), now=NOW)
    assert projected["amount_due"] == 2340000
    assert projected["amount_due_display"] == "$23,400.00"
    assert projected["amount_remaining_display"] == "$250.00"


def test_the_due_date_is_an_iso_date_not_a_unix_timestamp():
    projected = project_invoice(invoice_fixture(), now=NOW)
    assert projected["due_date"] == (NOW - timedelta(days=9)).date().isoformat()


def test_a_string_customer_id_is_handled_when_the_customer_is_not_expanded():
    projected = project_invoice(invoice_fixture(customer="cus_plain"), now=NOW)
    assert projected["customer_id"] == "cus_plain"
    assert projected["customer_email"] == "ap@acme.test"
    assert projected["customer_email_source"] == "invoice_snapshot"


def test_the_hosted_payment_link_is_passed_through_and_never_constructed():
    projected = project_invoice(invoice_fixture(), now=NOW)
    assert projected["hosted_invoice_url"] == "https://invoice.stripe.com/i/test_1001"


# ----------------------------------------------------------------------------------
# Deliverability: "If null, the agent must not propose a letter"
# ----------------------------------------------------------------------------------


def test_an_invoice_with_no_email_is_marked_undeliverable():
    raw = invoice_fixture(customer_email=None)
    raw["customer"] = {**raw["customer"], "email": None}
    projected = project_invoice(raw, now=NOW)
    assert projected["deliverable"] is False
    assert "no email address" in projected["not_deliverable_reason"]
    assert "Do not propose one" in projected["not_deliverable_reason"]


def test_the_current_customer_address_beats_the_invoices_finalized_snapshot():
    """Verified against live test mode: Stripe keeps the snapshot after the address is
    removed from the customer. The question is whether a letter can be sent *today*."""
    raw = invoice_fixture(customer_email="placeholder@example.invalid")
    raw["customer"] = {**raw["customer"], "email": None}
    projected = project_invoice(raw, now=NOW)

    assert projected["deliverable"] is False
    assert projected["customer_email"] is None
    assert "placeholder@example.invalid" in projected["not_deliverable_reason"]
    assert "not a valid destination" in projected["not_deliverable_reason"]


def test_an_updated_customer_address_wins_over_a_stale_snapshot():
    raw = invoice_fixture(customer_email="old@acme.test")
    raw["customer"] = {**raw["customer"], "email": "new@acme.test"}
    projected = project_invoice(raw, now=NOW)
    assert projected["customer_email"] == "new@acme.test"
    assert projected["deliverable"] is True


# ----------------------------------------------------------------------------------
# Automatic collection: do not dun someone whose card is mid-retry
# ----------------------------------------------------------------------------------


def test_an_invoice_still_being_retried_carries_a_note_for_the_agent():
    projected = project_invoice(
        invoice_fixture(collection_method="charge_automatically", attempt_count=2), now=NOW
    )
    assert "still retrying" in projected["automatic_collection_note"]
    assert "2 attempts" in projected["automatic_collection_note"]


def test_a_send_invoice_invoice_carries_no_retry_note():
    projected = project_invoice(invoice_fixture(attempt_count=3), now=NOW)
    assert "automatic_collection_note" not in projected


# ----------------------------------------------------------------------------------
# Detail and customer projections
# ----------------------------------------------------------------------------------


def test_the_detail_projection_includes_line_items_with_formatted_amounts():
    raw = invoice_fixture()
    raw["lines"] = {
        "data": [
            {"description": "March consumables", "quantity": 3, "amount": 15000},
            {"description": "Delivery", "quantity": 1, "amount": 10000},
        ]
    }
    detail = project_invoice_detail(raw, now=NOW)
    assert [line["description"] for line in detail["lines"]] == [
        "March consumables",
        "Delivery",
    ]
    assert detail["lines"][0]["amount_display"] == "$150.00"


def test_the_detail_projection_includes_payment_attempts():
    detail = project_invoice_detail(invoice_fixture(attempted=True, attempt_count=2), now=NOW)
    assert detail["payment_attempts"]["attempt_count"] == 2
    assert detail["payment_attempts"]["attempted"] is True


def test_the_customer_projection_matches_section_4s_tool_schema():
    projected = project_customer(invoice_fixture()["customer"])
    assert set(projected) == {
        "id",
        "name",
        "email",
        "created",
        "delinquent",
        "currency",
        "metadata",
    }
    assert projected["delinquent"] is False
    assert projected["metadata"] == {"segment": "smb"}


# ----------------------------------------------------------------------------------
# Payment history: the signal behind "tone calibrated by payment history"
# ----------------------------------------------------------------------------------


def test_an_invoice_paid_before_its_due_date_reads_as_paid_on_time():
    entry = project_payment_history_entry(
        {
            "id": "in_hist",
            "number": "INV-0900",
            "status": "paid",
            "currency": "usd",
            "amount_due": 120000,
            "amount_paid": 120000,
            "due_date": epoch_days_ago(370),
            "status_transitions": {"paid_at": epoch_days_ago(372)},
        },
        now=NOW,
    )
    assert entry["days_late_when_paid"] == 0
    assert entry["outcome"] == "paid on time"
    assert entry["amount_paid_display"] == "$1,200.00"


def test_an_invoice_paid_late_says_how_late():
    entry = project_payment_history_entry(
        {
            "id": "in_hist2",
            "status": "paid",
            "currency": "usd",
            "amount_due": 50000,
            "amount_paid": 50000,
            "due_date": epoch_days_ago(100),
            "status_transitions": {"paid_at": epoch_days_ago(58)},
        },
        now=NOW,
    )
    assert entry["days_late_when_paid"] == 42
    assert entry["outcome"] == "paid 42 days late"


def test_a_still_unpaid_prior_invoice_says_how_overdue_it_is():
    entry = project_payment_history_entry(
        {
            "id": "in_hist3",
            "status": "open",
            "currency": "usd",
            "amount_due": 80000,
            "amount_paid": 0,
            "due_date": epoch_days_ago(30),
            "status_transitions": {},
        },
        now=NOW,
    )
    assert entry["outcome"] == "still unpaid, 30 days overdue"


def test_a_voided_invoice_reports_its_status_rather_than_guessing():
    entry = project_payment_history_entry(
        {"id": "in_v", "status": "void", "currency": "usd", "amount_due": 0, "amount_paid": 0},
        now=NOW,
    )
    assert entry["outcome"] == "void"


# ----------------------------------------------------------------------------------
# The client: server-side filtering, ordering, caps
# ----------------------------------------------------------------------------------


class FakePage:
    def __init__(self, data: list[Any]) -> None:
        self.data = data


class FakeService:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.raises: Exception | None = None

    def _record(self, params: dict[str, Any] | None = None, positional: Any = None) -> Any:
        self.calls.append({"params": params or {}, "positional": positional})
        if self.raises is not None:
            raise self.raises
        return self.result

    def list(self, params: dict[str, Any] | None = None) -> Any:
        return self._record(params)

    def retrieve(self, identifier: str, params: dict[str, Any] | None = None) -> Any:
        return self._record(params, identifier)


class FakeStripe:
    """The shape StripeReadClient actually uses: client.v1.<service>."""

    def __init__(self, *, invoices=None, customers=None, test_clocks=None) -> None:
        self.v1 = type("V1", (), {})()
        self.v1.invoices = invoices or FakeService(FakePage([]))
        self.v1.customers = customers or FakeService(FakePage([]))
        self.v1.test_helpers = type("TH", (), {})()
        self.v1.test_helpers.test_clocks = test_clocks or FakeService(FakePage([]))


def make_client(**kwargs) -> tuple[StripeReadClient, FakeStripe]:
    fake = kwargs.pop("fake", None) or FakeStripe()
    client = StripeReadClient(
        "rk_test_x",
        client=fake,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        include_test_clock_fixtures=kwargs.pop("include_test_clock_fixtures", False),
        **kwargs,
    )
    return client, fake


def test_the_overdue_query_filters_server_side():
    """"Filter server-side; do not page the whole invoice list and filter in Python.""" ""
    fake = FakeStripe(invoices=FakeService(FakePage([invoice_fixture()])))
    client, _ = make_client(fake=fake)

    client.list_overdue_invoices()

    params = fake.v1.invoices.calls[0]["params"]
    assert params["status"] == "open"
    assert params["due_date"] == {"lt": to_epoch(NOW)}
    assert params["expand"] == ["data.customer"]


def test_min_days_overdue_excludes_anything_younger():
    fake = FakeStripe(
        invoices=FakeService(
            FakePage(
                [
                    invoice_fixture(id="in_3", due_date=epoch_days_ago(3)),
                    invoice_fixture(id="in_20", due_date=epoch_days_ago(20)),
                ]
            )
        )
    )
    client, _ = make_client(fake=fake)

    result = client.list_overdue_invoices(min_days_overdue=15)

    assert [row["id"] for row in result] == ["in_20"]


def test_min_amount_cents_excludes_small_balances():
    fake = FakeStripe(
        invoices=FakeService(
            FakePage(
                [
                    invoice_fixture(id="in_small", amount_due=500),
                    invoice_fixture(id="in_big", amount_due=500000),
                ]
            )
        )
    )
    client, _ = make_client(fake=fake)

    result = client.list_overdue_invoices(min_amount_cents=100000)

    assert [row["id"] for row in result] == ["in_big"]


def test_results_are_largest_first_and_capped_by_limit():
    fake = FakeStripe(
        invoices=FakeService(
            FakePage(
                [
                    invoice_fixture(id="a", amount_due=100),
                    invoice_fixture(id="b", amount_due=900),
                    invoice_fixture(id="c", amount_due=500),
                ]
            )
        )
    )
    client, _ = make_client(fake=fake)

    result = client.list_overdue_invoices(limit=2)

    assert [row["id"] for row in result] == ["b", "c"]


def test_a_zero_limit_returns_nothing_rather_than_everything():
    """A negative slice would silently return the whole list."""
    fake = FakeStripe(invoices=FakeService(FakePage([invoice_fixture()])))
    client, _ = make_client(fake=fake)
    assert client.list_overdue_invoices(limit=0) == []


def test_duplicate_invoices_from_two_sources_appear_once():
    """The test-clock scan can return an invoice the global query already found."""
    fake = FakeStripe(
        invoices=FakeService(FakePage([invoice_fixture(id="in_dup")])),
        customers=FakeService(FakePage([type("C", (), {"id": "cus_acme"})()])),
        test_clocks=FakeService(FakePage([type("K", (), {"id": "clock_1"})()])),
    )
    client, _ = make_client(fake=fake, include_test_clock_fixtures=True)

    result = client.list_overdue_invoices()

    assert [row["id"] for row in result] == ["in_dup"]


# ----------------------------------------------------------------------------------
# The test-clock fixture path
# ----------------------------------------------------------------------------------


def test_the_test_clock_scan_reuses_the_same_server_side_filters():
    """Only a customer scope is added. Nothing is paged and filtered in Python."""
    fake = FakeStripe(
        invoices=FakeService(FakePage([])),
        customers=FakeService(FakePage([type("C", (), {"id": "cus_seeded"})()])),
        test_clocks=FakeService(FakePage([type("K", (), {"id": "clock_1"})()])),
    )
    client, _ = make_client(fake=fake, include_test_clock_fixtures=True)

    client.list_overdue_invoices()

    scoped = fake.v1.invoices.calls[-1]["params"]
    assert scoped["status"] == "open"
    assert scoped["due_date"] == {"lt": to_epoch(NOW)}
    assert scoped["customer"] == "cus_seeded"
    assert fake.v1.customers.calls[0]["params"]["test_clock"] == "clock_1"


def test_the_test_clock_scan_is_skipped_when_disabled():
    fake = FakeStripe(
        invoices=FakeService(FakePage([])),
        test_clocks=FakeService(FakePage([type("K", (), {"id": "clock_1"})()])),
    )
    client, _ = make_client(fake=fake, include_test_clock_fixtures=False)

    client.list_overdue_invoices()

    assert fake.v1.test_helpers.test_clocks.calls == []
    assert len(fake.v1.invoices.calls) == 1


def test_a_key_without_test_clock_access_does_not_fail_the_run():
    """A tightened restricted key is the expected case, not an error."""
    clocks = FakeService(FakePage([]))
    clocks.raises = stripe.PermissionError("no access to test clocks")
    fake = FakeStripe(invoices=FakeService(FakePage([invoice_fixture()])), test_clocks=clocks)
    client, _ = make_client(fake=fake, include_test_clock_fixtures=True)

    result = client.list_overdue_invoices()

    assert len(result) == 1, "the global query's results must still come through"


# ----------------------------------------------------------------------------------
# Failure handling
# ----------------------------------------------------------------------------------


def test_a_rate_limit_is_retried_with_backoff_then_reported():
    slept: list[float] = []
    invoices = FakeService(FakePage([]))
    invoices.raises = stripe.RateLimitError("slow down")
    fake = FakeStripe(invoices=invoices)
    client = StripeReadClient(
        "rk_test_x",
        client=fake,
        sleep=slept.append,
        now=lambda: NOW,
        include_test_clock_fixtures=False,
    )

    with pytest.raises(StripeReadError) as raised:
        client.list_overdue_invoices()

    assert raised.value.code == "stripe_rate_limited"
    assert len(fake.v1.invoices.calls) == 3, "three attempts, per section 7"
    assert len(slept) == 2, "two waits between three attempts"
    assert slept[1] > slept[0], "backoff must increase"


def test_a_rate_limit_that_clears_succeeds_without_error():
    attempts = {"n": 0}

    class Flaky(FakeService):
        def list(self, params=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise stripe.RateLimitError("slow down")
            return FakePage([invoice_fixture()])

    fake = FakeStripe(invoices=Flaky())
    client, _ = make_client(fake=fake)

    assert len(client.list_overdue_invoices()) == 1
    assert attempts["n"] == 2


def test_a_stripe_error_becomes_a_structured_result_for_the_agent():
    (
        """"StripeError inside a tool returns a structured error to the agent rather than
    crashing the run."""
        ""
    )
    invoices = FakeService(FakePage([]))
    invoices.raises = stripe.InvalidRequestError("no such invoice", param="id")
    fake = FakeStripe(invoices=invoices)
    client, _ = make_client(fake=fake)

    with pytest.raises(StripeReadError) as raised:
        client.list_overdue_invoices()

    error = raised.value.as_tool_error()
    assert error["error"] == "stripe_error"
    assert error["recoverable"] is True
    assert "continue" in error["hint"]
    assert error["operation"] == "invoices.list"


def test_an_absent_key_is_refused_at_construction():
    with pytest.raises(StripeReadError) as raised:
        StripeReadClient("")
    assert raised.value.code == "stripe_not_configured"


# ----------------------------------------------------------------------------------
# The per-run customer cache
# ----------------------------------------------------------------------------------


def test_a_customer_is_fetched_once_per_run():
    """"Cache customer lookups for the lifetime of a run.""" ""
    customers = FakeService(invoice_fixture()["customer"])
    fake = FakeStripe(customers=customers)
    client, _ = make_client(fake=fake)

    first = client.get_customer("cus_acme")
    second = client.get_customer("cus_acme")

    assert first == second
    assert len(customers.calls) == 1, "the second lookup must come from the cache"


def test_listing_overdue_invoices_warms_the_customer_cache():
    """The expanded customer is already in hand; refetching it would be waste."""
    fake = FakeStripe(
        invoices=FakeService(FakePage([invoice_fixture()])),
        customers=FakeService(invoice_fixture()["customer"]),
    )
    client, _ = make_client(fake=fake)

    client.list_overdue_invoices()
    assert client.cached_customer_count == 1

    client.get_customer("cus_acme")
    assert fake.v1.customers.calls == [], "no API call was needed"


# ----------------------------------------------------------------------------------
# Every projection against a GENUINE StripeObject
#
# The fixtures above are plain dicts, which hid a real bug: `dict(stripe_object)` raises
# ("StripeObject is not iterable or a mapping"), so project_customer blew up the first time
# it met live data. These tests run the same projections over real StripeObjects so that
# class of bug cannot come back.
# ----------------------------------------------------------------------------------


def as_stripe_object(payload: dict[str, Any]) -> Any:
    from stripe import StripeObject

    return StripeObject.construct_from(payload, "rk_test_x")


def test_the_invoice_projection_handles_a_real_stripe_object():
    projected = project_invoice(as_stripe_object(invoice_fixture()), now=NOW)
    assert projected["id"] == "in_1001"
    assert projected["customer_name"] == "Acme Industries"
    assert projected["customer_email"] == "ap@acme.test"
    assert projected["amount_due_display"] == "$250.00"
    assert projected["days_overdue"] == 9
    assert projected["finalized_at"] is not None
    assert projected["deliverable"] is True


def test_the_customer_projection_handles_a_real_stripe_object():
    """The exact call that failed against live data. dict(StripeObject) raises."""
    projected = project_customer(as_stripe_object(invoice_fixture()["customer"]))
    assert projected["name"] == "Acme Industries"
    assert projected["metadata"] == {"segment": "smb"}
    assert projected["delinquent"] is False


def test_a_stripe_object_with_no_metadata_projects_to_an_empty_dict():
    raw = invoice_fixture()["customer"]
    raw.pop("metadata")
    assert project_customer(as_stripe_object(raw))["metadata"] == {}


def test_the_detail_projection_handles_a_real_stripe_object():
    raw = invoice_fixture()
    raw["lines"] = {"data": [{"description": "Line", "quantity": 1, "amount": 15000}]}
    detail = project_invoice_detail(as_stripe_object(raw), now=NOW)
    assert detail["lines"][0]["amount_display"] == "$150.00"
    assert detail["payment_attempts"]["attempt_count"] == 0


def test_the_history_projection_handles_a_real_stripe_object():
    entry = project_payment_history_entry(
        as_stripe_object(
            {
                "id": "in_hist",
                "status": "paid",
                "currency": "usd",
                "amount_due": 120000,
                "amount_paid": 120000,
                "due_date": epoch_days_ago(370),
                "status_transitions": {"paid_at": epoch_days_ago(372)},
            }
        ),
        now=NOW,
    )
    assert entry["outcome"] == "paid on time"


def test_an_undeliverable_invoice_is_detected_on_a_real_stripe_object():
    """The seeded Delta Fabrication case, in object form."""
    raw = invoice_fixture(customer_email="placeholder-delta@example.invalid")
    raw["customer"] = {**raw["customer"], "email": None}
    projected = project_invoice(as_stripe_object(raw), now=NOW)
    assert projected["deliverable"] is False
    assert projected["customer_email"] is None
