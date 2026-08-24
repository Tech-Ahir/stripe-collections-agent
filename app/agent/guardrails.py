"""Letter guardrails (brief section 8).

    "Collection correspondence is regulated. The system prompt must forbid threats of legal
    action, credit reporting, or any consequence the client has not authorised, and forbid
    any claim about fees or interest not present in the Stripe data. Put this in the prompt
    AND in a tests/test_letter_guardrails.py suite that asserts the forbidden phrases never
    appear."

The prompt asks; this module enforces. Section 4 sets the precedent explicitly -- "a
duplicate is rejected by the store, not by the prompt" -- and the same logic applies to
compliance: a violation is refused at the tool boundary and returned to the agent as a
correctable error, so the model can rewrite and try again inside the same run.

Three independent checks:

1. **Forbidden claims.** Threats of legal action, credit reporting, and any mention of fees
   or interest -- because no fee or interest figure exists anywhere in the Stripe data this
   system reads, so any such claim is necessarily invented.
2. **Required facts.** Section 8: every letter must contain the customer name, invoice
   number, amount due, original due date, days overdue, the hosted payment link, and a
   contact route for disputes.
3. **No invented figures.** Every money amount, URL and invoice number in the body must be
   one this system actually retrieved. This is also where section 12's currency trap is
   caught: the raw minor-unit integer must never appear.

FDCPA scope is a legal question for the client's counsel, not an engineering one. What this
module provides is a mechanical floor, not legal advice -- knowledge-base note 03 says so
in the words the client should hear.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

# ----------------------------------------------------------------------------------
# 1. Forbidden claims
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForbiddenPattern:
    code: str
    pattern: re.Pattern[str]
    why: str


def _phrase(*alternatives: str) -> re.Pattern[str]:
    """Word-boundary alternation, case-insensitive."""
    joined = "|".join(re.escape(alternative) for alternative in alternatives)
    return re.compile(rf"\b(?:{joined})\b", re.IGNORECASE)


FORBIDDEN_PATTERNS: tuple[ForbiddenPattern, ...] = (
    ForbiddenPattern(
        "legal_action",
        _phrase(
            "legal action",
            "legal proceedings",
            "lawsuit",
            "litigation",
            "sue you",
            "we will sue",
            "take you to court",
            "court action",
            "small claims",
            "statutory demand",
            "writ",
            "our attorney",
            "our solicitor",
            "our lawyers",
            "our legal team",
            "judgment against",
        ),
        "Section 8 forbids threats of legal action. State the balance and ask for a "
        "payment date instead.",
    ),
    ForbiddenPattern(
        "credit_reporting",
        _phrase(
            "credit bureau",
            "credit bureaux",
            "credit report",
            "credit reporting",
            "credit rating",
            "credit score",
            "credit file",
            "credit reference agency",
            "blacklist",
            "blacklisted",
            "default notice",
            "adverse credit",
        ),
        "Section 8 forbids any claim about credit reporting. Nothing in this system is "
        "authorised to report anything to anyone.",
    ),
    ForbiddenPattern(
        "collection_escalation",
        _phrase(
            "collection agency",
            "collections agency",
            "debt collector",
            "debt collection agency",
            "recovery agent",
            "bailiff",
            "repossess",
            "repossession",
        ),
        "Section 8 forbids threatening a consequence the client has not authorised. "
        "Referral to a third party is such a consequence.",
    ),
    ForbiddenPattern(
        "fees_or_interest",
        _phrase(
            "late fee",
            "late fees",
            "late charge",
            "late charges",
            "interest will",
            "interest charges",
            "interest accrues",
            "interest accrued",
            "accrue interest",
            "penalty fee",
            "penalty charge",
            "penalties will",
            "surcharge",
            "administration fee",
            "admin fee",
            "1.5% per month",
            "per annum",
        ),
        "Section 8 forbids any claim about fees or interest not present in the Stripe "
        "data. No fee or interest figure exists anywhere in this system's data, so any "
        "such claim is invented.",
    ),
    ForbiddenPattern(
        "service_threat",
        _phrase(
            "suspend your account",
            "suspend service",
            "terminate your account",
            "terminate the contract",
            "withdraw credit",
            "stop supply",
            "cease supply",
            "cut off",
        ),
        "Section 8 forbids threatening a consequence the client has not authorised. "
        "Suspension or termination is not ours to promise.",
    ),
)

#: Deliberately NOT forbidden. Section 8's own tone ladder calls the 60+ day letter a
#: "final notice", and "escalation" is the word it uses for what follows. Blocking these
#: would make the firm and final tones unwritable, which is a different bug.
DELIBERATELY_ALLOWED = ("final notice", "final reminder", "escalation", "overdue", "past due")


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    matched: str
    why: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "matched": self.matched, "why": self.why}


def find_forbidden_claims(*texts: str) -> list[Violation]:
    """Every forbidden claim in the supplied text, in declaration order."""
    found: list[Violation] = []
    for text in texts:
        if not text:
            continue
        for rule in FORBIDDEN_PATTERNS:
            match = rule.pattern.search(text)
            if match:
                found.append(Violation(rule.code, match.group(0), rule.why))
    return found


# ----------------------------------------------------------------------------------
# 2. Required facts
# ----------------------------------------------------------------------------------

#: A route for the customer to dispute or query the balance. Section 8 requires one.
_DISPUTE_ROUTE = re.compile(
    r"(reply to this (?:e-?mail|message)|reply directly|respond to this (?:e-?mail|message)"
    r"|contact us|get in touch|let us know|raise a query|in error"
    r"|[\w.+-]+@[\w-]+\.[\w.]+)",
    re.IGNORECASE,
)


def date_variants(iso_date: str) -> list[str]:
    """Renderings of a date a letter might reasonably use.

    Requiring the raw ISO form would fail a perfectly good letter that writes
    "1 August 2026", so the check accepts any of these.
    """
    try:
        parsed = date.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return [str(iso_date)]
    day = parsed.day
    month = parsed.strftime("%B")
    short_month = parsed.strftime("%b")
    year = parsed.year
    return [
        iso_date,
        f"{day} {month} {year}",
        f"{day:02d} {month} {year}",
        f"{month} {day}, {year}",
        f"{month} {day} {year}",
        f"{day} {short_month} {year}",
        f"{short_month} {day}, {year}",
        f"{parsed.day:02d}/{parsed.month:02d}/{year}",
        f"{parsed.month:02d}/{parsed.day:02d}/{year}",
    ]


def find_missing_facts(body: str, facts: dict[str, Any]) -> list[str]:
    """Which of section 8's required elements are absent from the letter."""
    text = body or ""
    missing: list[str] = []

    name = facts.get("customer_name")
    if name and name not in text:
        missing.append(f"customer name ({name})")

    number = facts.get("invoice_number")
    if number and str(number) not in text:
        missing.append(f"invoice number ({number})")

    display = facts.get("amount_display")
    if display and display not in text:
        missing.append(f"amount due ({display})")

    due = facts.get("due_date")
    if due and not any(variant in text for variant in date_variants(str(due))):
        missing.append(f"original due date ({due})")

    days = facts.get("days_overdue")
    if days is not None and not re.search(rf"\b{int(days)}\b", text):
        missing.append(f"days overdue ({days})")

    link = facts.get("hosted_invoice_url")
    if link and link not in text:
        missing.append("hosted payment link")

    if not _DISPUTE_ROUTE.search(text):
        missing.append("a contact route for disputes")

    return missing


