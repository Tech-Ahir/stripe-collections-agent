"""The read-only Stripe client (brief section 7).

This module holds ``STRIPE_API_KEY_READ`` -- a restricted key with read access to Invoices,
Customers and Charges and write access to nothing. There is no write path here, and
``.importlinter`` contract 3 forbids anything under ``app/`` from importing the
write-capable client that lives in the gateway.

Three things this file is careful about:

* **Filtering happens server-side.** ``status="open"`` plus ``due_date={"lt": now}`` rather
  than paging the whole invoice list and filtering in Python. Past-due is a display badge
  in Stripe's dashboard, not an API status, so the ``due_date`` filter is what does the
  work.
* **Every figure is projected before the agent sees it.** ``days_overdue`` is derived here,
  and every amount arrives with a preformatted ``amount_display`` beside the integer, so
  the model is never handed a number it has to divide.
* **A Stripe failure is data, not a crash.** ``StripeReadError`` carries a structured
  payload the tool layer returns to the agent as an error result, so the agent can note the
  failure and carry on rather than taking the whole run down.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from shared.clock import from_epoch, now_utc
from shared.money import format_amount

log = logging.getLogger("app.stripe.read")

#: Stripe's page size for the overdue query. The tool's own `limit` trims the result.
PAGE_SIZE = 100


class StripeReadError(RuntimeError):
    """A Stripe call failed in a way worth telling the agent about."""

    def __init__(self, message: str, *, code: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def as_tool_error(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": str(self),
            **self.detail,
            "recoverable": True,
            "hint": "Note the failure and continue with the invoices you already have.",
        }


# ----------------------------------------------------------------------------------
# Projections. Pure functions over mappings, so they are testable without Stripe.
# ----------------------------------------------------------------------------------


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    """Walk a path through a Stripe object or a plain mapping.

    ``StripeObject`` is deliberately *not* a mapping in stripe-python 15 -- it raises on
    ``.get()`` and on iteration and tells you to call ``.to_dict()``. So mappings are read
    with ``.get`` and everything else by attribute, which covers both the live objects and
    the fixture dicts the tests use.
    """
    current = obj
    for key in path:
        if current is None:
            return default
        if isinstance(current, Mapping):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return default if current is None else current


def _plain_dict(value: Any) -> dict[str, Any]:
    """A real dict, whether the input is one already or a StripeObject.

    ``dict(stripe_object)`` raises rather than converting. Found by running the live
    fixture through ``project_customer``, which the dict-based unit tests could not catch;
    ``tests/test_stripe_read.py`` now exercises every projection against genuine
    StripeObjects for exactly this reason.
    """
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        return dict(converted) if isinstance(converted, Mapping) else {}
    return {}


def days_overdue(due_date_epoch: int | None, *, now: datetime | None = None) -> int | None:
    """Whole days past the due date. Derived here so the model never computes a date."""
    if not due_date_epoch:
        return None
    reference = now or now_utc()
    delta = reference - from_epoch(due_date_epoch)
    return max(0, delta.days)


def _iso_date(epoch: int | None) -> str | None:
    return from_epoch(epoch).date().isoformat() if epoch else None


def project_invoice(raw: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """The invoice as the agent sees it. Section 7's field table, and nothing else."""
    currency = _get(raw, "currency", default="usd")
    amount_due = int(_get(raw, "amount_due", default=0) or 0)
    amount_remaining = _get(raw, "amount_remaining")
    due_epoch = _get(raw, "due_date")

    customer = _get(raw, "customer")
    customer_expanded = customer is not None and not isinstance(customer, str)
    customer_id = customer if isinstance(customer, str) else _get(customer, "id")

    # Which email counts?
    #
    # Stripe snapshots `customer_email` onto an invoice when it is finalized, and keeps
    # that snapshot even after the customer's own address is removed (verified against
    # test mode). The question this system asks is "can we send a letter to this customer
    # today?", and the answer to that lives on the customer record, not in a historical
    # snapshot. So the expanded customer wins whenever it is available, and the snapshot is
    # a fallback for callers that did not expand it.
    snapshot_email = _get(raw, "customer_email")
    current_email = _get(customer, "email") if customer_expanded else None
    email = current_email if customer_expanded else snapshot_email

    projected: dict[str, Any] = {
        "id": _get(raw, "id"),
        "number": _get(raw, "number"),
        "status": _get(raw, "status"),
        "customer_id": customer_id,
        "customer_name": _get(raw, "customer_name") or _get(customer, "name"),
        "customer_email": email,
        "amount_due": amount_due,
        "amount_due_display": format_amount(amount_due, currency),
        "currency": str(currency).lower(),
        "due_date": _iso_date(due_epoch),
        "days_overdue": days_overdue(due_epoch, now=now),
        "hosted_invoice_url": _get(raw, "hosted_invoice_url"),
        "collection_method": _get(raw, "collection_method"),
        "attempt_count": int(_get(raw, "attempt_count", default=0) or 0),
        "finalized_at": _iso_date(_get(raw, "status_transitions", "finalized_at")),
    }

    if amount_remaining is not None:
        projected["amount_remaining"] = int(amount_remaining)
        projected["amount_remaining_display"] = format_amount(int(amount_remaining), currency)

    projected["customer_email_source"] = "customer" if customer_expanded else "invoice_snapshot"

    # Section 7: "If null, the agent must not propose a letter." Say so in the data rather
    # than relying on the prompt to remember.
    if not email:
        projected["deliverable"] = False
        reason = (
            "This customer has no email address on file, so no letter can be sent. "
            "Do not propose one for this invoice."
        )
        if snapshot_email:
            reason += (
                f" (The invoice still carries {snapshot_email} from when it was finalized, "
                "but the customer record no longer has an address, so that snapshot is "
                "not a valid destination.)"
            )
        projected["not_deliverable_reason"] = reason
    else:
        projected["deliverable"] = True

    if projected["collection_method"] == "charge_automatically" and projected["attempt_count"]:
        projected["automatic_collection_note"] = (
            f"Stripe is still retrying this card automatically "
            f"({projected['attempt_count']} attempts so far). Consider whether a letter is "
            "appropriate before the retries are exhausted."
        )

    return projected


