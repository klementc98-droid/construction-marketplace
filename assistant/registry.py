"""Which forms the assistant can fill, and in what order it asks.

Imports are deferred into the builder rather than taken at module level: this
module is reached from ``assistant.views``, and pulling ``accounts.forms`` in
at import time would have the assistant app depending on two other apps' form
modules before the registry is ever used.

Adding a form here is the whole job of teaching the assistant a new one — the
schema, the questions and the validation all come from the form class itself.
"""

from __future__ import annotations

from functools import cache

from .schemas import FormSpec


@cache
def specs() -> dict[str, FormSpec]:
    from accounts.forms import WorkerProfileForm
    from jobs.forms import GigForm, StandingForm

    return {
        spec.key: spec
        for spec in (
            FormSpec(
                key="worker_profile",
                form_class=WorkerProfileForm,
                noun="your worker profile",
                # Trade first: it is the one question every tradesperson can
                # answer instantly and without thinking, which is the right way
                # to open. Money comes after the work is described, and the
                # free-text "about you" goes last, when they have warmed up.
                chat_fields=(
                    "trades",
                    "years_experience",
                    "rate_type",
                    "rate_min",
                    "rate_max",
                    "service_area",
                    "availability_status",
                    "available_dates",
                    "open_to_full_time",
                    "seeking",
                    "bio",
                ),
                review_url_name="accounts:worker_edit",
                deferred_note=(
                    "a résumé, photos of your work, and licence numbers if you "
                    "hold any"
                ),
            ),
            FormSpec(
                key="gig",
                form_class=GigForm,
                noun="a one-day gig",
                chat_fields=(
                    "title",
                    "trade",
                    "description",
                    "gig_dates",
                    "gig_hours",
                    "fixed_pay",
                    "location",
                ),
                review_url_name="jobs:post",
                review_url_kwargs={"job_type": "gig"},
                deferred_note="site coordinates, if you want check-ins sanity-checked",
            ),
            FormSpec(
                key="standing",
                form_class=StandingForm,
                noun="a standing position",
                chat_fields=(
                    "title",
                    "trade",
                    "description",
                    "position_type",
                    "rate_type",
                    "rate_min",
                    "rate_max",
                    "location",
                ),
                review_url_name="jobs:post",
                review_url_kwargs={"job_type": "standing"},
            ),
        )
    }


def get(key: str) -> FormSpec | None:
    return specs().get(key)


#: What the widget offers once someone picks "help me fill out a form". Keyed
#: by the profile the user actually holds — see ``assistant.views``.
WORKER_FORMS = ("worker_profile",)
CLIENT_FORMS = ("gig", "standing")
