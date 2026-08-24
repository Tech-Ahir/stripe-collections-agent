"""The import boundary (CLAUDE.md rule 3, brief section 2).

"No module imported by the agent may import the email adapter or the write-capable Stripe
client. Enforce with an import-linter contract in CI so a future contributor cannot
accidentally erase the boundary."

Three independent layers are asserted here:

1. **Static** -- `lint-imports` runs the contracts in `.importlinter`. This catches an
   import that exists in the source even if no test ever executes that code path.
2. **Runtime** -- importing the agent service must not pull `gateway` into `sys.modules`.
   This catches a lazy import hidden inside a function, which static analysis of a single
   module graph can miss.
3. **Credential** -- the agent's settings class must have no field for an action
   credential, and the service must refuse to boot if one is visible in its environment.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _lint_imports_command() -> list[str]:
    """Resolve the real `lint-imports` entry point.

    `python -m importlinter.cli` exits 0 without evaluating anything, which would make
    the contract check silently vacuous -- so it is not used. The console script is
    resolved from the interpreter's own bin/Scripts directory first, so a venv is
    preferred over anything else on PATH.
    """
    bin_dir = Path(sys.executable).parent
    for candidate in (bin_dir / "lint-imports.exe", bin_dir / "lint-imports"):
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which("lint-imports")
    if found:
        return [found]
    raise AssertionError("lint-imports is not installed; `pip install import-linter`")


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _lint_imports_command(),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


# ----------------------------------------------------------------------------------
# 1. Static: the contracts themselves
# ----------------------------------------------------------------------------------


def test_import_linter_contracts_all_pass():
    """`lint-imports` must exit 0 having actually evaluated contracts. As CI runs it."""
    result = _run_lint_imports()
    assert result.returncode == 0, (
        "import-linter contracts failed -- the boundary has been breached:\n"
        + result.stdout
        + result.stderr
    )
    assert "0 broken" in result.stdout, (
        "lint-imports produced no contract summary, so it enforced nothing:\n"
        + result.stdout
        + result.stderr
    )


def test_every_contract_in_the_file_is_actually_checked():
    """A contract that is silently skipped enforces nothing.

    Guards against a contract being disabled by a typo in its section header, which
    import-linter would ignore without failing.
    """
    contract_file = (REPO_ROOT / ".importlinter").read_text(encoding="utf-8")
    declared = contract_file.count("[importlinter:contract:")
    result = _run_lint_imports()
    reported = result.stdout.count("KEPT") + result.stdout.count("BROKEN")
    assert reported == declared, (
        f"{declared} contracts are declared but {reported} were evaluated.\n" + result.stdout
    )


# ----------------------------------------------------------------------------------
# 2. Runtime: what actually gets loaded
# ----------------------------------------------------------------------------------

_RUNTIME_PROBE = """
import sys
import app.main  # noqa: F401  -- the whole agent service, transitively

leaked = sorted(m for m in sys.modules if m == "gateway" or m.startswith("gateway."))
forbidden = sorted(m for m in sys.modules if m in ("smtplib", "resend"))
print("GATEWAY_MODULES=" + ",".join(leaked))
print("MAIL_MODULES=" + ",".join(forbidden))
"""


def test_importing_the_agent_service_does_not_load_the_gateway():
    """A fresh interpreter imports the whole agent service. No gateway module appears."""
    result = subprocess.run(
        [sys.executable, "-c", _RUNTIME_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**_clean_env(), "DATABASE_URL": "sqlite:///./_probe.db"},
    )
    assert result.returncode == 0, result.stderr
    gateway_line = _line(result.stdout, "GATEWAY_MODULES=")
    mail_line = _line(result.stdout, "MAIL_MODULES=")
    assert gateway_line == "", f"the agent service loaded gateway modules: {gateway_line}"
    assert mail_line == "", f"the agent service loaded mail modules: {mail_line}"


def _line(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"probe did not report {prefix!r}; got:\n{stdout}")


def _clean_env() -> dict[str, str]:
    from tests.conftest import minimal_subprocess_env

    return minimal_subprocess_env()


# ----------------------------------------------------------------------------------
# 3. Credential: the agent cannot even name the write key
# ----------------------------------------------------------------------------------


def test_app_settings_declares_no_action_credential_fields():
    from app.config import AppSettings

    fields = set(AppSettings.model_fields)
    forbidden = {
        "stripe_api_key_write",
        "smtp_password",
        "smtp_host",
        "resend_api_key",
        "email_adapter",
    }
    overlap = fields & forbidden
    assert overlap == set(), f"AppSettings must not declare action credentials: {overlap}"


def test_gateway_settings_declares_no_agent_credentials():
    """Symmetry: the gateway has no business holding an Anthropic key either."""
    from gateway.config import GatewaySettings

    fields = set(GatewaySettings.model_fields)
    overlap = fields & {"anthropic_api_key", "stripe_api_key_read"}
    assert overlap == set(), f"GatewaySettings must not declare agent credentials: {overlap}"


@pytest.mark.parametrize("variable", ["STRIPE_API_KEY_WRITE", "SMTP_PASSWORD", "RESEND_API_KEY"])
def test_agent_service_refuses_to_start_if_it_can_see_an_action_credential(variable: str):
    from app.guards import BoundaryViolation, assert_no_action_credentials

    with pytest.raises(BoundaryViolation) as raised:
        assert_no_action_credentials({variable: "sk_live_should_never_be_here"})
    assert variable in str(raised.value)


def test_agent_service_starts_cleanly_with_only_its_own_credentials():
    from app.guards import assert_no_action_credentials

    assert_no_action_credentials(
        {
            "STRIPE_API_KEY_READ": "rk_test_abc",
            "ANTHROPIC_API_KEY": "sk-ant-abc",
            "APPROVAL_SIGNING_SECRET": "x" * 64,
        }
    )


def test_blank_action_credential_is_not_treated_as_present():
    """Compose sometimes passes an empty string. An empty value is not a credential."""
    from app.guards import find_action_credentials

    assert find_action_credentials({"STRIPE_API_KEY_WRITE": ""}) == []
    assert find_action_credentials({"STRIPE_API_KEY_WRITE": "   "}) == []
