"""Money (brief section 7).

    "Stripe returns minor units -- 2500 is $25.00. Format once, in Python, using the
    currency's exponent, and pass the agent a preformatted amount_display string alongside
    the integer. Never ask the model to divide by 100. This is the single most likely
    source of an embarrassing error in a demo."

So the model never sees an amount it is expected to do arithmetic on. Every figure that
reaches a letter comes from ``format_amount`` here, and Phase 4's guardrail suite asserts
that a letter body never contains the raw minor-unit integer.

Two details that a naive divide-by-100 gets wrong:

* **Not every currency has two decimal places.** JPY has none, so 2500 JPY is Y2,500 and
  not Y25.00 -- a hundredfold error in the direction of a larger demand. KWD has three, so
  2500 KWD is 2.500 and not 25.00.
* **Floats cannot represent money.** ``Decimal`` throughout, with the scaling done by
  exponent shifting rather than division, so nothing is ever rounded.
"""

from __future__ import annotations

from decimal import Decimal

#: Currencies Stripe treats as having no minor unit: the amount IS the whole amount.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
)

#: Currencies with three minor digits.
THREE_DECIMAL_CURRENCIES = frozenset({"bhd", "jod", "kwd", "omr", "tnd"})

#: Symbols worth rendering. Anything absent is shown as a trailing ISO code instead, which
#: is unambiguous -- a guess at the right symbol is worse than no symbol.
SYMBOLS = {
    "usd": "$",
    "eur": "€",
    "gbp": "£",
    "jpy": "¥",
    "cny": "¥",
    "inr": "₹",
    "krw": "₩",
    "cad": "CA$",
    "aud": "A$",
    "nzd": "NZ$",
    "chf": "CHF ",
    "sek": "SEK ",
    "nok": "NOK ",
    "dkk": "DKK ",
    "pln": "PLN ",
    "brl": "R$",
    "mxn": "MX$",
    "zar": "R",
    "sgd": "S$",
    "hkd": "HK$",
    "aed": "AED ",
}


class CurrencyError(ValueError):
    """The currency code is not a currency code. Better to fail than to guess."""


def normalise(currency: str) -> str:
    if not currency or not isinstance(currency, str):
        raise CurrencyError(f"expected an ISO currency code, got {currency!r}")
    code = currency.strip().lower()
    if len(code) != 3 or not code.isalpha():
        raise CurrencyError(f"expected a three-letter ISO currency code, got {currency!r}")
    return code


def exponent(currency: str) -> int:
    """How many minor digits this currency has. Two, unless it is one of the exceptions."""
    code = normalise(currency)
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def to_decimal(minor_units: int, currency: str) -> Decimal:
    """Exact major-unit value. Scaled by exponent shift, never divided."""
    if isinstance(minor_units, bool) or not isinstance(minor_units, int):
        raise TypeError(
            f"amounts must be integer minor units, got {type(minor_units).__name__}. "
            "A float here is the bug this module exists to prevent."
        )
    return Decimal(minor_units).scaleb(-exponent(currency))


def format_amount(minor_units: int, currency: str, *, with_symbol: bool = True) -> str:
    """The one string that may appear in a letter. e.g. 25000 usd -> '$250.00'."""
    code = normalise(currency)
    places = exponent(code)
    value = to_decimal(minor_units, code)

    negative = value < 0
    digits = f"{abs(value):,.{places}f}"

    if not with_symbol:
        return ("-" if negative else "") + digits + " " + code.upper()

    symbol = SYMBOLS.get(code)
    if symbol is None:
        return ("-" if negative else "") + digits + " " + code.upper()
    return ("-" if negative else "") + symbol + digits


def describe(minor_units: int, currency: str) -> dict[str, object]:
    """Both representations together, which is what a tool result carries.

    The integer is there so downstream code can compute; the string is there so the model
    never has to. Both are always present, so no caller has to choose.
    """
    return {
        "amount_minor": minor_units,
        "currency": normalise(currency),
        "amount_display": format_amount(minor_units, currency),
    }
