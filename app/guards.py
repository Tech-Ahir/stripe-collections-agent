"""Startup guards for the agent service.

CLAUDE.md rule 3 says the agent service never touches an action credential. Import-linter
proves it cannot *import* the code that would use one; this module proves it cannot *see*
one either. If a write-capable Stripe key, an SMTP password or a Resend key is present in
the agent's environment, compose has been misconfigured and the credential split has been
erased -- so the service refuses to start rather than run in a state that would pass a
demo while failing the design.

Failing loudly here is deliberate. A silent warning would be discovered by nobody.
"""

from __future__ import annotations

import os

#: Environment variables that must never be visible to the agent service.
#:
#: ``STRIPE_API_KEY_SEED`` belongs here even though it only ever seeds test fixtures: it is a
#: standard, write-capable Stripe key, and "it is only used by a script" is a fact about
#: intent rather than about capability. It lives in exactly one place -- the `seed` compose
#: service, a one-off container that serves no traffic.
ACTION_CREDENTIAL_VARS = (
    "STRIPE_API_KEY_WRITE",
    "STRIPE_API_KEY_SEED",
    "SMTP_PASSWORD",
    "RESEND_API_KEY",
)


class BoundaryViolation(RuntimeError):
    """Raised at startup when the agent service can see a credential it must not have."""


def find_action_credentials(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [name for name in ACTION_CREDENTIAL_VARS if (env.get(name) or "").strip()]


def assert_no_action_credentials(environ: dict[str, str] | None = None) -> None:
    found = find_action_credentials(environ)
    if found:
        raise BoundaryViolation(
            "The agent service can see action credentials in its environment: "
            + ", ".join(found)
            + ". These belong to the gateway only (CLAUDE.md rule 3). Remove them from "
            "the app service's environment in docker-compose.yml."
        )
