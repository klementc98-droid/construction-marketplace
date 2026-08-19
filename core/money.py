"""Formatting an amount of money for a reader.

One function, because the symbol belongs in one place. It was written out as a
literal "$" in a dozen templates and four model methods, which meant changing
currency was a search-and-replace across the app — and the kind of
search-and-replace that leaves two of them behind.

Deliberately not localised beyond the symbol. Thousands separators and decimal
commas differ by locale, and Django's own ``floatformat``/``L10N`` machinery
already handles that where it is wanted; this is about which sign goes in front
of the number, not how the number is punctuated.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from config import business_rules as rules


def money(value, *, decimals: int = 0) -> str:
    """``90`` to ``"€90"``. Blank for ``None``, so callers need no guard.

    Whole units by default: a day rate is quoted as €240, not €240.00, and the
    extra zeros are noise on a card being scanned. Pass ``decimals=2`` where
    the cents are the point — a payout breakdown, or a fee.
    """
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return str(value)
    return f"{rules.CURRENCY_SYMBOL}{amount:,.{decimals}f}"
