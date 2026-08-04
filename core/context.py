"""Template context shared by every page."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

#: Which tab lights up, keyed by URL namespace. Derived from the resolved
#: route rather than set by hand in each view, so a new page in an existing
#: section highlights correctly without anyone remembering to pass a flag.
_SECTIONS = {
    "accounts:home": "home",
    "core:about": "about",
    "core:whitepaper": "whitepaper",
    "jobs:list": "jobs",
    "jobs:detail": "jobs",
    "jobs:apply": "jobs",
    "jobs:post_choose": "jobs",
    "jobs:post": "jobs",
    "jobs:worker_list": "workers",
    "jobs:mine": "mine",
    "messaging:inbox": "messages",
    "messaging:thread": "messages",
    "accounts:worker_detail": "profile",
    "accounts:client_detail": "profile",
    "accounts:details": "profile",
    "accounts:dashboard": "profile",
}


def nav(request):
    """Expose ``nav`` so the two navigation bars can mark the current tab."""
    match = getattr(request, "resolver_match", None)
    if match is None or not match.view_name:
        return {"nav": ""}
    return {"nav": _SECTIONS.get(match.view_name, "")}


#: The stylesheet and script that every page loads. Both are served from the
#: same URL for the life of the project, which is exactly the shape a browser
#: caches hardest — so an edit to either can sit on disk, be served correctly
#: by the server, and still not reach the screen.
_VERSIONED = ("css/crew.css", "js/crew.js")


def assets(request):
    """Expose ``asset_v``, a cache-busting stamp for the two static assets.

    The value is the newest modification time across the files above. Appended
    to the URL as a query string it makes any edit a different URL, so the
    browser fetches it instead of answering from its own cache — no hard
    refresh, no "it works on my machine, clear your cache".

    Recomputed per request on purpose. It is two ``stat`` calls against files
    the OS already has in its page cache, and in development the whole point is
    that it notices a change made a second ago. Under ``DEBUG = False`` this
    should give way to ``ManifestStaticFilesStorage``, which hashes contents at
    collectstatic time and does the same job without touching the disk on every
    request — but that machinery needs a collectstatic step, which is precisely
    what is not running here.

    Falls back to a constant if a file is missing rather than raising: a
    stylesheet that cannot be stamped should still load.
    """
    newest = 0.0
    for path in _VERSIONED:
        for root in settings.STATICFILES_DIRS or [Path(settings.BASE_DIR) / "static"]:
            candidate = Path(root) / path
            try:
                newest = max(newest, candidate.stat().st_mtime)
            except OSError:
                continue
    return {"asset_v": int(newest) or "1"}