# ----------------------------------------------------------------------------------
# 3. No invented figures
# ----------------------------------------------------------------------------------

_MONEY = re.compile(r"(?:[$£€¥]\s?[\d,]+(?:\.\d{1,3})?)|(?:\b[\d,]+\.\d{2}\s?[A-Z]{3}\b)")
_URL = re.compile(r"https?://[^\s<>\)\]\"']+")
_BIG_NUMBER = re.compile(r"(?<![\d.,$£€¥])\d{4,}(?![\d.,])")


def _normalise_money(value: str) -> str:
    return re.sub(r"\s+", "", value)


def find_invented_figures(body: str, facts: dict[str, Any]) -> list[Violation]:
    """Money, links and identifiers in the letter that this system never retrieved.

    Section 8: "Facts are injected by the tool layer, never recalled by the model." This is
    the check that holds the model to it.
    """
    text = body or ""
    found: list[Violation] = []

    allowed_money = {
        _normalise_money(str(value))
        for key, value in facts.items()
        if key.endswith("_display") and value
    }
    for match in _MONEY.finditer(text):
        if _normalise_money(match.group(0)) not in allowed_money:
            found.append(
                Violation(
                    "invented_amount",
                    match.group(0),
                    "Every figure in a letter must come from a tool result. The only "
                    f"amount retrieved for this invoice is {facts.get('amount_display')}.",
                )
            )

    allowed_urls = {str(value) for key, value in facts.items() if key.endswith("_url") and value}
    for match in _URL.finditer(text):
        if match.group(0).rstrip(".,;:") not in allowed_urls:
            found.append(
                Violation(
                    "invented_link",
                    match.group(0),
                    "Payment links are never constructed. Use the hosted_invoice_url "
                    "exactly as the tool returned it.",
                )
            )

    # Section 12's currency trap: "$2,500 invoices become $250,000 in the demo."
    #
    # The boundaries are narrow on purpose. They must reject the integer only when it
    # stands alone as a number -- not when it is part of a longer figure ("250001",
    # "25000.50") and not when it is part of an identifier ("INV-25000", "PO25000") -- while
    # still catching it in ordinary prose, where a sentence's own comma or full stop follows
    # it immediately ("for 25000, originally due...").
    minor = facts.get("amount_due")
    if isinstance(minor, int) and len(str(abs(minor))) >= 4:
        standalone = rf"(?<![\w.,$£€¥-]){abs(minor)}(?!\d)(?!\.\d)(?![A-Za-z_])"
        if re.search(standalone, text):
            found.append(
                Violation(
                    "raw_minor_units",
                    str(minor),
                    f"{minor} is the amount in minor units, not a sum of money. Use "
                    f"{facts.get('amount_display')}, which is already formatted.",
                )
            )

    return found


