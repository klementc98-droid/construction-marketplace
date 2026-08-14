"""Parsing the comma-separated date lists the chip picker produces.

Two forms collect a set of days — a worker's availability, and the dates a
client is offering someone work on. Both are backed by a plain text input that
``crew.js`` turns into tappable chips, and both post the same shape: ISO dates
separated by commas.

Shared rather than written twice because the awkward part is not the parsing,
it is agreeing on what to do with a date in the past. Rejecting is the answer
in both places, and for the same reason: the picker's ``min`` already prevents
it, so anything that arrives stale was typed by hand or posted directly, and
somebody who meant next Tuesday and wrote last Tuesday needs telling rather
than silently having their date dropped.
"""

from __future__ import annotations

from datetime import date, datetime

from django import forms
from django.utils.translation import gettext as _


def parse_date_list(raw: str | None, *, today: date) -> list[date]:
    """``"2026-08-04, 2026-08-05"`` to a sorted, de-duplicated list of dates.

    Empty input gives an empty list — whether that is allowed is the caller's
    question, not this function's. Raises ``ValidationError`` on anything
    unparseable or in the past.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    parsed: list[date] = []
    stale: list[date] = []
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            day = datetime.strptime(chunk, "%Y-%m-%d").date()
        except ValueError:
            raise forms.ValidationError(
                _("%(value)r isn't a date in YYYY-MM-DD form.") % {"value": chunk}
            )
        (stale if day < today else parsed).append(day)

    if stale:
        listed = ", ".join(d.isoformat() for d in sorted(set(stale)))
        raise forms.ValidationError(
            _("%(dates)s — that's in the past. Pick days from today onwards.")
            % {"dates": listed}
        )
    return sorted(set(parsed))
