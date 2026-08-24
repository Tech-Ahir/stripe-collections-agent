"""Seed Stripe test mode with overdue invoices (brief section 7).

    python scripts/seed_stripe_test_data.py            # create the fixture
    python scripts/seed_stripe_test_data.py --recreate  # delete ours first, then create
    python scripts/seed_stripe_test_data.py --list      # show what is currently overdue
    python scripts/seed_stripe_test_data.py --destroy   # delete ours and stop

Six customers and eight invoices, overdue ages spread from 3 to 95 days, including one
customer with no email address and one invoice already paid -- so the agent's filtering is
visible rather than assumed.

Run by a human, never by a service. It needs write access, which no service in this system
has: it reads ``STRIPE_API_KEY_SEED`` and falls back to ``STRIPE_API_KEY_WRITE``. The agent
service holds neither.

Four Stripe constraints shape this script, every one of them discovered by trying:

1. **``due_date`` must be in the future.** Stripe rejects a back-dated due date outright,
   so genuinely overdue invoices cannot be created directly. The fixture is therefore built
   on a **test clock** frozen in the past: relative to the clock the due dates are in the
   future, and in real time they are already overdue. Deleting the clock cascades to
   everything attached to it, which is what makes ``--destroy`` exact.
2. **Pending invoice items are excluded by default.** Creating an invoice item and then an
   invoice produces a $0 invoice that Stripe immediately marks paid. Items are therefore
   attached to an existing draft invoice by id.
3. **A test clock holds at most three customers.** Six customers therefore need two clocks,
   both carrying the fixture's name so ``--destroy`` still removes exactly our data.
4. **``send_invoice`` requires the customer to have an email**, so the no-email customer the
   brief asks for cannot be given an overdue invoice directly. It is created with a
   placeholder address, its invoice is finalized, and the address is then removed. Stripe
   keeps the finalized invoice's ``customer_email`` snapshot, which is why
   ``project_invoice`` prefers the customer's *current* address: the question is whether a
   letter can be sent today, and only the customer record answers that.

A note on ``attempt_count``: section 7 asks for it so the agent can avoid dunning someone
whose card is mid-retry. Every invoice here has ``collection_method="send_invoice"``,
because Stripe only accepts ``due_date`` on that method -- so a genuinely retrying invoice
cannot also be an overdue one. The projection handles a non-zero ``attempt_count`` and
``tests/test_stripe_read.py`` covers it against a fixture.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.money import format_amount  # noqa: E402

#: Everything this script creates is attached to a clock with this name, which is how
#: --destroy can remove exactly our fixture and nothing that was already in the account.
CLOCK_NAME = "stripe-collections-agent-seed"

#: The clock starts here. Far enough back that a paid invoice can be settled on the clock
#: and the open invoices' due dates still land in the clock's future.
CLOCK_START_DAYS_AGO = 400

#: Stripe's limit, discovered the hard way: "You already have the maximum number (3) of
#: customers allowed on this test clock."
CUSTOMERS_PER_CLOCK = 3

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


@dataclass(frozen=True)
class SeedCustomer:
    key: str
    name: str
    #: ``None`` means the customer must end up with no email address. Stripe will not let
    #: a send_invoice invoice be created for such a customer, so one is set during
    #: creation and removed once the invoices exist. See constraint 4 above.
    email: str | None
    note: str = ""

    @property
    def creation_email(self) -> str:
        return self.email or f"placeholder-{self.key}@example.invalid"


@dataclass(frozen=True)
class SeedInvoice:
    customer: str
    amount: int  # minor units
    description: str
    #: For open invoices: how many days past due, in real time.
    days_overdue: int | None = None
    #: For the paid invoice: offsets from the clock start, in days.
    due_offset_days: int | None = None
    paid_offset_days: int | None = None
    note: str = ""


CUSTOMERS = (
    SeedCustomer("acme", "Acme Industries", "ap@acme.test", "clean payment history"),
    SeedCustomer("borealis", "Borealis Systems", "accounts@borealis.test"),
    SeedCustomer("corvus", "Corvus Logistics", "finance@corvus.test"),
    SeedCustomer(
        "delta",
        "Delta Fabrication",
        None,
        "NO EMAIL -- the agent must not propose a letter for this one",
    ),
    SeedCustomer("eastwind", "Eastwind Retail", "ar@eastwind.test"),
    SeedCustomer("ferrolux", "Ferrolux Metals", "billing@ferrolux.test", "two open invoices"),
)

INVOICES = (
    # The already-paid one. Settled two days BEFORE it was due, which gives Acme a clean
    # history for the agent's get_payment_history call to find.
    SeedInvoice(
        "acme",
        120000,
        "Q1 maintenance retainer",
        due_offset_days=30,
        paid_offset_days=28,
        note="already paid, on time",
    ),
    # Seven open invoices, 3 -> 95 days overdue.
    SeedInvoice("acme", 25000, "March consumables", days_overdue=3, note="friendly territory"),
    SeedInvoice("borealis", 124050, "Licence renewal", days_overdue=9),
    SeedInvoice("corvus", 480000, "Freight - February", days_overdue=18),
    SeedInvoice(
        "delta",
        330000,
        "Tooling batch 47",
        days_overdue=27,
        note="no email on file -- must be skipped",
    ),
    SeedInvoice("eastwind", 87500, "Shelf fittings", days_overdue=47),
    SeedInvoice(
        "ferrolux",
        2340000,
        "Alloy supply - January",
        days_overdue=62,
        note="$23,400.00 -- makes a currency error obvious",
    ),
    SeedInvoice(
        "ferrolux", 865000, "Alloy supply - December", days_overdue=95, note="final notice"
    ),
)


def api_key() -> str:
    for name in ("STRIPE_API_KEY_SEED", "STRIPE_API_KEY_WRITE"):
        value = (os.environ.get(name) or "").strip()
        if value:
            if not value.startswith("sk_test") and not value.startswith("rk_test"):
                raise SystemExit(
                    f"{name} is not a test-mode key. This script only runs in test mode."
                )
            return value
    raise SystemExit(
        "Set STRIPE_API_KEY_SEED (a standard sk_test_... key) in your .env.\n"
        "The services never read it: seeding needs write access, and no service has any."
    )


def _wait_until_ready(client, clock_id: str, *, timeout: float = 120.0) -> None:
    """Advancing a test clock is asynchronous. Poll until it settles."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        clock = client.v1.test_helpers.test_clocks.retrieve(clock_id)
        status = clock.status
        if status == "ready":
            return
        if status == "internal_failure":
            raise SystemExit(f"test clock {clock_id} failed while advancing")
        time.sleep(1.0)
    raise SystemExit(f"test clock {clock_id} did not become ready within {timeout:.0f}s")


