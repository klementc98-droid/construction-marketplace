"""Function-calling schemas, derived from the real Django forms.

Nothing here restates what a field is. The label, the help text, the choices
and the required flag are read off the form class the ordinary UI already
uses, so a field added to :class:`WorkerProfileForm` is a field the assistant
can collect, and a label reworded in one place is reworded in both.

The alternative — a hand-written JSON schema mirroring the form — is a second
source of truth that starts correct and silently rots. The first symptom would
be the assistant confidently collecting a field the form no longer has.

Three kinds of field never reach the chat:

* **Files** (``cv``, avatars, portfolio photos). A chat box cannot take an
  upload, and pretending otherwise wastes the user's time. Offered on the form
  at the end instead.
* **Hidden fields** (``region``). The form's own mixin pre-fills it while
  there is one launch market; asking a user to choose their city from a list of
  one is a question with no information in it.
* **Coordinates** (``site_latitude``/``site_longitude``). Nobody looks up a
  latitude mid-conversation. The same judgement ``OfferForm`` already makes.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Any

from django import forms


class UnknownChoice(ValueError):
    """The model answered with a choice that is not on the field."""


@dataclass(frozen=True)
class FormSpec:
    """One form the assistant knows how to fill.

    ``chat_fields`` is an explicit ordered list rather than "every field minus
    exclusions". Order is conversational, not the order the form renders in:
    the first question should be the one a person can answer without thinking,
    and money questions land better once they have described the work.
    """

    key: str
    form_class: type[forms.Form]
    #: What the assistant says it is helping with, in the opening message.
    noun: str
    #: Fields to collect, in the order to ask them.
    chat_fields: tuple[str, ...]
    #: Where the filled form is reviewed and saved.
    review_url_name: str
    review_url_kwargs: dict[str, str] = dc_field(default_factory=dict)
    #: Named on the form but never in the chat — mentioned once at the end so
    #: the user knows they exist rather than discovering them on the form.
    deferred_note: str = ""

    def form(self) -> forms.Form:
        return self.form_class()


def _choice_pairs(bound: forms.Field) -> list[tuple[str, str]]:
    """``(value, label)`` for a choice field, minus the empty placeholder."""
    return [
        (str(value), str(label))
        for value, label in list(getattr(bound, "choices", []))
        if str(value) != ""
    ]


def _is_bool_choice(bound: forms.Field) -> bool:
    """A Yes/No rendered as a two-value choice field.

    ``open_to_full_time`` is a ``TypedChoiceField`` over ``"True"``/``"False"``
    because an unticked box cannot tell "no" apart from "didn't read it". That
    reasoning is about the HTML widget; to the model it is a boolean, and
    saying so gets a cleaner answer than offering it two strings.
    """
    return {v for v, _ in _choice_pairs(bound)} == {"True", "False"}


def _describe(name: str, bound: forms.Field) -> str:
    """The field's own words, joined into one line for the model."""
    parts = [str(bound.label or name.replace("_", " ")).strip()]
    if bound.help_text:
        parts.append(str(bound.help_text).strip())

    if isinstance(bound, (forms.ModelChoiceField, forms.ModelMultipleChoiceField)):
        options = ", ".join(str(obj) for obj in bound.queryset)
        parts.append(f"Must be one of: {options}.")
    elif not _is_bool_choice(bound) and _choice_pairs(bound):
        readable = ", ".join(f"{v} ({label})" for v, label in _choice_pairs(bound))
        parts.append(f"Allowed values: {readable}.")

    return " ".join(_sentence(p) for p in parts if p)


def _sentence(text: str) -> str:
    """End on exactly one mark. Labels are often questions already."""
    text = text.strip()
    return text if text.endswith((".", "?", "!", ":")) else text + "."


