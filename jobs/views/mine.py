"""Both sides of one person's dealings, on one page."""

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from core.state_machine import Actor, JobState, assert_transition, claim
from ..waiting import waiting_for
from ..models import (
    Application,
    booking_of,
    collapse_groups,
    collapse_rows,
    ReviewDirection,
    ApplicationStatus,
    Counter,
    CounterStatus,
    Job,
    JobType,
    Offer,
    OfferStatus,
    Party,
)

from .common import _client, _worker




# ---------------------------------------------------------------------------
# "Mine"
# ---------------------------------------------------------------------------


@login_required
def mine(request):
    """Both sides on one page: what I posted, and what I applied to.

    One page rather than two, because someone who is both should not have to
    work out which of two menu items holds the thing they are looking for.
    """
    client = _client(request)
    worker = _worker(request)
    return render(
        request,
        "jobs/mine.html",
        {
            "waiting": waiting_for(request.user),
            # The jobs this person still owes a rating on, from either side.
            # The "waiting on you" panel counts these and sends the reader
            # here; without the list to land on, the count was a dead end and
            # the rating page may as well not have existed.
            # Collapsed like everything else: a week worked for one client is
            # one person to rate, not five identical prompts.
            "to_rate": collapse_groups(
                job
                for job in Job.objects.filter(
                    models.Q(client=client) | models.Q(assigned_worker=worker),
                    state__in=[JobState.PAID_OUT, JobState.CLOSED],
                )
                .select_related("trade", "client__user", "assigned_worker__user")
                .order_by("-gig_date")
                if job.can_be_reviewed_by(request.user)
            ),
            # Collapsed, so a four-day booking is one line here too. The
            # client posted one thing and should see one thing.
            "posted": (
                collapse_groups(
                    Job.objects.filter(client=client)
                    .select_related("trade", "assigned_worker__user")
                    .with_applicant_counts()
                    .order_by("-created_at", "gig_date")
                )
                if client
                else None
            ),
            # One line per booking. Applying once applies to every day of it,
            # so listing the days back is listing an action nobody took five
            # times. Earliest day first, so the row that survives is the one
            # the booking starts on.
            "applications": (
                collapse_rows(
                    Application.objects.filter(worker=worker)
                    .select_related("job__trade", "job__client__user")
                    .order_by("job__gig_date", "pk"),
                    lambda application: application.job,
                )
                if worker
                else None
            ),
            # Top of the page for the worker: somebody is waiting on an answer,
            # and a gig offered for Thursday is worth nothing if it is read on
            # Friday. Only live ones — an offer for a job that has since been
            # cancelled is not a decision anyone still has to make.
            "offers": (
                collapse_rows(
                    Offer.objects.filter(worker=worker, status=OfferStatus.PENDING)
                    .filter(job__state=JobState.POSTED)
                    .select_related("job__trade", "job__client__user")
                    .order_by("job__gig_date", "pk"),
                    lambda offer: offer.job,
                )
                if worker
                else None
            ),
            # And for the client: what is still out with somebody.
            "offers_sent": (
                collapse_rows(
                    Offer.objects.filter(job__client=client, status=OfferStatus.PENDING)
                    .select_related("job__trade", "worker__user")
                    .order_by("job__gig_date", "pk"),
                    lambda offer: offer.job,
                )
                if client
                else None
            ),
        },
    )
