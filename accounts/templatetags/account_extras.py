"""Small template helpers."""

from __future__ import annotations

from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def dict_get(mapping, key):
    """``{{ mydict|dict_get:some_var }}`` — dict lookup by a variable key.

    Django's template language can only do ``dict.literal_key``, which is no
    use when the key is a trade id from a loop.
    """
    if not hasattr(mapping, "get"):
        return ""
    return mapping.get(key, "")


@register.filter
def as_percent(value: Decimal | None, digits: int = 0) -> str:
    """Render a 0-1 rate as a percentage, or "New" when it is unknown.

    ``None`` means "not enough history" — never "zero". Rendering that as 0%
    would brand a first-time user with a failure they have not had the chance
    to earn, which is the single rule the trust display exists to protect.
    """
    if value is None:
        return "New"
    return f"{Decimal(value) * 100:.{digits}f}%"


@register.filter
def stars(value: Decimal | None) -> str:
    if value is None:
        return "New"
    return f"{value} ★"
