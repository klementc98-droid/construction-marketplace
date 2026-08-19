"""Public explanatory pages.

These are the pages someone reads *before* they trust the platform with a day's
pay, so every number on them is read from :mod:`config.business_rules` rather
than typed into the copy. A marketing page that says 12% while the code charges
15% is not a stale page, it is a false statement about someone's money — and
the only way to guarantee that never happens is to make the page unable to
disagree with the code.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.utils.safestring import mark_safe
from django.utils.translation import get_language

from config import business_rules as rules

#: The whitepaper is served from the repository file, not from a copy pasted
#: into a template. One artefact: editing docs/whitepaper.md updates the page,
#: and there is no second version to forget about. It is also still a plain
#: Markdown file anyone can read on disk or hand to someone directly.
WHITEPAPER_PATH = Path(settings.BASE_DIR) / "docs" / "whitepaper.md"

#: Parsed HTML per file, keyed by the file's modification time. Rendering 12 KB
#: of Markdown per request would be wasteful for a page that changes about
#: never, and keying on mtime means an edit still shows up without a restart.
#:
#: Per file rather than one entry, because the document exists once per
#: language and a single slot would re-parse on every switch between them.
_rendered: dict[str, tuple[float, str]] = {}


def about(request):
    """How it works: the money, the two job types, and what we don't do."""
    return render(
        request,
        "core/about.html",
        {
            "fee_pct": rules.PLATFORM_FEE_PCT * 100,
            "approval_hours": int(
                rules.CLIENT_APPROVAL_WINDOW.total_seconds() // 3600
            ),
            "dispute_hours": int(
                rules.EARLY_END_DISPUTE_WINDOW.total_seconds() // 3600
            ),
            "minimum_hours": rules.MINIMUM_GUARANTEED_HOURS,
            "funding_window_days": rules.ESCROW_AUTHORIZATION_MAX_DAYS,
            "min_jobs_for_stats": rules.MIN_JOBS_FOR_PUBLIC_STATS,
            "minimum_age": rules.MINIMUM_WORKING_AGE,
        },
    )


def _whitepaper_path(language: str | None) -> Path:
    """The document in the reader's language, falling back to the original.

    A translated document, not a translated *string*. Two thousand words of
    prose in a gettext catalogue would be unreadable to whoever had to keep it
    up to date and unusable to whoever had to translate it — the unit a
    translator works in here is the document, so the unit the app stores is the
    document. ``docs/whitepaper.el.md`` beside ``docs/whitepaper.md``, and a
    language with no file of its own reads the English one rather than an
    empty page.
    """
    if language:
        # "el-gr" and "el" are the same document.
        localised = WHITEPAPER_PATH.with_suffix(f".{language.split('-')[0]}.md")
        if localised.exists():
            return localised
    return WHITEPAPER_PATH


def _whitepaper_html(language: str | None = None) -> str:
    """Render the whitepaper, reusing the last parse until the file moves."""
    path = _whitepaper_path(language)

    try:
        stamp = path.stat().st_mtime
    except OSError as exc:
        raise Http404("The whitepaper is not available.") from exc

    cached = _rendered.get(str(path))
    if cached is None or cached[0] != stamp:
        import markdown

        html = markdown.markdown(
            path.read_text(encoding="utf-8"),
            # fenced_code carries the lifecycle diagram in section 6; tables
            # carries the gig/standing comparison. Both are load-bearing —
            # without them those sections render as a wall of text.
            extensions=["extra", "sane_lists", "smarty"],
        )
        _rendered[str(path)] = (stamp, html)

    return _rendered[str(path)][1]


def whitepaper(request):
    """The long-form document, as a page.

    ``mark_safe`` is doing something real here, so it is worth saying why it is
    not a hole: the input is a file committed to this repository, written by
    whoever can already deploy the app. It is never user input and never
    reaches this function from a request. Anyone able to edit it could ship a
    template instead.
    """
    return render(
        request,
        "core/whitepaper.html",
        # In the reader's language, which is the same question the rest of the
        # page asks — a Greek frame around an English document is worse than
        # either on its own.
        {"body": mark_safe(_whitepaper_html(get_language()))},  # noqa: S308 - repo file

    )
