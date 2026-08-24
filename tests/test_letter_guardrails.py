"""Letter guardrails (brief section 8).

    "Put this in the prompt AND in a tests/test_letter_guardrails.py suite that asserts the
    forbidden phrases never appear."

Section 8 also says to flag that FDCPA scope is a legal question for the client's counsel,
not an engineering one. What this suite establishes is a mechanical floor: the specific
claims section 8 names cannot get through, every required fact must be present, and no
figure can appear that this system did not retrieve.

The last block matters as much as the first. A guardrail that also blocks legitimate firm
language would make section 8's own tone ladder unwritable, which is a different bug with
the same cause.
"""

from __future__ import annotations

import pytest

from app.agent.guardrails import (
    DELIBERATELY_ALLOWED,
    FORBIDDEN_PATTERNS,
    date_variants,
    find_forbidden_claims,
    find_invented_figures,
    find_missing_facts,
    review_letter,
)

FACTS = {
    "invoice_id": "in_1001",
    "invoice_number": "INV-1001",
    "customer_name": "Acme Industries",
    "customer_email": "ap@acme.test",
    "amount_due": 25000,
    "amount_display": "$250.00",
    "currency": "usd",
    "due_date": "2026-08-01",
    "days_overdue": 9,
    "hosted_invoice_url": "https://invoice.stripe.com/i/test_1001",
    "deliverable": True,
}

GOOD_LETTER = """\
Dear Acme Industries,

Our records show that invoice INV-1001 for $250.00, originally due on 2026-08-01, is now 9
days past due.

You can settle it here: https://invoice.stripe.com/i/test_1001

If this has already been paid or you believe it is in error, reply to this email and we
will look into it right away.

Kind regards,
Servicia Collections
"""


def letter_with(fragment: str) -> str:
    return GOOD_LETTER.replace("Kind regards,", f"{fragment}\n\nKind regards,")


# ----------------------------------------------------------------------------------
# A good letter passes
# ----------------------------------------------------------------------------------


def test_a_compliant_letter_is_accepted():
    report = review_letter(
        subject="Invoice INV-1001 is 9 days past due", body=GOOD_LETTER, facts=FACTS
    )
    assert report.ok, (report.forbidden, report.missing_facts, report.invented)


# ----------------------------------------------------------------------------------
# The forbidden claims of section 8
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment,expected",
    [
        ("We will commence legal action if payment is not received.", "legal_action"),
        ("Our attorney will be in touch.", "legal_action"),
        ("This may result in litigation.", "legal_action"),
        ("We will take you to court.", "legal_action"),
        ("A judgment against your company would follow.", "legal_action"),
        ("This will be reported to the credit bureau.", "credit_reporting"),
        ("Your credit rating may be affected.", "credit_reporting"),
        ("A default notice will be issued.", "credit_reporting"),
        ("The account will be passed to a collection agency.", "collection_escalation"),
        ("A debt collector will contact you.", "collection_escalation"),
        ("A late fee of 5% now applies.", "fees_or_interest"),
        ("Interest accrues at 1.5% per month.", "fees_or_interest"),
        ("A penalty charge has been added.", "fees_or_interest"),
        ("We will suspend your account.", "service_threat"),
        ("We will terminate the contract.", "service_threat"),
    ],
)
def test_a_forbidden_claim_is_caught(fragment, expected):
    violations = find_forbidden_claims(letter_with(fragment))
    assert [v.code for v in violations] == [expected], fragment


def test_a_forbidden_claim_in_the_subject_line_is_caught_too():
    violations = find_forbidden_claims("FINAL WARNING: legal action pending", GOOD_LETTER)
    assert [v.code for v in violations] == ["legal_action"]


def test_a_forbidden_claim_carries_the_reason_the_model_needs():
    violations = find_forbidden_claims(letter_with("Interest accrues daily."))
    assert "not present in the Stripe data" in violations[0].why


def test_detection_is_case_insensitive():
    assert find_forbidden_claims(letter_with("LEGAL ACTION will follow."))
    assert find_forbidden_claims(letter_with("Credit Bureau notification pending."))


def test_every_declared_pattern_is_exercised_by_this_suite():
    """A pattern nobody tests is a pattern nobody knows works."""
    covered = set()
    for rule in FORBIDDEN_PATTERNS:
        covered.add(rule.code)
    tested = {
        "legal_action",
        "credit_reporting",
        "collection_escalation",
        "fees_or_interest",
        "service_threat",
    }
    assert covered == tested, f"untested forbidden pattern(s): {covered - tested}"


