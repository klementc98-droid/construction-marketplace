"""Conversation state, held by the server and never by the model.

The model is asked to follow a protocol — one field at a time, confirm each
one individually, only finish when everything is in. Prompts get you most of
the way there and then fail on the day someone types something unusual. So the
protocol is *also* a small state machine here, and this side is the authority:

* ``proposed`` is what the model says it heard. It is not data yet.
* ``confirmed`` is what the user has since agreed to, field by field.
* Only ``confirmed`` is ever handed to a form.
* :meth:`missing` is computed from the form's own required fields, so
  :func:`ready_for_review` cannot succeed early no matter what the model claims.

A model that confirms four fields off one "yes" is therefore wrong about the
conversation but harmless to the data — the user still sees every value on the
real form before anything is written.

Everything lives in the Django session. There is no model and no migration; see
``assistant/models.py`` for why that is deliberate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from . import registry
from .schemas import FormSpec, UnknownChoice, required_fields, to_form_data

#: One session key holds the whole thing, so ``reset`` is a single delete and a
#: stale conversation cannot leave fragments behind under other keys.
SESSION_KEY = "assistant"

#: Session key for the finished payload, read once by the real form view.
HANDOFF_KEY = "assistant_handoff"

BRANCH_FORM = "form"
BRANCH_QA = "qa"

#: How many user/assistant turns to keep. The form flow is meant to finish in
#: well under a dozen exchanges and the Q&A branch in two or three, so this is
#: generous — it exists to bound cost if someone settles in for a long chat,
#: not to shape normal use.
MAX_TURNS = 24


@dataclass
class Conversation:
    """A live conversation, loaded from and saved back to the session."""

    branch: str | None = None
    form_key: str | None = None
    proposed: dict[str, Any] | None = None
    confirmed: dict[str, Any] | None = None
    transcript: list[dict[str, str]] | None = None
    calls: list[float] | None = None

    def __post_init__(self) -> None:
        self.proposed = self.proposed or {}
        self.confirmed = self.confirmed or {}
        self.transcript = self.transcript or []
        self.calls = self.calls or []

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, request) -> "Conversation":
        return cls(**request.session.get(SESSION_KEY, {}))

    def save(self, request) -> None:
        request.session[SESSION_KEY] = {
            "branch": self.branch,
            "form_key": self.form_key,
            "proposed": self.proposed,
            "confirmed": self.confirmed,
            "transcript": self.transcript[-MAX_TURNS:],
            "calls": self.calls,
        }
        request.session.modified = True

    @staticmethod
    def clear(request) -> None:
        request.session.pop(SESSION_KEY, None)
        request.session.modified = True

    # -- branch ------------------------------------------------------------

    def start(self, branch: str, form_key: str | None = None) -> None:
        """Lock the conversation into one branch.

        Called only from the view, in response to the user pressing one of two
        buttons — never inferred from message text. That is what stops a Q&A
        conversation from talking its way into the branch that has tools.
        """
        self.branch = branch
        self.form_key = form_key
        self.proposed = {}
        self.confirmed = {}
        self.transcript = []

    @property
    def spec(self) -> FormSpec | None:
        return registry.get(self.form_key) if self.form_key else None

    # -- rate limiting -----------------------------------------------------

    def rate_limited(self) -> bool:
        """A stuck client retrying in a loop is the realistic failure here."""
        cutoff = time.time() - 3600
        self.calls = [t for t in self.calls if t > cutoff]
        return len(self.calls) >= settings.ASSISTANT_RATE_LIMIT_PER_HOUR

    def note_call(self) -> None:
        self.calls.append(time.time())

    # -- transcript --------------------------------------------------------

    def add(self, role: str, content: str) -> None:
        if content:
            self.transcript.append({"role": role, "content": content})
            self.transcript = self.transcript[-MAX_TURNS:]

    def messages(self) -> list[dict[str, str]]:
        """The transcript, plus a server-written note on where the form stands.

        The note is appended as a system-role message each turn rather than
        being left to the model's memory of its own tool calls. It keeps the
        model honest about what is actually confirmed — if it believed it had
        collected something it had not, this is the correction.
        """
        messages = list(self.transcript)
        if self.branch == BRANCH_FORM and (spec := self.spec):
            messages.append({"role": "system", "content": self._status(spec)})
        return messages

    def _status(self, spec: FormSpec) -> str:
        confirmed = ", ".join(
            f"{k}={v!r}" for k, v in self.confirmed.items()
        ) or "nothing yet"
        awaiting = ", ".join(self.awaiting_confirmation()) or "nothing"
        missing = ", ".join(self.missing()) or "nothing"
        return (
            "FORM STATE (authoritative — this is the server's record, trust it "
            "over your own memory).\n"
            f"Confirmed: {confirmed}\n"
            f"Proposed but NOT yet confirmed by the user: {awaiting}\n"
            f"Required and still missing: {missing}\n"
            "Ask about the first still-missing field, unless something above is "
            "awaiting confirmation — in that case confirm that first, one field "
            "per question."
        )

    # -- field collection --------------------------------------------------

    def propose(self, values: dict[str, Any]) -> None:
        """Record what the model heard. Deliberately does not confirm it."""
        spec = self.spec
        if spec is None:
            return
        for name, value in values.items():
            if name in spec.chat_fields and value not in (None, ""):
                self.proposed[name] = value
                # A re-proposal is a correction: the user changed their mind,
                # so the old agreement no longer covers the new value.
                self.confirmed.pop(name, None)

    def confirm(self, names: list[str]) -> list[str]:
        """Promote proposed values the user has agreed to.

        Only fields that were actually proposed can be confirmed. A model that
        confirms something it never heard gets nothing — the value would be
        invented, and an invented rate on a profile is worse than a repeated
        question.
        """
        accepted = []
        for name in names:
            if name in self.proposed:
                self.confirmed[name] = self.proposed[name]
                accepted.append(name)
        return accepted

    def awaiting_confirmation(self) -> list[str]:
        return [n for n in self.proposed if n not in self.confirmed]

    def missing(self) -> list[str]:
        """Required chat fields with no confirmed value."""
        spec = self.spec
        if spec is None:
            return []
        return [n for n in required_fields(spec) if n not in self.confirmed]

    def can_review(self) -> bool:
        return bool(self.spec) and not self.missing() and not self.awaiting_confirmation()

    def blocking_reason(self) -> str:
        """Why ``ready_for_review`` was refused, phrased for the model."""
        if missing := self.missing():
            return (
                f"Not ready: these required fields have no confirmed value yet: "
                f"{', '.join(missing)}. Ask about the first one."
            )
        if awaiting := self.awaiting_confirmation():
            return (
                f"Not ready: these were proposed but the user has not confirmed "
                f"them individually yet: {', '.join(awaiting)}. Read the first one "
                f"back and ask if it is right."
            )
        return "Not ready."

    # -- handoff -----------------------------------------------------------

    def handoff(self, request) -> str | None:
        """Stash confirmed values for the real form view and return its URL.

        Returns ``None`` if a value cannot be mapped onto the form — a trade
        name the model altered, say. Better to keep asking than to hand the
        form a value it will silently drop.
        """
        from django.urls import reverse

        spec = self.spec
        if spec is None or not self.can_review():
            return None

        try:
            data = to_form_data(spec, self.confirmed)
        except UnknownChoice:
            return None

        request.session[HANDOFF_KEY] = {"form_key": spec.key, "data": data}
        request.session.modified = True
        return reverse(spec.review_url_name, kwargs=spec.review_url_kwargs or None)


def take_handoff(request, form_key: str) -> dict[str, Any] | None:
    """Pop chat-collected values for ``form_key``, if any are waiting.

    Popped rather than read, so a prefill happens exactly once. Landing on the
    posting form a second time an hour later should give a blank form, not a
    ghost of a conversation the user has forgotten having.
    """
    payload = request.session.get(HANDOFF_KEY)
    if not payload or payload.get("form_key") != form_key:
        return None
    del request.session[HANDOFF_KEY]
    request.session.modified = True
    return payload.get("data") or None
