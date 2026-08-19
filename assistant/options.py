"""The clickable answers offered under a question.

Typing is the thing this app's users are worst served by. Many are on a phone,
on a site, in gloves, and some are reading in a second language — so a question
whose answer is one of six known values should be six buttons, not a text box
and a spelling test. Free typing still works and always will; these are a
shortcut past it, not a replacement for it.

Every option here is derived from the same Django form field the ordinary UI
renders and :mod:`assistant.schemas` builds its tool schema from. That is the
whole design: a trade added to the database appears as a button without anyone
touching this file, and a button can never offer a value the form would then
reject. A hand-written list of choices would start correct and rot, and the
first symptom would be a user tapping "Roofer" and being told it is not valid.

Fields with no fixed answer — a rate, a job title, a description — get no
buttons. Inventing plausible ones ("$25/hr? $30/hr?") would anchor people to a
number we made up, which on a marketplace is a real harm rather than a
convenience.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django import forms
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from .schemas import FormSpec, _choice_pairs, _is_bool_choice, required_fields

#: How many days to offer on a date question. A week covers "tomorrow" and
#: "next Tuesday", which is nearly every gig anyone posts in a hurry; anything
#: further out is rare enough to be worth typing.
DATE_HORIZON_DAYS = 7


def _option(value: str, label: str | None = None) -> dict[str, str]:
    """One button.

    ``value`` is what gets sent to the model as the user's message; ``label``
    is what the button says. They differ only where a precise value reads
    badly — a date, where the model needs ``2026-08-18`` and the user should
    see "Tomorrow · 18 Aug".
    """
    return {"value": value, "label": label or value}


def _date_options() -> list[dict[str, str]]:
    """The next few days, named the way people say them.

    ISO in the value because the form parses ISO and the model is told to send
    ISO; the friendly name stays on the button. Today is included: same-day
    gigs are a real and urgent case on this board.
    """
    today = timezone.localdate()
    out = []
    for offset in range(DATE_HORIZON_DAYS):
        day = today + timedelta(days=offset)
        if offset == 0:
            name = _("Today")
        elif offset == 1:
            name = _("Tomorrow")
        else:
            name = date_format(day, "D")
        out.append(_option(day.isoformat(), f"{name} · {date_format(day, 'j M')}"))
    return out


def options_for_field(spec: FormSpec, name: str) -> list[dict[str, str]]:
    """The buttons for one field, or an empty list if it has no fixed answers."""
    bound = spec.form().fields.get(name)
    if bound is None:
        return []

    if isinstance(bound, (forms.ModelChoiceField, forms.ModelMultipleChoiceField)):
        return [_option(str(obj)) for obj in bound.queryset]

    # The label is what a person says out loud and what the model is told to
    # listen for; the stored value ("hourly", "day_rate", "True") is an
    # identifier that would look like a database leak on a button.
    #
    # A yes/no field is not special-cased into a bare "Yes"/"No": its labels
    # are usually where the actual question lives — "Yes, I'd take a permanent
    # position" says something "Yes" does not — and throwing them away to save
    # a few characters loses the meaning the form author wrote. Only a field
    # whose labels really are "True"/"False" gets the generic pair.
    if pairs := _choice_pairs(bound):
        if _is_bool_choice(bound) and {label for _v, label in pairs} == {
            "True",
            "False",
        }:
            return [_option(_("Yes")), _option(_("No"))]
        return [_option(label) for _value, label in pairs]

    if isinstance(bound, forms.DateField) or name.endswith("_dates"):
        return _date_options()

    return []


def next_field(spec: FormSpec, collected: dict[str, Any]) -> str | None:
    """The field the assistant is about to ask about.

    First unanswered field in the spec's own order — which is the order the
    system prompt tells the model to ask in, so the buttons match the question
    on screen. When they ever disagree the buttons are simply skipped rather
    than shown against the wrong question: see :func:`options_for`.
    """
    for name in spec.chat_fields:
        if name not in collected:
            return name
    return None


def options_for(conversation) -> list[dict[str, str]]:
    """Buttons for whatever the assistant is asking next, if anything.

    Optional fields gain a "Skip" button. The prompt already tells the model to
    accept "skip" and move on, and an optional question with no visible way past
    it is how a form conversation stalls.
    """
    spec = conversation.spec
    if spec is None:
        return []

    name = next_field(spec, conversation.collected)
    if name is None:
        return []

    options = options_for_field(spec, name)
    if options and name not in required_fields(spec):
        options.append(_option(_("Skip")))
    return options


#: Openers for the Q&A branch. Not derived from anything — these are the
#: questions people actually arrive with, and the point of showing them is that
#: someone who does not know what to ask can still get started with one tap.
#: Kept short enough to fit a phone button.
QA_STARTERS = (
    _("How do I get paid?"),
    _("What's the platform fee?"),
    _("What is escrow?"),
    _("How does check-in work?"),
    _("How do ratings work?"),
    _("How do I post a job?"),
)


def qa_options() -> list[dict[str, str]]:
    return [_option(str(question)) for question in QA_STARTERS]
