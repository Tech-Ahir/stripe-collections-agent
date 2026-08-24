"""Documentation deliverables (brief sections 10, 11 and 15).

Acceptance criterion 10 is "the Obsidian vault opens and its internal links resolve". That is a
checkable fact, so it is checked here rather than clicked through by hand on the day.

The rest of this file guards the same class of problem in the other documents: a README that
promises a command the repository does not ship, or a boundary guide that names a file that has
moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT = REPO_ROOT / "knowledge-base"
DOCS = REPO_ROOT / "docs"

#: Section 11 lists these by number. The titles are ours; the numbering is the brief's.
REQUIRED_NOTES = (
    ("00", "Start Here"),
    ("01", "Architecture"),
    ("02", "The Approval Boundary"),
    ("03", "Agent Design"),
    ("04", "Stripe Integration"),
    ("05", "Data Model"),
    ("06", "API Reference"),
    ("07", "Running Locally"),
    ("08", "Decisions"),
    ("09", "Development Standards"),
    ("10", "Extending This"),
)

WIKI_LINK = re.compile(r"\[\[([^\]|#]+)")


@pytest.fixture(scope="module")
def notes() -> dict[str, str]:
    return {path.stem: path.read_text(encoding="utf-8") for path in VAULT.glob("*.md")}


# ----------------------------------------------------------------------------------
# The vault
# ----------------------------------------------------------------------------------


def test_the_vault_ships_in_the_repository():
    assert VAULT.is_dir(), "the knowledge base is a committed deliverable, not a link"
    assert (VAULT / ".obsidian" / "app.json").exists(), (
        "the folder should open as a vault without configuration"
    )


@pytest.mark.parametrize("number,title", REQUIRED_NOTES)
def test_every_note_section_11_lists_exists(notes, number, title):
    matching = [name for name in notes if name.startswith(number)]
    assert matching, f"section 11 requires note {number} ({title})"
    assert title.lower() in matching[0].lower()


def test_every_internal_link_resolves(notes):
    """Acceptance criterion 10, mechanically."""
    broken = [
        (source, target.strip())
        for source, body in notes.items()
        for target in WIKI_LINK.findall(body)
        if target.strip() not in notes
    ]
    assert broken == [], f"broken wiki links: {broken}"


def test_the_vault_is_actually_linked_together(notes):
    """A set of unconnected notes is a folder, not a knowledge base."""
    inbound = dict.fromkeys(notes, 0)
    for body in notes.values():
        for target in WIKI_LINK.findall(body):
            target = target.strip()
            if target in inbound:
                inbound[target] += 1

    orphans = [name for name, count in inbound.items() if count == 0]
    assert orphans == [], f"nothing links to: {orphans}"
    assert sum(inbound.values()) >= 30, "the notes should cross-reference each other properly"


def test_every_note_links_to_at_least_one_other(notes):
    for name, body in notes.items():
        assert WIKI_LINK.search(body), f"{name} is a dead end"


def test_the_two_notes_that_carry_judgement_are_substantial(notes):
    """Section 11: notes 08 and 10 "carry judgment that the generated text will not".

    Note 10 in particular "turns this trial into a reusable template and shapes what they ask
    for next, so it deserves real attention rather than a stub".
    """
    decisions = next(body for name, body in notes.items() if name.startswith("08"))
    extending = next(body for name, body in notes.items() if name.startswith("10"))

    # Every decision states what was rejected, not only what was chosen.
    assert decisions.count("**Rejected") >= 10, "one entry per decision, each with alternatives"
    assert decisions.count("**Context") >= 10
    assert len(extending) > 4000, "note 10 must not be a stub"
    for expected in ("invariant", "action type", "Postgres", "Alembic"):
        assert expected.lower() in extending.lower(), f"note 10 should cover {expected}"


# ----------------------------------------------------------------------------------
# The other documents
# ----------------------------------------------------------------------------------


def test_the_generated_openapi_document_is_committed():
    """Section 1: "Generated OpenAPI plus a written guide to the boundary"."""
    import json

    spec = json.loads((DOCS / "openapi.json").read_text(encoding="utf-8"))
    operations = {(method.upper(), path) for path, item in spec["paths"].items() for method in item}
    for required in [
        ("POST", "/v1/runs"),
        ("POST", "/v1/proposals/{proposal_id}/approve"),
        ("POST", "/v1/proposals/{proposal_id}/reject"),
        ("GET", "/v1/audit"),
        ("GET", "/healthz"),
    ]:
        assert required in operations, f"the exported document is missing {required}"


def test_the_boundary_guide_covers_all_seven_checks():
    guide = (DOCS / "boundary-guide.md").read_text(encoding="utf-8")
    for code in [
        "invalid_signature",
        "token_expired",
        "token_replayed",
        "not_approved",
        "approval_mismatch",
        "payload_modified",
        "idempotency",
    ]:
        assert code in guide, f"the boundary guide should explain {code}"
    assert "Check 4 is the one that matters" in guide


def test_the_readme_only_promises_scripts_that_exist():
    """A quickstart that names a missing file wastes the ten minutes it promises."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for referenced in re.findall(r"scripts/[a-z_]+\.py", readme):
        assert (REPO_ROOT / referenced).exists(), f"README references missing {referenced}"


