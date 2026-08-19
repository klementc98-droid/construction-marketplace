"""``{{ amount|money }}`` — the currency symbol, from one place.

Templates used to write the sign themselves as ``${{ job.fixed_pay }}``. That
put the currency in the markup, where changing it means finding every one of
them, and where a page can disagree with what the payment gateway was told.
"""

from __future__ import annotations

from django import template

from core.money import money as _money

register = template.Library()


@register.filter(name="money")
def money(value, decimals: int = 0) -> str:
    return _money(value, decimals=int(decimals or 0))