def _json_type(name: str, bound: forms.Field) -> dict[str, Any]:
    """One field's JSON Schema fragment.

    Model-backed choices are exposed as their **labels**, not their primary
    keys. "Carpenter" is a thing the model can hear in a sentence and hand
    back; ``7`` is not, and asking it to hold a pk mapping in its head is a
    reliable way to get the wrong trade on someone's profile.
    """
    if isinstance(bound, forms.ModelMultipleChoiceField):
        return {
            "type": "array",
            "items": {"type": "string", "enum": [str(o) for o in bound.queryset]},
        }
    if isinstance(bound, forms.ModelChoiceField):
        return {"type": "string", "enum": [str(o) for o in bound.queryset]}
    if _is_bool_choice(bound):
        return {"type": "boolean"}
    if pairs := _choice_pairs(bound):
        return {"type": "string", "enum": [v for v, _ in pairs]}
    if isinstance(bound, forms.DateField):
        return {"type": "string", "description": "Date as YYYY-MM-DD."}
    if isinstance(bound, forms.IntegerField) and not isinstance(bound, forms.DecimalField):
        return {"type": "integer"}
    if isinstance(bound, (forms.DecimalField, forms.FloatField)):
        return {"type": "number"}
    return {"type": "string"}


def field_schema(spec: FormSpec) -> dict[str, dict[str, Any]]:
    """JSON Schema properties for everything the assistant may collect."""
    form = spec.form()
    properties: dict[str, dict[str, Any]] = {}
    for name in spec.chat_fields:
        bound = form.fields[name]
        fragment = _json_type(name, bound)
        described = _describe(name, bound)
        fragment["description"] = (
            f"{described} {fragment.pop('description', '')}".strip()
        )
        properties[name] = fragment
    return properties


def required_fields(spec: FormSpec) -> tuple[str, ...]:
    """Which of the chat fields the form will refuse to save without.

    Read from the form, so a field that becomes optional stops being something
    the assistant nags about, without anyone remembering to update a list here.
    """
    form = spec.form()
    return tuple(n for n in spec.chat_fields if form.fields[n].required)


def tool_definitions(spec: FormSpec) -> list[dict[str, Any]]:
    """The tools offered to the model while filling ``spec``.

    Three verbs, and the split is the point. ``propose_fields`` is what the
    model heard; ``confirm_fields`` is what the user has since agreed to. The
    server keeps them apart, so "confirm every field individually" is a rule
    the code enforces rather than a sentence in a prompt that a determined
    conversation can talk its way around.
    """
    properties = field_schema(spec)
    return [
        {
            "type": "function",
            "function": {
                "name": "propose_fields",
                "description": (
                    "Record field values you have just heard from the user. Call "
                    "this the moment you hear something, even mid-sentence, and "
                    "even if the user answered several fields at once. Proposing "
                    "a field does NOT fill it in — you must still read each one "
                    "back and get a yes before it counts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "confirm_fields",
                "description": (
                    "Mark fields the user has explicitly agreed are correct, after "
                    "you read the value back to them. Only list a field here once "
                    "the user has confirmed that specific field."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": list(properties),
                            },
                        }
                    },
                    "required": ["names"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ready_for_review",
                "description": (
                    "Call when every required field is confirmed and the user is "
                    "ready to see the finished form. The server checks this and "
                    "will refuse if anything is still missing or unconfirmed."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def to_form_data(spec: FormSpec, values: dict[str, Any]) -> dict[str, Any]:
    """Turn confirmed chat values into something the real form can bind.

    Labels go back to primary keys, booleans back to the ``"True"``/``"False"``
    the widget expects, decimals to strings. Anything the model invented that
    is not a field of this form is dropped rather than passed along — the form
    would ignore it, but silently carrying it means a typo in a tool call looks
    like it worked.
    """
    form = spec.form()
    data: dict[str, Any] = {}

    for name, value in values.items():
        bound = form.fields.get(name)
        if bound is None or name not in spec.chat_fields or value in (None, "", []):
            continue

        if isinstance(bound, forms.ModelMultipleChoiceField):
            data[name] = [_pk_for_label(bound, str(v)) for v in value]
        elif isinstance(bound, forms.ModelChoiceField):
            data[name] = _pk_for_label(bound, str(value))
        elif _is_bool_choice(bound):
            data[name] = "True" if value in (True, "True", "true", 1) else "False"
        elif isinstance(value, Decimal):
            data[name] = str(value)
        else:
            data[name] = value

    return data


def _pk_for_label(bound: forms.ModelChoiceField, label: str) -> Any:
    """Match a model choice by its displayed name, case-insensitively.

    Raises rather than guessing. A near-miss here would put the wrong trade on
    a profile — visible to every client who searches by trade, and not the kind
    of thing the worker would think to check.
    """
    wanted = label.strip().casefold()
    for obj in bound.queryset:
        if str(obj).casefold() == wanted:
            return obj.pk
    raise UnknownChoice(label)
