"""The only module that talks to OpenAI over the network.

Same boundary, and the same reason, as :mod:`payments.gateway`: everything
else in :mod:`assistant` calls :func:`complete`, so the branch logic, the
confirmation gating and the views are all testable without a key, a network,
or a recorded transcript. The tests replace this one function.

An absent key is not an error at import time. The widget asks
:func:`configured` and hides itself, so the rest of the app stays usable for
anyone working without an OpenAI account — the same posture Stripe has here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class AssistantUnavailable(RuntimeError):
    """The assistant cannot answer right now.

    One exception type for "no key", "API refused" and "API timed out",
    because the caller does the same thing with all three: apologise in the
    widget and leave the rest of the page working. The distinction matters in
    the log, not in the UI.
    """


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    """One turn from the model."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)

    def calls_named(self, name: str) -> tuple[ToolCall, ...]:
        return tuple(c for c in self.tool_calls if c.name == name)


def configured() -> bool:
    return bool(settings.OPENAI_API_KEY)


def _client():
    if not configured():
        raise AssistantUnavailable(
            "OPENAI_API_KEY is not set — add it to .env to enable the assistant."
        )
    from openai import OpenAI

    return OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.OPENAI_TIMEOUT_SECONDS,
        # One retry, not the SDK's default of two. A user is watching a typing
        # indicator; three sequential timeouts is a minute of silence, which
        # reads as broken rather than slow.
        max_retries=1,
    )


def complete(
    *,
    system: str,
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]] | None = None,
) -> Reply:
    """One round trip.

    ``tools`` being ``None`` is load-bearing, not a convenience: the Q&A branch
    passes nothing, so the model has no callable surface at all. That is what
    makes "the assistant cannot act on your behalf" a property of the request
    rather than a promise in a prompt.
    """
    client = _client()

    payload: dict[str, Any] = {
        "model": settings.OPENAI_MODEL,
        "max_completion_tokens": settings.OPENAI_MAX_OUTPUT_TOKENS,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        response = client.chat.completions.create(**payload)
    except Exception as exc:  # noqa: BLE001 - every SDK failure lands the same way
        logger.warning("Assistant call failed: %s", exc)
        raise AssistantUnavailable("The assistant is having trouble right now.") from exc

    choice = response.choices[0].message
    return Reply(
        text=(choice.content or "").strip(),
        tool_calls=tuple(_parse_calls(choice)),
    )


def _parse_calls(message) -> list[ToolCall]:
    """Read tool calls off a completion, discarding anything malformed.

    A model that emits unparseable JSON has produced no instruction, and the
    right response is to drop it and let the conversation continue — the
    server's own state is the authority on what has been collected, so a lost
    call costs one repeated question rather than a wrong value.
    """
    calls: list[ToolCall] = []
    for raw in getattr(message, "tool_calls", None) or []:
        try:
            arguments = json.loads(raw.function.arguments or "{}")
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Discarding unparseable tool call: %r", raw)
            continue
        if isinstance(arguments, dict):
            calls.append(ToolCall(name=raw.function.name, arguments=arguments))
    return calls