# ----------------------------------------------------------------------------------
# ...and firm language that must still be allowed
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", DELIBERATELY_ALLOWED)
def test_the_tone_ladders_own_vocabulary_is_not_blocked(phrase):
    """Section 8 calls the 60+ day letter a "final notice". Blocking that would make the
    tone ladder unwritable."""
    assert find_forbidden_claims(letter_with(f"This is a {phrase}.")) == []


@pytest.mark.parametrize(
    "fragment",
    [
        "This is a final notice before the account is escalated internally.",
        "The balance is now significantly overdue and we need a payment date.",
        "Please confirm when payment will be made, or contact us to discuss terms.",
        "We would rather resolve this directly with you.",
        "If payment has been sent, please let us know the date and reference.",
    ],
)
def test_legitimate_firm_language_passes(fragment):
    assert find_forbidden_claims(letter_with(fragment)) == [], fragment


# ----------------------------------------------------------------------------------
# Required facts (section 8)
# ----------------------------------------------------------------------------------


def test_a_complete_letter_is_missing_nothing():
    assert find_missing_facts(GOOD_LETTER, FACTS) == []


@pytest.mark.parametrize(
    "removed,expected_fragment",
    [
        ("Acme Industries", "customer name"),
        ("INV-1001", "invoice number"),
        ("$250.00", "amount due"),
        ("2026-08-01", "original due date"),
        ("https://invoice.stripe.com/i/test_1001", "hosted payment link"),
    ],
)
def test_a_missing_required_fact_is_reported(removed, expected_fragment):
    body = GOOD_LETTER.replace(removed, "")
    missing = find_missing_facts(body, FACTS)
    assert any(expected_fragment in entry for entry in missing), missing


def test_a_letter_with_no_dispute_route_is_reported():
    body = GOOD_LETTER.replace(
        "If this has already been paid or you believe it is in error, reply to this email "
        "and we\nwill look into it right away.",
        "Pay now.",
    )
    assert any("dispute" in entry for entry in find_missing_facts(body, FACTS))


@pytest.mark.parametrize(
    "route",
    [
        "Reply to this email if you have any questions.",
        "Please contact us if this is in error.",
        "Write to ar@servicia.ai with any queries.",
        "Let us know if this has already been paid.",
    ],
)
def test_any_reasonable_dispute_route_satisfies_the_requirement(route):
    body = GOOD_LETTER.replace(
        "If this has already been paid or you believe it is in error, reply to this email "
        "and we\nwill look into it right away.",
        route,
    )
    assert not any("dispute" in entry for entry in find_missing_facts(body, FACTS))


def test_the_due_date_may_be_written_the_way_a_person_would():
    """Requiring the raw ISO form would fail a perfectly good letter."""
    for rendering in ("1 August 2026", "August 1, 2026", "01/08/2026"):
        body = GOOD_LETTER.replace("2026-08-01", rendering)
        assert not any("due date" in entry for entry in find_missing_facts(body, FACTS)), rendering


def test_date_variants_covers_the_common_renderings():
    variants = date_variants("2026-08-01")
    assert "2026-08-01" in variants
    assert "1 August 2026" in variants
    assert "August 1, 2026" in variants


# ----------------------------------------------------------------------------------
# No invented figures -- including section 12's currency trap
# ----------------------------------------------------------------------------------


def test_the_raw_minor_unit_integer_is_caught():
    """Section 12: "$2,500 invoices become $250,000 in the demo.""" ""
    body = GOOD_LETTER.replace("$250.00", "25000")
    violations = find_invented_figures(body, FACTS)
    codes = {v.code for v in violations}
    assert "raw_minor_units" in codes
    reason = next(v for v in violations if v.code == "raw_minor_units").why
    assert "minor units" in reason
    assert "$250.00" in reason


def test_an_amount_that_was_never_retrieved_is_caught():
    body = GOOD_LETTER.replace("$250.00", "$2,500.00")
    violations = find_invented_figures(body, FACTS)
    assert [v.code for v in violations] == ["invented_amount"]
    assert violations[0].matched == "$2,500.00"