def project_invoice_detail(raw: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """``get_invoice``: full detail, including line items and payment attempts."""
    detail = project_invoice(raw, now=now)
    currency = detail["currency"]

    lines = []
    for line in _get(raw, "lines", "data", default=[]) or []:
        amount = int(_get(line, "amount", default=0) or 0)
        lines.append(
            {
                "description": _get(line, "description"),
                "quantity": _get(line, "quantity"),
                "amount": amount,
                "amount_display": format_amount(amount, _get(line, "currency", default=currency)),
            }
        )
    detail["lines"] = lines
    detail["payment_attempts"] = {
        "attempt_count": detail["attempt_count"],
        "next_payment_attempt": _iso_date(_get(raw, "next_payment_attempt")),
        "attempted": bool(_get(raw, "attempted", default=False)),
    }
    detail["description"] = _get(raw, "description")
    return detail


def project_customer(raw: Any) -> dict[str, Any]:
    """``get_customer``: name, email, created, delinquent, currency, metadata."""
    return {
        "id": _get(raw, "id"),
        "name": _get(raw, "name"),
        "email": _get(raw, "email"),
        "created": _iso_date(_get(raw, "created")),
        "delinquent": bool(_get(raw, "delinquent", default=False)),
        "currency": (_get(raw, "currency") or "").lower() or None,
        "metadata": _plain_dict(_get(raw, "metadata")),
    }


def project_payment_history_entry(raw: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """One prior invoice and its outcome.

    ``days_late`` is the fact that lets the agent tell a first-time late payer from a
    habitual one, which section 4 asks it to use when choosing a tone. It is computed here,
    not by the model.
    """
    currency = _get(raw, "currency", default="usd")
    amount_paid = int(_get(raw, "amount_paid", default=0) or 0)
    amount_due = int(_get(raw, "amount_due", default=0) or 0)
    due_epoch = _get(raw, "due_date")
    paid_epoch = _get(raw, "status_transitions", "paid_at")

    days_late: int | None = None
    if due_epoch and paid_epoch:
        days_late = max(0, (from_epoch(paid_epoch) - from_epoch(due_epoch)).days)

    entry = {
        "invoice_id": _get(raw, "id"),
        "number": _get(raw, "number"),
        "status": _get(raw, "status"),
        "currency": str(currency).lower(),
        "amount_due": amount_due,
        "amount_due_display": format_amount(amount_due, currency),
        "amount_paid": amount_paid,
        "amount_paid_display": format_amount(amount_paid, currency),
        "due_date": _iso_date(due_epoch),
        "paid_at": _iso_date(paid_epoch),
        "days_late_when_paid": days_late,
    }
    if _get(raw, "status") == "paid":
        entry["outcome"] = (
            "paid on time" if (days_late or 0) == 0 else f"paid {days_late} days late"
        )
    elif _get(raw, "status") == "open":
        still_owed = days_overdue(due_epoch, now=now)
        entry["outcome"] = (
            f"still unpaid, {still_owed} days overdue" if still_owed else "still unpaid"
        )
    else:
        entry["outcome"] = f"{_get(raw, 'status')}"
    return entry


# ----------------------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------------------


class StripeReadClient:
    """Read-only access to Stripe, with a per-run customer cache and bounded retries."""

    def __init__(
        self,
        api_key: str,
        *,
        client: Any = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = now_utc,
        include_test_clock_fixtures: bool = True,
        max_test_clocks: int = 20,
    ) -> None:
        if client is None:
            if not api_key:
                raise StripeReadError(
                    "STRIPE_API_KEY_READ is not configured.",
                    code="stripe_not_configured",
                )
            import stripe

            # An explicit client instance, never the module-global stripe.api_key: the
            # reach of this credential should be visible in one place.
            client = stripe.StripeClient(api_key)
        self._client = client
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._now = now
        self._include_test_clock_fixtures = include_test_clock_fixtures
        self._max_test_clocks = max_test_clocks
        #: Cached for the lifetime of a run (section 7).
        self._customers: dict[str, dict[str, Any]] = {}

    # -- retry ---------------------------------------------------------------------

    def _call(self, what: str, operation: Callable[[], Any]) -> Any:
        """Exponential backoff with jitter on a rate limit; three attempts."""
        import stripe

        last: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return operation()
            except stripe.RateLimitError as exc:
                last = exc
                if attempt == self._max_attempts - 1:
                    break
                delay = (0.5 * (2**attempt)) + random.uniform(0, 0.25)
                log.warning("stripe rate limited on %s; retrying in %.2fs", what, delay)
                self._sleep(delay)
            except stripe.StripeError as exc:
                raise StripeReadError(
                    f"Stripe rejected {what}: {exc.user_message or exc}",
                    code="stripe_error",
                    detail={
                        "operation": what,
                        "stripe_code": getattr(exc, "code", None),
                        "http_status": getattr(exc, "http_status", None),
                    },
                ) from exc
        raise StripeReadError(
            f"Stripe rate limited {what} after {self._max_attempts} attempts.",
            code="stripe_rate_limited",
            detail={"operation": what, "attempts": self._max_attempts},
        ) from last

    # -- the four read operations ---------------------------------------------------

    def list_overdue_invoices(
        self,
        *,
        min_days_overdue: int = 1,
        limit: int = 25,
        min_amount_cents: int | None = None,
    ) -> list[dict[str, Any]]:
        """Open invoices whose due date has passed. Filtered server-side."""
        now = self._now()
        cutoff = int(now.timestamp())
        overdue_filter: dict[str, Any] = {
            "status": "open",  # finalized, with a balance remaining
            "due_date": {"lt": cutoff},
            "limit": PAGE_SIZE,
            "expand": ["data.customer"],
        }
        page = self._call(
            "invoices.list", lambda: self._client.v1.invoices.list(params=overdue_filter)
        )
        raw_invoices = list(getattr(page, "data", []) or [])

        if self._include_test_clock_fixtures:
            raw_invoices.extend(self._test_clock_overdue(overdue_filter))

        invoices: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_invoices:
            projected = project_invoice(raw, now=now)
            if projected["id"] in seen:
                continue
            if (projected["days_overdue"] or 0) < min_days_overdue:
                continue
            if min_amount_cents is not None and projected["amount_due"] < min_amount_cents:
                continue
            seen.add(projected["id"])
            invoices.append(projected)
            self._remember_customer(raw)

        # Largest first: the operator's queue is sorted by amount, and so is the agent's
        # attention when a run is capped.
        invoices.sort(key=lambda item: item["amount_due"], reverse=True)
        return invoices[: max(0, limit)]

    def _test_clock_overdue(self, overdue_filter: dict[str, Any]) -> list[Any]:
        """The same server-side filter, scoped per test-clock customer.

        Stripe will not accept a back-dated ``due_date`` at creation or on update, so an
        invoice that is genuinely 95 days overdue can only be produced in test mode on a
        **test clock** -- and objects on a test clock are omitted from unfiltered list
        calls, while ``invoices.list`` offers no ``test_clock`` filter. The fixture the
        brief asks for in section 7 is therefore invisible to the query in section 7.

        The resolution keeps the requirement that matters: filtering still happens
        server-side, with the identical ``status`` and ``due_date`` conditions. All that is
        added is a ``customer`` scope, because that is the only handle Stripe gives on a
        clock's objects. Nothing is paged and filtered in Python.

        Switch it off with ``STRIPE_INCLUDE_TEST_CLOCK_INVOICES=false``. In live mode there
        are no test clocks, so this costs one empty call and returns nothing -- and this
        system has no live-key path at all.
        """
        collected: list[Any] = []
        try:
            clocks = self._call(
                "test_clocks.list",
                lambda: self._client.v1.test_helpers.test_clocks.list(
                    params={"limit": self._max_test_clocks}
                ),
            )
        except StripeReadError:
            # A restricted key without test-clock read access is the expected case in a
            # tightened deployment. That is not an error worth failing a run over.
            log.info("test clocks are not readable with this key; skipping fixture scan")
            return collected

        scoped = {key: value for key, value in overdue_filter.items() if key != "limit"}
        for clock in getattr(clocks, "data", []) or []:
            customers = self._call(
                "customers.list(test_clock)",
                lambda clock_id=clock.id: self._client.v1.customers.list(
                    params={"limit": 100, "test_clock": clock_id}
                ),
            )
            for customer in getattr(customers, "data", []) or []:
                page = self._call(
                    "invoices.list(customer)",
                    lambda customer_id=customer.id: self._client.v1.invoices.list(
                        params={**scoped, "customer": customer_id, "limit": PAGE_SIZE}
                    ),
                )
                collected.extend(getattr(page, "data", []) or [])
        if collected:
            log.info("included %d overdue invoice(s) from test-clock fixtures", len(collected))
        return collected

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        raw = self._call(
            "invoices.retrieve",
            lambda: self._client.v1.invoices.retrieve(
                invoice_id, params={"expand": ["customer", "lines"]}
            ),
        )
        self._remember_customer(raw)
        return project_invoice_detail(raw, now=self._now())

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        if customer_id in self._customers:
            return self._customers[customer_id]
        raw = self._call(
            "customers.retrieve", lambda: self._client.v1.customers.retrieve(customer_id)
        )
        projected = project_customer(raw)
        self._customers[customer_id] = projected
        return projected

    def get_payment_history(self, customer_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Prior invoices and their outcomes, newest first."""
        now = self._now()
        page = self._call(
            "invoices.list(customer)",
            lambda: self._client.v1.invoices.list(
                params={"customer": customer_id, "limit": max(1, min(limit, PAGE_SIZE))}
            ),
        )
        return [
            project_payment_history_entry(raw, now=now) for raw in (getattr(page, "data", []) or [])
        ]

    # -- cache ---------------------------------------------------------------------

    def _remember_customer(self, raw_invoice: Any) -> None:
        customer = _get(raw_invoice, "customer")
        if isinstance(customer, Mapping) and _get(customer, "id"):
            self._customers.setdefault(_get(customer, "id"), project_customer(customer))

    @property
    def cached_customer_count(self) -> int:
        return len(self._customers)
