"""The deployment boundary (CLAUDE.md rules 1-3, brief sections 2 and 10).

Section 12 of the brief names four specific ways this architecture gets quietly dismantled
by someone trying to be helpful:

    "Merge the gateway into the app"        -> both services must exist, separately
    "Publish the gateway port to make        -> the gateway must declare no ports
     testing easier"
    "Use one Stripe key for both services"  -> each service's env must hold only its own
    (and, by extension) ship one image      -> neither image may contain the other's code

Every one of those produces a system that demos identically and fails the evaluation, and
none of them would be caught by a functional test. So they are caught here, by reading the
deployment configuration itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Credentials that let a process act on the outside world.
ACTION_CREDENTIALS = {
    "STRIPE_API_KEY_WRITE",
    "SMTP_PASSWORD",
    "RESEND_API_KEY",
    "STRIPE_API_KEY_SEED",
}

#: Credentials that let a process read Stripe or drive the model.
AGENT_CREDENTIALS = {"ANTHROPIC_API_KEY", "STRIPE_API_KEY_READ"}


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def _env_keys(service: dict) -> set[str]:
    env = service.get("environment") or {}
    if isinstance(env, list):  # the "KEY=value" list form
        return {entry.split("=", 1)[0] for entry in env}
    return set(env)


# ----------------------------------------------------------------------------------
# Rule 1: two services
# ----------------------------------------------------------------------------------


def test_both_services_exist_separately(compose):
    services = compose["services"]
    assert "app" in services, "the agent service is missing"
    assert "gateway" in services, "the action gateway is missing -- was it merged into app?"
    assert services["app"]["build"]["dockerfile"] != services["gateway"]["build"]["dockerfile"]


def test_the_app_depends_on_the_gateway_being_healthy(compose):
    depends = compose["services"]["app"]["depends_on"]
    assert "gateway" in depends
    assert depends["gateway"]["condition"] == "service_healthy"


# ----------------------------------------------------------------------------------
# Rule 2: the gateway publishes nothing
# ----------------------------------------------------------------------------------


def test_the_gateway_publishes_no_port(compose):
    """The single most important assertion in this file.

    If this fails, `curl localhost:9000` connects from the host and the strongest moment
    of the handoff demo is gone. Acceptance criterion 7.
    """
    gateway = compose["services"]["gateway"]
    assert "ports" not in gateway, (
        "the gateway must publish NO port -- it is reachable only on the internal "
        f"Docker network. Found: {gateway.get('ports')!r}"
    )
    assert "expose" not in gateway or not gateway["expose"]


def test_the_app_publishes_exactly_the_public_port(compose):
    assert compose["services"]["app"]["ports"] == ["8000:8000"]


def test_no_service_other_than_app_and_mailpit_publishes_a_port(compose):
    """A future contributor adding a debug port to the gateway fails here."""
    allowed = {"app", "mailpit"}
    publishing = {name for name, service in compose["services"].items() if service.get("ports")}
    assert publishing <= allowed, f"unexpected published ports on: {publishing - allowed}"


# ----------------------------------------------------------------------------------
# Rule 3: split credentials
# ----------------------------------------------------------------------------------


def test_the_app_environment_holds_no_action_credential(compose):
    leaked = _env_keys(compose["services"]["app"]) & ACTION_CREDENTIALS
    assert leaked == set(), (
        f"the agent service must never see an action credential, found: {sorted(leaked)}"
    )


def test_the_gateway_environment_holds_no_agent_credential(compose):
    leaked = _env_keys(compose["services"]["gateway"]) & AGENT_CREDENTIALS
    assert leaked == set(), (
        f"the gateway must never see an agent credential, found: {sorted(leaked)}"
    )


def test_the_signing_secret_is_shared_and_required_by_both(compose):
    """The one secret both services must hold -- and nothing else may."""
    for name in ("app", "gateway"):
        env = compose["services"][name]["environment"]
        assert "APPROVAL_SIGNING_SECRET" in env
        # `:?` makes compose refuse to start rather than mint unverifiable tokens.
        assert ":?" in str(env["APPROVAL_SIGNING_SECRET"]), (
            f"{name} should fail fast when APPROVAL_SIGNING_SECRET is unset"
        )


def test_the_default_email_adapter_cannot_email_anyone(compose):
    """Cloning this repo must not be able to send mail to a real person by accident."""
    adapter = compose["services"]["gateway"]["environment"]["EMAIL_ADAPTER"]
    assert "outbox" in str(adapter), f"default email adapter must be outbox, got {adapter!r}"


def test_stripe_invoice_send_is_off_by_default(compose):
    flag = compose["services"]["gateway"]["environment"]["ENABLE_STRIPE_INVOICE_SEND"]
    assert "false" in str(flag).lower()


# ----------------------------------------------------------------------------------
# The images themselves
# ----------------------------------------------------------------------------------


def _dockerfile(name: str) -> str:
    return (REPO_ROOT / "docker" / name).read_text(encoding="utf-8")


def _copied_paths(dockerfile: str) -> list[str]:
    paths: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("COPY ") and "--from=" not in stripped:
            paths.extend(stripped.split()[1:-1])
    return paths


def test_the_agent_image_does_not_contain_the_gateway_source():
    """The boundary is enforced by the filesystem too, not only by import-linter."""
    copied = _copied_paths(_dockerfile("app.Dockerfile"))
    assert not any(p.startswith("gateway") for p in copied), (
        f"the agent image must not contain gateway source, copies: {copied}"
    )
    assert any(p.startswith("app") for p in copied)


def test_the_gateway_image_does_not_contain_the_agent_source():
    copied = _copied_paths(_dockerfile("gateway.Dockerfile"))
    assert not any(p.startswith("app") for p in copied), (
        f"the gateway image must not contain agent source, copies: {copied}"
    )
    assert any(p.startswith("gateway") for p in copied)


def test_the_gateway_image_exposes_no_port():
    """Instructions only -- the file's comment explaining the absence does not count."""
    instructions = [
        line.strip()
        for line in _dockerfile("gateway.Dockerfile").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(line.upper().startswith("EXPOSE") for line in instructions)


def test_the_test_image_is_the_only_one_holding_both_services():
    """It has to, in order to prove the two stay apart. Hence the `test` profile."""
    copied = _copied_paths(_dockerfile("tests.Dockerfile"))
    assert any(p.startswith("app") for p in copied)
    assert any(p.startswith("gateway") for p in copied)


def test_the_test_runner_never_starts_with_docker_compose_up(compose):
    assert compose["services"]["tests"]["profiles"] == ["test"]


# ----------------------------------------------------------------------------------
# The seeding credential, and the command the README promises
# ----------------------------------------------------------------------------------


def test_the_seed_key_lives_in_exactly_one_service(compose):
    """It is a standard, write-capable Stripe key. Only the one-off container may see it."""
    holders = {
        name
        for name, service in compose["services"].items()
        if "STRIPE_API_KEY_SEED" in _env_keys(service)
    }
    assert holders == {"seed"}, f"STRIPE_API_KEY_SEED should be on `seed` alone, found {holders}"


def test_the_seed_service_never_starts_with_docker_compose_up(compose):
    assert compose["services"]["seed"]["profiles"] == ["seed"]


def test_the_seed_service_serves_no_traffic(compose):
    seed = compose["services"]["seed"]
    assert "ports" not in seed
    assert seed["entrypoint"] == ["python"], "a one-off runner, not a server"


def test_the_readme_seed_command_names_a_service_that_has_the_key(compose):
    """The first version of this command used the `tests` service, which has no Stripe key.

    So the README's own quickstart step failed, which is worse than a missing step: it makes
    the ten-minute promise untrue for anyone following it literally. This test reads the
    command out of the README and checks the service it names can actually run it.
    """
    import re

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    commands = re.findall(
        r"docker compose run --rm (?:--entrypoint python )?([a-z]+) "
        r"scripts/seed_stripe_test_data\.py",
        readme,
    )
    assert commands, "the README should document how to seed"
    for service in set(commands):
        assert service in compose["services"], f"README names unknown service {service!r}"
        assert "STRIPE_API_KEY_SEED" in _env_keys(compose["services"][service]), (
            f"README tells the reader to seed with `{service}`, which has no Stripe key"
        )


# ----------------------------------------------------------------------------------
# Interpolation applies to the whole file, profiles or not
# ----------------------------------------------------------------------------------

#: Variables that genuinely must be present for `docker compose up` to be meaningful. A `:?`
#: on anything else makes starting the system fail over a value it does not need yet.
REQUIRED_TO_START = {"STRIPE_API_KEY_READ", "APPROVAL_SIGNING_SECRET"}


def test_only_variables_needed_to_start_the_system_are_mandatory(compose):
    """Compose interpolates the ENTIRE file before deciding which services to act on.

    So a `${FOO:?...}` on a service behind a profile still makes plain `docker compose up`
    fail when FOO is unset. That is exactly what happened: a `:?` on the profile-gated `seed`
    service broke CI and would have broken the README's own step 2 for any reader who had not
    reached step 3 yet.
    """
    mandatory: dict[str, str] = {}
    for name, service in compose["services"].items():
        env = service.get("environment") or {}
        values = env.values() if isinstance(env, dict) else env
        for value in values:
            for match in re.finditer(r"\$\{([A-Z_]+):\?", str(value)):
                mandatory[match.group(1)] = name

    unexpected = {var: svc for var, svc in mandatory.items() if var not in REQUIRED_TO_START}
    assert unexpected == {}, (
        "these variables are mandatory for `docker compose up` but are not needed to start "
        f"the system: {unexpected}"
    )


def test_a_profiled_service_never_makes_a_variable_mandatory(compose):
    """Stated separately because it is the specific trap, and it is not obvious."""
    for name, service in compose["services"].items():
        if not service.get("profiles"):
            continue
        env = service.get("environment") or {}
        values = env.values() if isinstance(env, dict) else env
        for value in values:
            assert ":?" not in str(value), (
                f"service {name!r} is behind a profile, so it is not started by "
                f"`docker compose up` -- but `:?` in {value!r} would still fail that command"
            )