def find_our_clocks(client) -> list:
    return [
        clock
        for clock in client.v1.test_helpers.test_clocks.list(params={"limit": 100}).data
        if clock.name == CLOCK_NAME
    ]


def destroy(client) -> int:
    """Delete our fixture. Deleting a test clock cascades to its customers and invoices."""
    clocks = find_our_clocks(client)
    for clock in clocks:
        client.v1.test_helpers.test_clocks.delete(clock.id)
        print(f"  deleted test clock {clock.id} (and everything attached to it)")
    if not clocks:
        print(f"  {DIM}no fixture to delete{RESET}")
    return len(clocks)


def seed(client) -> dict:
    now = int(time.time())
    clock_start = now - CLOCK_START_DAYS_AGO * 86400

    print(f"{BOLD}Creating the fixture{RESET}")

    # One clock per three customers (Stripe's limit), all sharing the fixture's name.
    groups = [
        CUSTOMERS[index : index + CUSTOMERS_PER_CLOCK]
        for index in range(0, len(CUSTOMERS), CUSTOMERS_PER_CLOCK)
    ]
    clocks: list[str] = []
    customers: dict[str, str] = {}
    clock_of: dict[str, str] = {}

    for number, group in enumerate(groups, start=1):
        clock = client.v1.test_helpers.test_clocks.create(
            params={"frozen_time": clock_start, "name": CLOCK_NAME}
        )
        clocks.append(clock.id)
        print(
            f"  test clock {number}/{len(groups)} {clock.id} "
            f"frozen {CLOCK_START_DAYS_AGO} days in the past"
        )
        for spec in group:
            created = client.v1.customers.create(
                params={
                    "name": spec.name,
                    "email": spec.creation_email,
                    "test_clock": clock.id,
                    "metadata": {"seeded_by": CLOCK_NAME, "key": spec.key},
                }
            )
            customers[spec.key] = created.id
            clock_of[spec.key] = clock.id
            marker = f"  {YELLOW}<- {spec.note}{RESET}" if spec.note else ""
            print(f"    customer {spec.name:<20} {spec.email or '(no email)':<26}{marker}")

    def build(spec: SeedInvoice, due_epoch: int) -> str:
        draft = client.v1.invoices.create(
            params={
                "customer": customers[spec.customer],
                "collection_method": "send_invoice",
                "due_date": due_epoch,
                "auto_advance": False,  # no automatic dunning; nothing is emailed
                "metadata": {"seeded_by": CLOCK_NAME},
            }
        )
        # Attach the line to THIS draft by id. A pending item would be excluded and the
        # invoice would finalize at zero and be marked paid.
        client.v1.invoice_items.create(
            params={
                "customer": customers[spec.customer],
                "invoice": draft.id,
                "amount": spec.amount,
                "currency": "usd",
                "description": spec.description,
            }
        )
        final = client.v1.invoices.finalize_invoice(draft.id)
        return final.id

    # --- the paid invoice(s) -------------------------------------------------------
    paid_specs = [s for s in INVOICES if s.paid_offset_days is not None]
    open_specs = [s for s in INVOICES if s.days_overdue is not None]

    paid_ids: list[tuple[str, SeedInvoice]] = []
    for spec in paid_specs:
        invoice_id = build(spec, clock_start + (spec.due_offset_days or 0) * 86400)
        paid_ids.append((invoice_id, spec))

    # Settle each on its own clock, in chronological order, so paid_at lands before
    # due_date and the payment history genuinely reads "paid on time".
    for invoice_id, spec in sorted(paid_ids, key=lambda pair: pair[1].paid_offset_days or 0):
        clock_id = clock_of[spec.customer]
        settle_at = clock_start + (spec.paid_offset_days or 0) * 86400
        client.v1.test_helpers.test_clocks.advance(clock_id, params={"frozen_time": settle_at})
        _wait_until_ready(client, clock_id)
        client.v1.invoices.pay(invoice_id, params={"paid_out_of_band": True})
        late = (spec.paid_offset_days or 0) - (spec.due_offset_days or 0)
        when = "on time" if late <= 0 else f"{late} days late"
        print(
            f"  invoice {format_amount(spec.amount, 'usd'):>12}  {spec.customer:<10} PAID ({when})"
        )

    # --- the open invoices ---------------------------------------------------------
    for spec in sorted(open_specs, key=lambda s: s.days_overdue or 0):
        due_epoch = now - (spec.days_overdue or 0) * 86400
        build(spec, due_epoch)
        marker = f"  {YELLOW}<- {spec.note}{RESET}" if spec.note else ""
        print(
            f"  invoice {format_amount(spec.amount, 'usd'):>12}  "
            f"{spec.customer:<10} {spec.days_overdue:>3} days overdue{marker}"
        )

    # --- and finally, strip the placeholder address ---------------------------------
    #
    # Done last, because Stripe refuses to create a send_invoice invoice for a customer
    # with no email. The finalized invoices keep their snapshot; the customer does not.
    for spec in CUSTOMERS:
        if spec.email is None:
            client.v1.customers.update(customers[spec.key], params={"email": ""})
            print(
                f"  {spec.name}: email removed "
                f"{DIM}(its invoice is now undeliverable, by design){RESET}"
            )

    return {"clocks": len(clocks), "customers": len(customers), "invoices": len(INVOICES)}


