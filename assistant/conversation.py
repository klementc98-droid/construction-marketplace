"""Conversation state, held by the server and never by the model.

There is one store, :attr:`Conversation.collected`, holding what the model has
heard. The server remains the authority on what is in it and on when the form
is finished — :meth:`missing` is computed from the form's own required fields,
so :func:`ready_for_review` cannot succeed early no matter what the model
claims.

This used to be two stores. Values landed in ``proposed``, and only moved to
``confirmed`` once the model had read each one back and got a "yes" — so every
field cost two exchanges. The review it was buying already exists, and in a
better place: the handoff opens the real form with the values filled in, where
they can be seen together, corrected by typing, and saved deliberately. Reading
a rate back in chat asked people to proofread by ear something they were about
to be shown on screen, and the round trips made a six-field form feel like an
interrogation.

Nothing about safety rests on the removed step. Only the user submitting the
real form writes anything, through ordinary Django validation — a wrong value
here costs one correction on a form they are already looking at.

Everything lives in the Django session. There is no model and no migration; see
``assistant/models.py`` for why that is deliberate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, fields
from typing import Any

from django.conf import settings
from django.core.cache import cache

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
    collected: dict[str, Any] | None = None
    transcript: list[dict[str, str]] | None = None
    calls: list[float] | None = None

    def __post_init__(self) -> None:
        self.collected = self.collected or {}
        self.transcript = self.transcript or []
        self.calls = self.calls or []

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, request) -> "Conversation":
        """Rebuild from the session, ignoring keys this class no longer has.

        Sessions outlive deploys. A conversation stored before ``proposed`` and
        ``confirmed`` became one ``collected`` would otherwise raise TypeError
        on the next message — a crash for anyone mid-form when the change ships,
        in the one code path least able to afford one.
        """
        stored = request.session.get(SESSION_KEY, {})
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in stored.items() if k in known})

    def save(self, request) -> None:
        request.session[SESSION_KEY] = {
            "branch": self.branch,
            "form_key": self.form_key,
            "collected": self.collected,
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
        self.collected = {}
        self.transcript = []

    @property
    def spec(self) -> FormSpec | None:
        return registry.get(self.form_key) if self.form_key else None

    # -- rate limiting -----------------------------------------------------

    def claim_call(self, user_id) -> bool:
        """Take one of this hour's calls, or say there are none left.

        Checking and counting are one operation, which they were not. The
        count lived in the session: every request read the same list, appended
        its own timestamp, and the last save won — so ten requests fired
        together counted as one, and the limit meant nothing to the only
        client it exists to stop, the stuck one retrying in a loop.

        It lives in the cache now, keyed by the person rather than by their
        session, because a session is something a caller can throw away and
        get a fresh allowance with. ``add`` then ``incr`` is the atomic pair:
        whoever creates the key gets 1, everybody else gets a number nobody
        else has.

        A fixed hourly bucket rather than a sliding window, and that is a
        deliberate trade. A sliding window needs the list of timestamps this
        was built on, and a list cannot be incremented atomically. The cost is
        that somebody spanning a boundary can send up to twice the limit across
        two hours; the benefit is that the check cannot be raced at all, which
        is the failure that was actually happening.

        One caveat worth writing down: with the in-memory cache this app runs
        by default the count is per process, so several workers each get their
        own allowance. Production wants a shared cache — the limit is only as
        atomic as the thing holding it.
        """
        window = int(time.time() // 3600)
        key = f"assistant-calls:{user_id}:{window}"
        if cache.add(key, 1, timeout=3600):
            used = 1
        else:
            try:
                used = cache.incr(key)
            except ValueError:
                # Expired between the add and the incr. Start again rather
                # than letting a race hand out a free pass.
                cache.set(key, 1, timeout=3600)
                used = 1
        return used <= settings.ASSISTANT_RATE_LIMIT_PER_HOUR

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
        collected = ", ".join(
            f"{k}={v!r}" for k, v in self.collected.items()
        ) or "nothing yet"
        missing = ", ".join(self.missing()) or "nothing"
        return (
            "FORM STATE (authoritative — this is the server's record, trust it "
            "over your own memory).\n"
            f"Collected so far: {collected}\n"
            f"Required and still missing: {missing}\n"
            "Do this in order. FIRST: if the user's last message contains any "
            "value for a field, call record_fields with it now — a value you do "
            "not record is lost, and this list is the proof of what got through. "
            "THEN: ask about the first still-missing field. Never re-ask or read "
            "back anything already collected; the user reviews all of it on the "
            "real form at the end."
        )

    # -- field collection --------------------------------------------------

    def collect(self, values: dict[str, Any]) -> None:
        """Record what the model heard.

        A field arriving twice is a correction — the user changed their mind, or
        the model misheard the first time — so the newer value simply replaces
        the older one. Empty values are ignored rather than stored: a model that
        emits ``{"rate_min": ""}`` while asking about the rate would otherwise
        overwrite a real answer with a blank.
        """
        spec = self.spec
        if spec is None:
            return
        for name, value in values.items():
            if name in spec.chat_fields and value not in (None, "", []):
                self.collected[name] = value

    def missing(self) -> list[str]:
        """Required chat fields with no value yet."""
        spec = self.spec
        if spec is None:
            return []
        return [n for n in required_fields(spec) if n not in self.collected]

    def can_review(self) -> bool:
        return bool(self.spec) and not self.missing()

    def blocking_reason(self) -> str:
        """Why ``ready_for_review`` was refused, phrased for the model."""
        if missing := self.missing():
            return (
                f"Not ready: these required fields have no value yet: "
                f"{', '.join(missing)}. Ask about the first one."
            )
        return "Not ready."

    # -- handoff -----------------------------------------------------------

    def handoff(self, request) -> str | None:
        """Stash collected values for the real form view and return its URL.

        Returns ``None`` if a value cannot be mapped onto the form — a trade
        name the model altered, say. Better to keep asking than to hand the
        form a value it will silently drop.
        """
        from django.urls import reverse

        spec = self.spec
        if spec is None or not self.can_review():
            return None

        try:
            data = to_form_data(spec, self.collected)
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
