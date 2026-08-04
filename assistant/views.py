"""The chat endpoint.

One widget, one branch point. Which branch a conversation is in is decided
here — from which button the user pressed — and stored server-side. It is
never inferred from what someone types, so there is no sentence that moves a
Q&A conversation into the branch that holds tools.

Signed-in only. The Q&A branch would be useful to a visitor too, but an
unauthenticated model endpoint is an open invitation to spend someone else's
API budget, and signed-out visitors already have /about/ for the same answers.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import llm, prompts, registry
from .conversation import BRANCH_FORM, BRANCH_QA, Conversation

#: Cap on one user message. The assistant asks for a rate, not an essay, and an
#: unbounded field is a way to make our token bill someone else's decision.
MAX_MESSAGE_CHARS = 1500


def _payload(request) -> dict:
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _state(conversation: Conversation) -> dict:
    """What the widget needs to render, without leaking the transcript back."""
    spec = conversation.spec
    return {
        "branch": conversation.branch,
        "form_key": conversation.form_key,
        "confirmed": conversation.confirmed,
        "awaiting": conversation.awaiting_confirmation(),
        "missing": conversation.missing(),
        "can_review": conversation.can_review(),
        "total_fields": len(spec.chat_fields) if spec else 0,
    }


@login_required
@require_GET
def config(request):
    """What this user can ask the assistant for.

    The offered forms follow the profiles they actually hold. Offering to help
    a client fill in a worker profile would produce a conversation that ends at
    a form they cannot save.
    """
    available = []
    if getattr(request.user, "worker_profile", None):
        available.extend(registry.WORKER_FORMS)
    if getattr(request.user, "client_profile", None):
        available.extend(registry.CLIENT_FORMS)

    return JsonResponse(
        {
            "configured": llm.configured(),
            "forms": [
                {"key": key, "noun": registry.get(key).noun}
                for key in available
                if registry.get(key)
            ],
        }
    )


@login_required
@require_POST
def start(request):
    """Begin a conversation in one branch. Resets anything already running."""
    data = _payload(request)
    branch = data.get("branch")
    conversation = Conversation.load(request)

    if branch == BRANCH_QA:
        conversation.start(BRANCH_QA)
        opening = (
            "Ask me anything about how this app works — how you get paid, when "
            "money is released, check-ins, ratings, or where to find something."
        )
    elif branch == BRANCH_FORM:
        spec = registry.get(data.get("form_key") or "")
        if spec is None:
            return HttpResponseBadRequest("Unknown form.")
        conversation.start(BRANCH_FORM, spec.key)
        opening = (
            f"Right — let's do {spec.noun} together. I'll ask one thing at a "
            "time, and you can answer however you like. Nothing gets saved "
            "until you've seen the finished form and pressed save."
        )
    else:
        return HttpResponseBadRequest("Unknown branch.")

    conversation.add("assistant", opening)
    conversation.save(request)
    return JsonResponse({"reply": opening, "state": _state(conversation)})


@login_required
@require_POST
def say(request):
    """One user turn."""
    conversation = Conversation.load(request)
    if conversation.branch is None:
        return HttpResponseBadRequest("Pick what you need help with first.")

    text = (_payload(request).get("text") or "").strip()[:MAX_MESSAGE_CHARS]
    if not text:
        return HttpResponseBadRequest("Empty message.")

    if conversation.rate_limited():
        return JsonResponse(
            {
                "reply": (
                    "That's a lot of messages in one go — give it a few minutes "
                    "and try again."
                ),
                "state": _state(conversation),
            },
            status=429,
        )

    conversation.add("user", text)
    conversation.note_call()

    try:
        if conversation.branch == BRANCH_QA:
            result = _answer_question(conversation)
        else:
            result = _fill_form(request, conversation)
    except llm.AssistantUnavailable:
        conversation.save(request)
        return JsonResponse(
            {
                "reply": (
                    "Sorry — I can't reach the assistant right now. You can "
                    "still fill the form in yourself, everything works as normal."
                ),
                "state": _state(conversation),
            },
            status=503,
        )

    conversation.add("assistant", result["reply"])
    conversation.save(request)
    return JsonResponse({**result, "state": _state(conversation)})


def _answer_question(conversation: Conversation) -> dict:
    """Branch 2. No tools are passed, so the model has nothing it can call."""
    reply = llm.complete(
        system=prompts.question_answering(),
        messages=conversation.messages(),
    )
    return {"reply": reply.text or _FALLBACK}


def _fill_form(request, conversation: Conversation) -> dict:
    """Branch 1: extract, gate, and decide whether the form is ready.

    Tool calls are applied to the server's own record first, and only then is
    the model asked what to say. It never gets to both decide the state and
    narrate it in the same breath.
    """
    from .schemas import tool_definitions

    spec = conversation.spec
    if spec is None:
        return {"reply": _FALLBACK}

    system = prompts.form_filling(spec)
    reply = llm.complete(
        system=system,
        messages=conversation.messages(),
        tools=tool_definitions(spec),
    )

    for call in reply.calls_named("propose_fields"):
        conversation.propose(call.arguments)
    for call in reply.calls_named("confirm_fields"):
        conversation.confirm(call.arguments.get("names") or [])

    if reply.calls_named("ready_for_review"):
        if url := conversation.handoff(request):
            return {
                "reply": (
                    "That's everything. Here's the finished form — have a read "
                    "through, change anything that isn't right by clicking "
                    "straight into it, then press save."
                ),
                "redirect": url,
            }
        # The model called it early, or a value would not map onto the form.
        # Tell it why and let it ask the next question rather than dead-ending.
        return {"reply": _speak(conversation, system, conversation.blocking_reason())}

    if reply.text:
        return {"reply": reply.text}

    # Tool calls with no prose. Common and harmless — ask again for words only,
    # with the freshly updated state in front of it.
    return {"reply": _speak(conversation, system, "Now say the next thing to the user.")}


def _speak(conversation: Conversation, system: str, instruction: str) -> str:
    """A second, tool-less turn purely to produce the sentence the user reads."""
    reply = llm.complete(
        system=system,
        messages=[*conversation.messages(), {"role": "system", "content": instruction}],
    )
    return reply.text or _FALLBACK


_FALLBACK = "Sorry, I didn't catch that — could you say it another way?"


@login_required
@require_POST
def close(request):
    Conversation.clear(request)
    return JsonResponse({"ok": True})