def test_a_second_invented_amount_alongside_the_real_one_is_caught():
    body = letter_with("A further $99.00 in charges has been added.")
    violations = find_invented_figures(body, FACTS)
    assert any(v.matched == "$99.00" for v in violations)


def test_a_constructed_payment_link_is_caught():
    body = GOOD_LETTER.replace(
        "https://invoice.stripe.com/i/test_1001", "https://pay.servicia.ai/invoice/1001"
    )
    violations = find_invented_figures(body, FACTS)
    assert [v.code for v in violations] == ["invented_link"]
    assert "never constructed" in violations[0].why


def test_the_real_amount_and_link_produce_no_violations():
    assert find_invented_figures(GOOD_LETTER, FACTS) == []


def test_a_trailing_period_after_the_link_is_not_treated_as_a_different_url():
    body = GOOD_LETTER.replace(
        "https://invoice.stripe.com/i/test_1001",
        "https://invoice.stripe.com/i/test_1001.",
    )
    assert find_invented_figures(body, FACTS) == []


def test_a_short_number_is_not_mistaken_for_minor_units():
    """Invoice references and small counts must not trip the currency check."""
    facts = {**FACTS, "amount_due": 500, "amount_display": "$5.00"}
    body = (
        GOOD_LETTER.replace("$250.00", "$5.00")
        + "\nThis relates to purchase order 500 and batch 12.\n"
    )
    assert [v.code for v in find_invented_figures(body, facts)] == []


# ----------------------------------------------------------------------------------
# The report the agent receives
# ----------------------------------------------------------------------------------


def test_a_rejected_letter_comes_back_as_a_correctable_error():
    report = review_letter(
        subject="Pay now",
        body="Dear Acme Industries, pay 25000 or we will take legal action.",
        facts=FACTS,
    )
    assert not report.ok
    error = report.as_tool_error()

    assert error["error"] == "letter_rejected"
    assert error["recoverable"] is True
    assert "Nothing has been proposed" in error["message"]
    assert "call propose_collection_letter again" in error["message"]

    codes = {problem["code"] for problem in error["problems"]}
    assert "legal_action" in codes
    assert "raw_minor_units" in codes
    assert "missing_required_facts" in codes


def test_every_problem_carries_a_reason_the_model_can_act_on():
    report = review_letter(subject="s", body="Pay up.", facts=FACTS)
    for problem in report.as_tool_error()["problems"]:
        assert problem["why"], problem
        assert len(problem["why"]) > 20, problem


def test_an_empty_letter_is_rejected_rather_than_passed():
    report = review_letter(subject="", body="", facts=FACTS)
    assert not report.ok
    assert len(report.missing_facts) >= 6


@pytest.mark.parametrize(
    "fragment,should_flag",
    [
        ("The balance is 25000, please settle it.", True),
        ("The balance is 25000. Please settle it.", True),
        ("The balance is 25000", True),
        ("Amount: 25000 USD", True),
        ("This concerns invoice INV-25000.", False),
        ("Purchase order PO25000 refers.", False),
        ("The figure 250001 is unrelated.", False),
        ("A total of 25000.50 was quoted.", False),
        ("Our reference is 25000A.", False),
    ],
)
def test_the_minor_units_check_has_narrow_boundaries(fragment, should_flag):
    """It must catch the integer in prose without flagging identifiers or longer figures."""
    body = letter_with(fragment)
    codes = {v.code for v in find_invented_figures(body, FACTS)}
    assert ("raw_minor_units" in codes) is should_flag, fragment


# ----------------------------------------------------------------------------------
# Payment links: Stripe reissues them on every read
#
# Exact matching was tried first and rejected a letter carrying a perfectly genuine link,
# because the link had been reissued between the read and the draft. These tests pin both
# halves of the fix, against numbers measured from live test mode.
# ----------------------------------------------------------------------------------

