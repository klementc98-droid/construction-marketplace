"""Writing a job, changing it, and calling it off.

The client's side of the board. Editing and cancelling both write
conditionally: the state they check is one another request can move while a
form is open.
"""

from uuid import uuid4
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from notifications.models import Kind
from notifications.services import audience_for, booking_key, notify
from assistant.conversation import take_handoff
from core.models import Region
from core.state_machine import Actor, JobState, assert_transition, claim
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

from .common import _client




# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def _announce(job, *, actor) -> int:
    """Tell the trade that this work exists. Returns how many were told.

    The board is only useful to somebody who opens it, and the people this is
    for spend their day on a site rather than refreshing a page. This is the
    one notification that reaches out rather than answering something the
    recipient already did — which is why the audience rule lives in
    ``notifications.services.audience_for`` with its reasoning written down,
    and why it is deliberately narrow.

    Keyed on the booking, so posting four days announces one job rather than
    mailing every plumber in the city four times.
    """
    told = 0
    for worker in audience_for(job):
        if notify(
            worker.user,
            Kind.JOB_POSTED,
            job=job,
            actor=actor,
            dedupe=booking_key("posted", job, worker.pk),
            job_title=job.title,
            trade=str(job.trade),
            pay=str(job.fixed_pay or ""),
            hours=str(job.gig_hours or ""),
            when=date_format(job.gig_date, "D j M") if job.gig_date else "",
            where=job.location or "",
        ):
            told += 1
    return told




@login_required
def job_post_choose(request):
    """Pick the kind of post before filling anything in.

    The two types promise a worker different things — a rate, versus a fixed
    price for a named day — so the choice comes first and the form follows.
    """
    if _client(request) is None:
        messages.info(request, "Add a client profile to post work.")
        return redirect("accounts:select_role")
    return render(request, "jobs/job_post_choose.html")




@login_required
def job_post(request, job_type: str):
    client = _client(request)
    if client is None:
        messages.info(request, "Add a client profile to post work.")
        return redirect("accounts:select_role")

    form_class = JOB_FORMS.get(job_type)
    if form_class is None:
        return redirect("jobs:post_choose")

    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            region = Region.objects.filter(is_active=True).first()

            # A gig form asks for days, plural, and each one becomes its own
            # gig — same as a direct offer, and for the same reason: a gig is
            # one dated shift with its own escrow and its own sign-off. A
            # standing position has no date at all and takes the single path.
            days = form.cleaned_data.get("gig_dates") or [None]
            group = uuid4() if len(days) > 1 else None

            with transaction.atomic():
                posted = []
                for day in days:
                    job = form.save(commit=False)
                    # save(commit=False) returns the same instance each time, so
                    # it carries the previous day's key. Clearing both makes the
                    # next save an INSERT rather than an overwrite.
                    job.pk = None
                    job._state.adding = True
                    job.client = client
                    job.job_type = job_type
                    job.offer_group = group
                    if day is not None:
                        job.gig_date = day
                    if job.region_id is None:
                        job.region = region
                    job.full_clean()
                    job.save()
                    posted.append(job)

            # Told once, about the booking, to the people whose trade it is.
            # Outside the transaction above on purpose: this is a fan-out over
            # every matching worker, and holding a write transaction open for
            # the length of it would make posting a job slower the more people
            # there are to tell — exactly backwards.
            _announce(posted[0], actor=request.user)

            if len(posted) == 1:
                messages.success(request, _("Posted. Workers can see it now."))
                return redirect("jobs:detail", pk=posted[0].pk)

            messages.success(
                request,
                _("%(count)s days posted, one gig each. Workers can see them now.")
                % {"count": len(posted)},
            )
            return redirect("jobs:mine")
    else:
        # Arriving from the chat assistant. Still an ordinary unbound form:
        # nothing is written yet, corrections are made by typing into the
        # fields, and posting runs the same validation as any other post.
        prefill = take_handoff(request, job_type)
        if prefill:
            messages.info(
                request,
                "Here's what we filled in together. Check it over, change "
                "anything that isn't right, then post it.",
            )
        form = form_class(initial=prefill or None)

    return render(
        request,
        "jobs/job_form.html",
        {"form": form, "job_type": job_type, "is_gig": job_type == JobType.GIG},
    )




@login_required
def job_edit(request, pk: int):
    job = get_object_or_404(Job, pk=pk, client=_client(request))
    if not job.is_open:
        messages.error(request, "This job has moved on — it can no longer be edited.")
        return redirect("jobs:detail", pk=job.pk)

    form_class = JOB_FORMS[job.job_type]
    if request.method == "POST":
        form = form_class(request.POST, instance=job)
        if form.is_valid():
            edited = form.save(commit=False)
            edited.full_clean()

            # Written as a conditional UPDATE on the columns this form owns,
            # and both halves of that are load-bearing.
            #
            # Conditional, because the check above read a state that another
            # request can move while this form is being filled in. A worker
            # accepting between the two turns this into an edit of a job
            # somebody has already agreed to — and the fields here are the
            # money: a stale form could write €70 onto a day accepted at €100,
            # and funding takes its amount straight from the job.
            #
            # And only these columns, because ``job.save()`` writes the whole
            # row from an instance read before the accept. That is worse than
            # the wrong price: state and assigned_worker travel with it, so the
            # save could put the job back to POSTED and erase the worker
            # entirely, leaving the offer and application rows pointing at an
            # acceptance the job no longer records.
            columns = [
                name
                for name in form._meta.fields
                if name not in ("state", "assigned_worker")
            ]
            values = {name: getattr(edited, name) for name in columns}
            if job.is_gig:
                # Not in Meta.fields — GigForm derives it from the day picker.
                values["gig_date"] = edited.gig_date
            values["updated_at"] = timezone.now()

            written = Job.objects.filter(pk=job.pk, state=JobState.POSTED).update(
                **values
            )
            if not written:
                messages.error(
                    request,
                    "Somebody answered this job while you were editing it — "
                    "nothing has been changed. Open it to see where it stands.",
                )
                return redirect("jobs:detail", pk=job.pk)
            messages.success(request, "Updated.")
            return redirect("jobs:detail", pk=job.pk)
    else:
        form = form_class(instance=job)

    return render(
        request,
        "jobs/job_form.html",
        {"form": form, "job": job, "job_type": job.job_type, "is_gig": job.is_gig},
    )




@login_required
@require_POST
def job_cancel(request, pk: int):
    job = get_object_or_404(Job, pk=pk, client=_client(request))
    # Route the change through the state machine rather than assigning the
    # state directly, so an illegal move raises instead of being written.
    assert_transition(job.state, JobState.CANCELLED, Actor.CLIENT)

    # And then claim it, which is the half that was missing. The check above
    # judges a state read a moment ago, and _seal exists precisely because
    # another request can move that state in between — a worker accepting as
    # the client cancels. Without the claim this wrote CANCELLED over an
    # acceptance that had already happened, leaving a cancelled job with a
    # worker assigned to it and an offer row saying yes.
    if not claim(Job, job.pk, expect=job.state, to=JobState.CANCELLED):
        messages.error(
            request,
            "This job moved while you were cancelling it — nothing has been "
            "changed. Open it to see where it stands.",
        )
        return redirect("jobs:detail", pk=job.pk)
    messages.success(request, "Cancelled.")
    return redirect("jobs:mine")
