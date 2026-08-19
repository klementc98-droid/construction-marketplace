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

import json
from datetime import date, datetime

from django import forms
from django.utils.translation import gettext as _


def date_picker_attrs(
    *, floor: date, single: bool = False, taken=()
) -> dict[str, str]:
    """Widget attrs for the calendar in ``crew.js``.

    The script draws its own calendar, so the words on it have to come from
    somewhere translatable. They are handed over as one JSON attribute rather
    than through a JavaScript catalogue: it is a handful of strings on three
    forms, and a catalogue would be a second request and a second place for
    the language to be decided.

    ``data-date-list`` carries the server's today, in the app's timezone. Using
    the browser's clock instead would let someone on a device set to yesterday
    offer a day the server will reject.

    ``single`` is for the fields that hold one day rather than a set — a
    counter-offer moves the date, it does not collect dates. The calendar is
    the same calendar; picking replaces instead of adding and the panel closes
    on the choice, because the reason it stays open on the multi-day fields is
    that there is a second day coming, and here there is not.

    Sharing it is the point. A counter is answered on the same screen the offer
    was read on, and a native ``dd/mm/yyyy`` box next to the picker the offer
    was written with looks like two different applications.

    ``taken`` are days the calendar must refuse — the ones a worker is already
    booked on. Sent to the picker rather than only checked on submit because
    the answer is knowable before anybody types: a day that cannot be offered
    should not be tickable, and finding out after filling in the hours and the
    price is the round trip this whole field exists to avoid. The form still
    validates them, since a disabled cell is a courtesy and not a rule.
    """
    words = {
        "open": _("Pick a day") if single else _("Pick days"),
        "more": _("Change the day") if single else _("Add or remove days"),
        "none": _("No day picked yet.") if single else _("No days picked yet."),
        "done": _("Done"),
        "remove": _("Remove"),
        "prev": _("Previous month"),
        "next": _("Next month"),
        "taken": _("Already booked"),
    }
    attrs = {
        "data-date-list": floor.isoformat(),
        "data-date-list-i18n": json.dumps(words, ensure_ascii=False),
    }
    if taken:
        attrs["data-date-taken"] = ",".join(day.isoformat() for day in taken)
    if single:
        attrs["data-date-one"] = ""
    return attrs


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


def month_grids(dates, *, months_ahead: int = 3) -> list:
    """The months these days fall in, drawn as calendar grids.

    A list of dates answers "which days?" only if the reader already knows what
    a Tuesday in three weeks looks like. A month laid out the way a calendar is
    laid out answers it at a glance, and — this being the point — it shows the
    gaps as plainly as the bookings: somebody booked on the 25th and the 30th
    is visibly free on the 26th, which a run of chips is not.

    Rendered on the server rather than by the picker in ``crew.js``. That one
    is an input; this is a statement, it must be there with the script off, and
    it must be readable to somebody using a screen reader as a table of days
    rather than as forty buttons.

    Returns ``[{"label": date, "weeks": [[cell, ...], ...]}]`` where a cell is
    ``None`` for the padding before and after a month, and otherwise a dict of
    the date, whether it is booked, and whether it is today.
    """
    import calendar as _calendar

    wanted = sorted({day for day in dates if day})
    if not wanted:
        return []

    today = date.today()
    out = []
    seen: set = set()
    for day in wanted:
        key = (day.year, day.month)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > months_ahead:
            break

        weeks = []
        # Monday first, as everywhere else in the app.
        for week in _calendar.Calendar(firstweekday=0).monthdatescalendar(*key):
            row = []
            for cell in week:
                if cell.month != day.month:
                    row.append(None)
                    continue
                row.append({
                    "date": cell,
                    "booked": cell in wanted,
                    "today": cell == today,
                })
            weeks.append(row)
        out.append({"label": date(day.year, day.month, 1), "weeks": weeks})
    return out
