"""Print the agent's complete toolset, exactly as the model receives it.

    docker compose exec app python scripts/show_agent_tools.py
    docker compose exec app python scripts/show_agent_tools.py --json

Section 12, step 4 of "before you call it done": "Open app/agent/tools/ and confirm by eye
that no send capability exists." This is that check, shipped, so it takes five seconds
rather than a code read -- and so the answer comes from the running code rather than from
someone's memory of it.

It needs no Stripe key and no Anthropic key: the schema is static, and the reader is
stubbed out because inspecting the toolset requires nothing of it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"

#: Anything matching in a tool name would mean the agent can reach the outside world.
SEND_WORDS = ("send", "email", "deliver", "dispatch", "execute", "transmit", "mail", "notify")


class _NoStripeNeeded:
    def list_overdue_invoices(self, **_kwargs):
        return []

    def get_invoice(self, _invoice_id):
        return {}

    def get_customer(self, _customer_id):
        return {}

    def get_payment_history(self, _customer_id, *, limit=10):
        return []


def build():
    # A throwaway database, so this can run anywhere without touching the real one.
    os.environ.setdefault(
        "DATABASE_URL", "sqlite:///" + tempfile.mkdtemp().replace("\\", "/") + "/tools.db"
    )
    from app.agent.tools.registry import build_registry
    from app.store.repositories import RunStore
    from shared.schema import ensure_schema

    ensure_schema()
    run_id = RunStore.create(goal="tool schema inspection", operator_id="inspector", params={})
    return build_registry(
        reader=_NoStripeNeeded(), store=RunStore(run_id), max_proposals=10, ttl_hours=72
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw wire JSON")
    args = parser.parse_args()

    registry = build()
    schemas = registry.anthropic_schemas()
    classes = registry.classification()

    if args.json:
        print(json.dumps(schemas, indent=2))
        return 0

    print(f"{BOLD}The agent's complete toolset{RESET}")
    print(f"{DIM}Exactly what goes in the `tools` parameter of the Messages API request.{RESET}\n")

    for tool in schemas:
        name = tool["name"]
        schema = tool["input_schema"]
        print(f"  {BOLD}[{classes[name]:5s}]{RESET} {name}({', '.join(schema['properties'])})")
        for field, spec in schema["properties"].items():
            kind = spec.get("type")
            rendered = "|".join(kind) if isinstance(kind, list) else str(kind)
            if spec.get("enum"):
                rendered += " " + str(spec["enum"])
            print(f"          {field}: {rendered}")
        print()

    wire = json.dumps(schemas)
    action_tools = [name for name, cls in classes.items() if cls == "ACTION"]
    suspicious = [name for name in classes if any(word in name.lower() for word in SEND_WORDS)]

    checks = [
        ("tools declared", len(classes), len(classes) == 5),
        (
            "classes present",
            sorted(set(classes.values())),
            sorted(set(classes.values())) == ["DRAFT", "READ"],
        ),
        ("ACTION tools", len(action_tools), not action_tools),
        ("tools whose name implies acting", suspicious or "none", not suspicious),
        (
            "'send_collection_letter' in the wire JSON",
            "send_collection_letter" in wire,
            "send_collection_letter" not in wire,
        ),
    ]
    print(f"  {BOLD}{'-' * 68}{RESET}")
    ok = True
    for label, value, passed in checks:
        mark = f"{GREEN}ok{RESET}" if passed else f"{RED}FAIL{RESET}"
        ok = ok and passed
        print(f"  [{mark}] {label:<42} {value}")
    print(f"  {BOLD}{'-' * 68}{RESET}")
    print(
        f"\n  {GREEN if ok else RED}"
        + (
            "The agent has no capability to act on the outside world."
            if ok
            else "THE BOUNDARY HAS BEEN BREACHED. See CLAUDE.md rule 4."
        )
        + RESET
    )
    print(
        f"  {DIM}Its only route there is a proposal a human approves, which the gateway "
        f"executes.{RESET}\n"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
