"""The job views, split by what they are about.

One file until it was 1,848 lines and held posting, browsing, applying,
offers, counters, acceptance and reviews at once. Nothing here changed in the
split: the functions were moved verbatim and this module re-exports them, so
``jobs.urls`` — and anything else that reached for ``views.job_detail`` —
sees exactly what it saw before.

The one thing that did change is where a test patches. ``get_object_or_404``
is looked up in the module that uses it, so patching it now means naming that
module rather than this one.
"""

from .browse import job_list, worker_list, job_detail
from .posting import job_post_choose, job_post, job_edit, job_cancel
from .applications import job_apply, application_withdraw, job_applicants, application_select
from .offers import offer_create, offer_respond, offer_withdraw, offer_publish
from .negotiation import counter_create, counter_respond
from .mine import mine
from .reviews import review_create

__all__ = [
    "job_list",
    "worker_list",
    "job_detail",
    "job_post_choose",
    "job_post",
    "job_edit",
    "job_cancel",
    "job_apply",
    "application_withdraw",
    "job_applicants",
    "application_select",
    "offer_create",
    "offer_respond",
    "offer_withdraw",
    "offer_publish",
    "counter_create",
    "counter_respond",
    "mine",
    "review_create",
]
