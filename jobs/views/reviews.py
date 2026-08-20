"""Rating the other side, once the work is over and settled."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, models, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from notifications.models import Kind
from notifications.services import audience_for, booking_key, notify
from config import business_rules as rules
from ..forms import (
    JOB_FORMS,
    ReviewForm,
    ApplicationForm,
    CounterForm,
    JobFilterForm,
    OfferExistingForm,
    OfferForm,
    OfferResponseForm,
    WorkerFilterForm,
)
from ..services import (
    ReviewError,
    booked_days_among,
    clashing_dates,
    describe_dates,
    leave_review,
)
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




# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


@login_required
def review_create(request, pk: int):
    """Rate the other side of a finished job.

    Both directions use this one view. The direction is derived from who is
    asking rather than passed in the URL — a parameter saying which way the
    review points would be a parameter somebody could change.

    Written only once the job is over and its day has passed. Rating before the
    money has moved would put a thumb on the scale of the payment itself
    ("five stars and I'll approve"), and rating before the work has happened is
    a score about nothing.
    """
    job = get_object_or_404(
        Job.objects.select_related("client__user", "assigned_worker__user"), pk=pk
    )

    # One rating for the booking, written against its first day. A week worked
    # for one client is one opinion, not five — and five would count five times
    # towards an average that is meant to say how many jobs somebody has been
    # rated on. Whichever day they arrived from, they land on the same row, so
    # the "already rated" check below is what stops the second attempt.
    booking = booking_of(job)
    job = booking[0]

    direction = job.review_direction_for(request.user)
    if direction is None:
        raise Http404("No job matches the given query.")

    existing = job.review_from(request.user)
    if existing is not None:
        messages.info(request, _("You've already rated this one."))
        return redirect("jobs:detail", pk=job.pk)

    if not job.can_be_reviewed_by(request.user):
        messages.info(
            request,
            _("You can rate this once the job is finished and its day has passed."),
        )
        return redirect("jobs:detail", pk=job.pk)

    if request.method == "POST":
        form = ReviewForm(request.POST)
        if form.is_valid():
            try:
                # Through the service, not written here. The rule about who
                # may rate what, and when, lived twice — once in this view and
                # once in leave_review — and the two had already drifted: the
                # service refused anything but PAID_OUT while this view happily
                # rated a CLOSED job. One of them had to be the rule, and it is
                # not the one that also renders a page. It folds the score into
                # the subject's average in the same transaction.
                review = leave_review(
                    job,
                    request.user,
                    rating=form.cleaned_data["rating"],
                    comment=form.cleaned_data.get("comment", "") or "",
                )
            except ReviewError as refusal:
                # The checks above already cover the ordinary cases, so this is
                # the narrow one they cannot: two tabs, or a second submit that
                # arrives while the first is still in flight. The service and
                # the constraint underneath it decide, and the loser is told
                # rather than shown a 500.
                messages.info(request, str(refusal))
                return redirect("jobs:detail", pk=job.pk)
            notify(
                (
                    job.assigned_worker.user
                    if direction == ReviewDirection.CLIENT_ON_WORKER
                    else job.client.user
                ),
                Kind.RATING,
                job=job,
                actor=request.user,
                dedupe=booking_key("rating", job, direction),
                author=str(request.user),
                job_title=job.title,
                rating=review.rating,
                maximum=rules.RATING_MAX,
                comment=review.comment[:300],
            )
            messages.success(request, _("Thanks — that's on their profile now."))
            return redirect("jobs:detail", pk=job.pk)
    else:
        form = ReviewForm()

    return render(
        request,
        "jobs/review_form.html",
        {
            "job": job,
            "form": form,
            # Who they are rating, for the heading. Derived here rather than in
            # the template so the template cannot get the direction backwards.
            "subject": (
                job.assigned_worker.user
                if direction == ReviewDirection.CLIENT_ON_WORKER
                else job.client.user
            ),
        },
    )