# ----------------------------------------------------------------------------------
# The single entry point the draft tool calls
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardrailReport:
    forbidden: list[Violation]
    missing_facts: list[str]
    invented: list[Violation]

    @property
    def ok(self) -> bool:
        return not (self.forbidden or self.missing_facts or self.invented)

    def as_tool_error(self) -> dict[str, Any]:
        """A correctable error, so the agent can rewrite the letter inside the same run."""
        problems: list[dict[str, Any]] = []
        problems.extend(violation.as_dict() for violation in self.forbidden)
        problems.extend(violation.as_dict() for violation in self.invented)
        if self.missing_facts:
            problems.append(
                {
                    "code": "missing_required_facts",
                    "matched": ", ".join(self.missing_facts),
                    "why": "Section 8 requires every letter to contain the customer name, "
                    "invoice number, amount due, original due date, days overdue, the "
                    "hosted payment link, and a contact route for disputes.",
                }
            )
        return {
            "error": "letter_rejected",
            "message": (
                "This letter was not accepted. Nothing has been proposed. Fix the problems "
                "listed and call propose_collection_letter again."
            ),
            "problems": problems,
            "recoverable": True,
        }


def review_letter(*, subject: str, body: str, facts: dict[str, Any]) -> GuardrailReport:
    return GuardrailReport(
        forbidden=find_forbidden_claims(subject, body),
        missing_facts=find_missing_facts(body, facts),
        invented=find_invented_figures(body, facts),
    )