def test_every_shipped_script_is_documented_somewhere():
    documented = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [REPO_ROOT / "README.md", DOCS / "boundary-guide.md", *VAULT.glob("*.md")]
    )
    for script in (REPO_ROOT / "scripts").glob("*.py"):
        assert f"scripts/{script.name}" in documented, f"{script.name} is undocumented"


def test_the_readme_states_the_two_stripe_constraints_a_reader_will_hit():
    """Both cost real time to discover. A reader should not have to rediscover them."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "back-dated" in readme and "test clock" in readme.lower()
    assert "reissues" in readme and "hosted_invoice_url" in readme


def test_claude_md_carries_the_seven_rules():
    """Section 12 step 1: the invariants live where an AI assistant reads them every session."""
    rules = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for fragment in [
        "TWO services",
        "NO published port",
        "NEVER imports",
        "READ and DRAFT tools ONLY",
        "seven checks",
        "read from the DB",
        "append-only",
    ]:
        assert fragment in rules, f"CLAUDE.md should state: {fragment}"


# ----------------------------------------------------------------------------------
# Configuration that is settable must be documented
# ----------------------------------------------------------------------------------

#: Set by docker-compose.yml rather than by an operator, so .env.example does not list them.
COMPOSE_OWNED_SETTINGS = {"database_url", "gateway_url", "outbox_dir"}


def test_env_example_documents_every_operator_facing_setting():
    """A setting nobody documents is a setting nobody knows they have.

    Two were found doing nothing at all -- RUN_TIMEOUT_SECONDS was declared and read by
    nothing, and a wedged model call could hold a worker indefinitely as a result. Requiring
    every setting to appear here means the next such gap is visible.
    """
    from app.config import AppSettings
    from gateway.config import GatewaySettings

    documented = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").upper()
    declared = set(AppSettings.model_fields) | set(GatewaySettings.model_fields)
    missing = sorted(
        name for name in declared - COMPOSE_OWNED_SETTINGS if name.upper() not in documented
    )
    assert missing == [], f"settings absent from .env.example: {missing}"


def test_the_readme_config_table_covers_the_settings_an_operator_changes():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for name in (
        "STRIPE_API_KEY_READ",
        "APPROVAL_SIGNING_SECRET",
        "ANTHROPIC_API_KEY",
        "EMAIL_ADAPTER",
        "PROPOSAL_TTL_HOURS",
        "RUN_TIMEOUT_SECONDS",
        "ENABLE_UNAPPROVED_ATTEMPT_DEMO",
    ):
        assert name in readme, f"the README should document {name}"