def show_overdue(key: str) -> None:
    """What the agent's list_overdue_invoices tool will actually see.

    Goes through StripeReadClient rather than a bespoke query, so this output cannot drift
    from what the agent is handed.
    """
    from app.stripe_client.read import StripeReadClient

    reader = StripeReadClient(key)
    rows = reader.list_overdue_invoices(min_days_overdue=1, limit=100)

    print()
    print(f"{BOLD}Open and past due, as the agent will see it{RESET}")
    print(f"  {'amount':>12}  {'days':>4}  {'customer':<22} {'email':<28} send?")
    print(f"  {'-' * 12}  {'-' * 4}  {'-' * 22} {'-' * 28} -----")
    ours = 0
    for row in rows:
        seeded = (row["customer_name"] or "") in {c.name for c in CUSTOMERS}
        ours += seeded
        flag = "" if seeded else f"  {YELLOW}<- pre-existing, not from this fixture{RESET}"
        print(
            f"  {row['amount_due_display']:>12}  {str(row['days_overdue']):>4}  "
            f"{(row['customer_name'] or '?'):<22} {(row['customer_email'] or '(none)'):<28} "
            f"{'yes' if row['deliverable'] else 'NO':<5}{flag}"
        )

    undeliverable = [r for r in rows if not r["deliverable"]]
    print()
    print(f"  {len(rows)} overdue invoice(s); {ours} from this fixture")
    if undeliverable:
        print(
            f"  {GREEN}{len(undeliverable)} with no email on file -- the agent must not "
            f"propose a letter for {'them' if len(undeliverable) > 1 else 'it'}{RESET}"
        )
    spread = sorted({r["days_overdue"] for r in rows if r["days_overdue"]})
    if spread:
        print(f"  overdue spread: {spread}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recreate", action="store_true", help="delete our fixture, then create")
    parser.add_argument("--destroy", action="store_true", help="delete our fixture and stop")
    parser.add_argument("--list", action="store_true", help="show what is currently overdue")
    args = parser.parse_args()

    import stripe

    key = api_key()
    client = stripe.StripeClient(key)

    if args.destroy:
        destroy(client)
        return 0

    if args.list:
        show_overdue(key)
        return 0

    if args.recreate:
        destroy(client)
    elif find_our_clocks(client):
        print(
            f"{YELLOW}A fixture already exists.{RESET} Use --recreate to replace it, "
            "--list to inspect it, or --destroy to remove it."
        )
        return 1

    summary = seed(client)
    print(
        f"\n{GREEN}Seeded {summary['customers']} customers and "
        f"{summary['invoices']} invoices.{RESET}"
    )
    show_overdue(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
