"""One boolean, so the launcher never renders when there is nothing behind it.

Cheaper than it looks: :func:`llm.configured` reads a setting. Doing it here
rather than in the widget's own JavaScript means a deployment without a key
shows no button at all, instead of a button that apologises when pressed.
"""

from __future__ import annotations

from . import llm


def assistant(request):
    user = getattr(request, "user", None)
    return {
        "assistant_available": bool(
            llm.configured() and user is not None and user.is_authenticated
        )
    }
