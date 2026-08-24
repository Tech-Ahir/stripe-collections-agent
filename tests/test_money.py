"""Currency handling (brief section 7).

Section 12 names the failure this suite prevents: "$2,500 invoices become $250,000 in the
demo." Every assertion here is a specific way that happens.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.money import (
    CurrencyError,
    describe,
    exponent,
    format_amount,
    to_decimal,
)

# ----------------------------------------------------------------------------------
# The headline case from the brief
# ----------------------------------------------------------------------------------


def test_2500_usd_is_twenty_five_dollars_not_two_thousand_five_hundred():
    """The brief's own example: "2500 is $25.00"."""
    assert format_amount(2500, "usd") == "$25.00"


def test_a_2500_dollar_invoice_renders_as_2500_and_not_250000():
    """Section 12's failure mode, stated as an assertion."""
    assert format_amount(250000, "usd") == "$2,500.00"
    assert format_amount(250000, "usd") != "$250,000.00"


def test_thousands_separators_appear_where_a_reader_expects_them():
    assert format_amount(123456789, "usd") == "$1,234,567.89"
    assert format_amount(100000, "gbp") == "£1,000.00"


# ----------------------------------------------------------------------------------
# Not every currency has two decimal places
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "currency,expected_exponent",
    [
        ("usd", 2),
        ("eur", 2),
        ("gbp", 2),
        ("jpy", 0),
        ("krw", 0),
        ("vnd", 0),
        ("clp", 0),
        ("kwd", 3),
        ("bhd", 3),
        ("tnd", 3),
    ],
)
def test_the_exponent_comes_from_the_currency(currency, expected_exponent):
    assert exponent(currency) == expected_exponent


def test_a_zero_decimal_currency_is_not_divided_at_all():
    """2500 JPY is Y2,500. Dividing by 100 would understate it a hundredfold -- and
    overstating a debt by 100x is the worse direction for a collections letter."""
    assert format_amount(2500, "jpy") == "¥2,500"
    assert format_amount(2500, "jpy") != "¥25.00"


def test_a_three_decimal_currency_keeps_three_places():
    assert format_amount(2500, "kwd") == "2.500 KWD"
    assert format_amount(1234567, "bhd") == "1,234.567 BHD"


def test_case_and_whitespace_in_the_currency_code_do_not_change_the_answer():
    assert format_amount(2500, "USD") == format_amount(2500, " usd ") == "$25.00"


# ----------------------------------------------------------------------------------
# Exactness
# ----------------------------------------------------------------------------------


def test_conversion_is_exact_and_returns_a_decimal():
    assert to_decimal(2500, "usd") == Decimal("25.00")
    assert isinstance(to_decimal(2500, "usd"), Decimal)


def test_an_amount_that_a_float_would_mangle_is_exact():
    """0.1 + 0.2 problems have no place in an invoice total."""
    assert to_decimal(1010, "usd") == Decimal("10.10")
    assert str(to_decimal(1010, "usd")) == "10.10"
    assert to_decimal(999999999999, "usd") == Decimal("9999999999.99")


def test_a_float_amount_is_refused_rather_than_silently_rounded():
    """A float reaching this function means minor units were already lost upstream."""
    with pytest.raises(TypeError, match="integer minor units"):
        to_decimal(25.0, "usd")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        format_amount(25.5, "usd")  # type: ignore[arg-type]


def test_a_bool_is_not_an_amount():
    with pytest.raises(TypeError):
        to_decimal(True, "usd")  # type: ignore[arg-type]


def test_zero_is_a_valid_amount():
    assert format_amount(0, "usd") == "$0.00"
    assert format_amount(0, "jpy") == "¥0"


def test_a_credit_is_rendered_with_the_sign_outside_the_symbol():
    assert format_amount(-2500, "usd") == "-$25.00"


# ----------------------------------------------------------------------------------
# Unknown currencies are labelled, not guessed at
# ----------------------------------------------------------------------------------


def test_a_currency_with_no_symbol_gets_its_iso_code():
    """An unambiguous code beats a wrong symbol in a letter demanding payment."""
    assert format_amount(250000, "czk") == "2,500.00 CZK"


def test_the_symbol_can_be_suppressed():
    assert format_amount(2500, "usd", with_symbol=False) == "25.00 USD"


@pytest.mark.parametrize("bad", ["", "us", "dollars", "US$", "u2d", None, 123])
def test_a_nonsense_currency_is_refused(bad):
    with pytest.raises((CurrencyError, TypeError)):
        format_amount(2500, bad)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------
# What a tool result carries
# ----------------------------------------------------------------------------------


def test_describe_carries_both_representations_so_no_caller_has_to_choose():
    assert describe(250000, "USD") == {
        "amount_minor": 250000,
        "currency": "usd",
        "amount_display": "$2,500.00",
    }


def test_the_display_string_never_equals_the_bare_minor_units():
    """The property Phase 4's letter guardrail relies on: the two are distinguishable."""
    for minor, currency in [(2500, "usd"), (250000, "usd"), (99, "eur"), (5, "gbp")]:
        assert format_amount(minor, currency) != str(minor)