# Two reads of the SAME invoice. Identical for 140 of 159 characters; the tail is a
# per-read timestamp and nonce.
LINK_READ_1 = (
    "https://invoice.stripe.com/i/acct_1TmfapEg9A7GQeAT/test_YWNjdF8xVG1mYXBFZzlBN0dRZUFU"
    "LF9WODhVOElyaE1vbXpwUmZadVdwenNWSlFzQlpiYkdYLDE3ODEwMTk2Mw0200bU9TN0k8?s=ap"
)
LINK_READ_2 = (
    "https://invoice.stripe.com/i/acct_1TmfapEg9A7GQeAT/test_YWNjdF8xVG1mYXBFZzlBN0dRZUFU"
    "LF9WODhVOElyaE1vbXpwUmZadVdwenNWSlFzQlpiYkdYLDE3ODEwMTk2OA0200Boop1ARG?s=ap"
)
# A DIFFERENT invoice in the same account. Diverges at character 92.
LINK_OTHER_INVOICE = (
    "https://invoice.stripe.com/i/acct_1TmfapEg9A7GQeAT/test_YWNjdF8xVG1mYXBFZzlBN0dRZUFU"
    "LF9WODhVaFE0T1lwdEVjMHUxaXFBTGVPTEd6NDNJV0lQLDE3ODEwMjEwMg0200na5wihYn?s=ap"
)


def test_the_measured_premise_still_holds():
    """If these fixtures ever stop matching live Stripe, the threshold needs re-measuring."""
    from app.agent.guardrails import STABLE_LINK_PREFIX

    def common(a: str, b: str) -> int:
        count = 0
        for left, right in zip(a, b, strict=False):
            if left != right:
                break
            count += 1
        return count

    same_invoice = common(LINK_READ_1, LINK_READ_2)
    different_invoice = common(LINK_READ_1, LINK_OTHER_INVOICE)
    assert LINK_READ_1 != LINK_READ_2, "Stripe reissues the link on every read"
    assert same_invoice == 140
    assert different_invoice == 92
    assert different_invoice < STABLE_LINK_PREFIX <= same_invoice, (
        "the threshold must tell invoices apart while tolerating a reissue"
    )


def test_a_reissued_link_for_the_same_invoice_is_accepted():
    """The bug this fixed: a genuine link rejected because Stripe had reissued it."""
    facts = {**FACTS, "hosted_invoice_url": LINK_READ_1}
    body = GOOD_LETTER.replace("https://invoice.stripe.com/i/test_1001", LINK_READ_2)

    assert find_invented_figures(body, facts) == []
    assert not any("payment link" in entry for entry in find_missing_facts(body, facts))


def test_a_link_for_a_different_invoice_is_still_rejected():
    """Tolerating the reissue must not tolerate pointing at someone else's invoice."""
    facts = {**FACTS, "hosted_invoice_url": LINK_READ_1}
    body = GOOD_LETTER.replace("https://invoice.stripe.com/i/test_1001", LINK_OTHER_INVOICE)

    violations = find_invented_figures(body, facts)
    assert [v.code for v in violations] == ["invented_link"]


@pytest.mark.parametrize(
    "forged",
    [
        "https://invoice.stripe.com.evil.test/i/acct_1TmfapEg9A7GQeAT/test_YWNjdF8x",
        "http://invoice.stripe.com/i/acct_1TmfapEg9A7GQeAT/test_YWNjdF8x",
        "https://pay.servicia.ai/i/acct_1TmfapEg9A7GQeAT/test_YWNjdF8x",
        "https://invoice.stripe.com/i/acct_OTHERACCOUNT/test_YWNjdF8x",
    ],
)
def test_a_link_at_the_wrong_host_or_account_is_rejected(forged):
    facts = {**FACTS, "hosted_invoice_url": LINK_READ_1}
    body = GOOD_LETTER.replace("https://invoice.stripe.com/i/test_1001", forged)
    assert any(v.code == "invented_link" for v in find_invented_figures(body, facts))


def test_link_identity_ignores_the_query_string_and_trailing_punctuation():
    from app.agent.guardrails import link_identity, links_match

    assert link_identity(LINK_READ_1) == link_identity(LINK_READ_1 + "&extra=1")
    assert links_match(LINK_READ_1 + ".", LINK_READ_1)
    assert links_match(LINK_READ_1, LINK_READ_2)
    assert not links_match(LINK_READ_1, LINK_OTHER_INVOICE)
    assert not links_match("", LINK_READ_1)
    assert not links_match(LINK_READ_1, "")


def test_a_short_link_still_has_to_match_exactly():
    """Below the threshold there is nothing to truncate, so the comparison stays strict."""
    from app.agent.guardrails import links_match

    assert links_match("https://invoice.stripe.com/i/test_1001", FACTS["hosted_invoice_url"])
    assert not links_match("https://invoice.stripe.com/i/test_9999", FACTS["hosted_invoice_url"])
