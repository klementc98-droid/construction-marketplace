"""The chat endpoint.

One thing: answering questions about how the app works. The model is handed no
tools, so "takes no action on the user's behalf" is not a rule it is trusted to
keep — there is nothing for it to call. Nothing here writes to the database.

Signed-in only. The answers would be useful to a visitor too, but an
unauthenticated model endpoint is an open invitation to spend someone else's
API budget, and signed-out visitors already have /about/ and the whitepaper for
the same ground.
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from . import llm, options, prompts
from .conversation import Conversation

#: Cap on one user message. This answers questions, it does not read essays,
#: and an unbounded field is a way to make our token bill someone else's
#: decision.
MAX_MESSAGE_CHARS = 1500

_FALLBACK = _("Sorry, I didn't catch that — could you say it another way?")


def _payload(request) -> dict:
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _openers(conversation: Conversation) -> list[dict[str, str]]:
    """Starter questions, while the transcript is short.

    A starter list still sitting there on the fifth exchange is clutter, and by
    then the user plainly knows what to ask.
    """
    return options.qa_options() if len(conversation.transcript) <= 1 else []


@login_required
@require_GET
def config(request):
    """Whether there is anything behind the button."""
    return JsonResponse({"configured": llm.configured()})


@login_required
@require_POST
def start(request):
    """Begin a conversation. Resets anything already running."""
    conversation = Conversation.load(request)
    conversation.start()

    opening = _(
        "Ask me anything about how this app works — how you get paid, when "
        "money is released, check-ins, ratings, who can take a job with no "
        "experience, or where to find something."
    )
    conversation.add("assistant", opening)
    conversation.save(request)
    return JsonResponse({"reply": opening, "options": _openers(conversation)})


@login_required
@require_POST
def say(request):
    """One user turn."""
    conversation = Conversation.load(request)
    if not conversation.started:
        return HttpResponseBadRequest("Open the assistant first.")

    text = (_payload(request).get("text") or "").strip()[:MAX_MESSAGE_CHARS]
    if not text:
        return HttpResponseBadRequest("Empty message.")

    # Claimed rather than checked: the call is counted by the same operation
    # that decides whether it is allowed, so concurrent requests cannot all
    # read the same remaining allowance.
    if not conversation.claim_call(request.user.pk):
        return JsonResponse(
            {
                "reply": _(
                    "That's a lot of messages in one go — give it a few minutes "
                    "and try again."
                )
            },
            status=429,
        )

    conversation.add("user", text)

    try:
        reply = llm.complete(
            system=prompts.question_answering(),
            messages=conversation.messages(),
        )
    except llm.AssistantUnavailable:
        conversation.save(request)
        return JsonResponse(
            {
                "reply": _(
                    "Sorry — I can't reach the assistant right now. Everything "
                    "else in the app works as normal, and How it works has the "
                    "same answers."
                )
            },
            status=503,
        )

    answer = reply.text or _FALLBACK
    conversation.add("assistant", answer)
    conversation.save(request)
    return JsonResponse({"reply": answer, "options": _openers(conversation)})


@login_required
@require_POST
def close(request):
    Conversation.clear(request)
    return JsonResponse({"ok": True})
