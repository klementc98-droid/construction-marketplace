"""Conversation state, held by the server and never by the model.

There is one thing to hold now: the transcript. The assistant used to have a
second branch that filled in forms by chat — it asked for a rate, a date, a
trade, one at a time, and handed the answers to the real form. That is gone.
Posting a job and writing a profile both ask one question per screen now, on
the form itself, where the answer is typed into the box it belongs to and is
visible beside the others. A chat that collects the same values is a slower
route to a screen the user is going to have to look at anyway.

What is left is the part a form cannot do: answering "how does this work". So
there is no branch to choose, no collected values, and no handoff — and with
them go the tool definitions, which means this endpoint hands the model no
tools at all. "Takes no action on the user's behalf" stops being a rule the
model is asked to remember and becomes a fact about what it was given.

Everything lives in the Django session. There is no model and no migration; see
``assistant/models.py`` for why that is deliberate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, fields

from django.conf import settings
from django.core.cache import cache

#: One session key holds the whole thing, so ``clear`` is a single delete and a
#: stale conversation cannot leave fragments behind under other keys.
SESSION_KEY = "assistant"

#: How many user/assistant turns to keep. Generous: it exists to bound cost if
#: somebody settles in for a long chat, not to shape normal use.
MAX_TURNS = 24


@dataclass
class Conversation:
    """A live conversation, loaded from and saved back to the session."""

    #: Whether the user has opened one. Not a branch — there is only one thing
    #: this does — but the view still has to tell "say" apart from "say before
    #: anything was started", which is a bad request rather than a turn.
    started: bool = False
    transcript: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        self.transcript = self.transcript or []

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, request) -> "Conversation":
        """Rebuild from the session, ignoring keys this class no longer has.

        Sessions outlive deploys. A conversation stored while the form branch
        existed carries ``branch``, ``form_key`` and ``collected``, and would
        otherwise raise TypeError on the next message — a crash for anyone
        mid-chat when this ships, in the code path least able to afford one.
        """
        stored = request.session.get(SESSION_KEY, {})
        known = {f.name for f in fields(cls)}
        kept = {k: v for k, v in stored.items() if k in known}
        # A conversation from before this change has no ``started`` key and a
        # transcript that is plainly running. Treat it as started rather than
        # rejecting the user's next message as a bad request.
        if "started" not in kept and kept.get("transcript"):
            kept["started"] = True
        return cls(**kept)

    def save(self, request) -> None:
        request.session[SESSION_KEY] = {
            "started": self.started,
            "transcript": self.transcript[-MAX_TURNS:],
        }
        request.session.modified = True

    @staticmethod
    def clear(request) -> None:
        request.session.pop(SESSION_KEY, None)
        request.session.modified = True

    def start(self) -> None:
        self.started = True
        self.transcript = []

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
        deliberate trade. A sliding window needs a list of timestamps, and a
        list cannot be incremented atomically. The cost is that somebody
        spanning a boundary can send up to twice the limit across two hours;
        the benefit is that the check cannot be raced at all, which is the
        failure that was actually happening.

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
        return list(self.transcript)
