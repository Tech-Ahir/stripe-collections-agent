"""Agent-service settings.

Note what is absent: there is no field for a write-capable Stripe key, no SMTP settings
and no Resend key. This class *cannot* read them. That is the credential split expressed
in the type system rather than in a comment.
"""

from __future__ import annotations

from functools import lru_cache

from shared.config import CommonSettings


class AppSettings(CommonSettings):
    #: Restricted key. Read-only on Invoices, Customers, Charges.
    stripe_api_key_read: str = ""

    anthropic_api_key: str = ""

    #: Section 3 names Claude Sonnet. Overridable so a different tier is a config change.
    anthropic_model: str = "claude-sonnet-5"

    #: The only crossing point. Resolved on the internal Docker network.
    gateway_url: str = "http://gateway:9000"

    max_tool_calls_per_run: int = 25
    max_proposals_per_run: int = 10

    #: Stripe refuses a back-dated due_date, so a genuinely 95-days-overdue invoice can
    #: only exist in test mode on a test clock -- and clock objects are omitted from
    #: unfiltered list calls. With this on, the overdue query is additionally run scoped to
    #: each test-clock customer, with the identical server-side status and due_date
    #: filters. It is what makes scripts/seed_stripe_test_data.py visible to the agent.
    #: Harmless in live mode, where no test clocks exist. See knowledge-base note 04.
    stripe_include_test_clock_invoices: bool = True

    #: Upper bound on a single agent run, so a wedged run cannot hold a worker forever.
    run_timeout_seconds: float = 600.0
    gateway_timeout_seconds: float = 30.0

    #: How many agent runs may execute at once. Two is plenty for one operator, and it
    #: keeps SQLite write contention where the retry logic can absorb it.
    max_concurrent_runs: int = 2

    #: Section 9 requires a "Try to send without approval" button, which is step 4 of the
    #: handoff demo. It mints a correctly signed token for a PENDING proposal so the gateway
    #: refuses it at check 4 rather than at the signature check. See app/approval/probe.py.
    enable_unapproved_attempt_demo: bool = True


@lru_cache(maxsize=1)
def settings() -> AppSettings:
    return AppSettings()
