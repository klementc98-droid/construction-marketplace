"""A worker putting themselves forward, and a client answering.

The mirror of offers. Confirming somebody goes through _seal, which is what
decides ownership; nothing in this module grants it.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, models, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from notifications.models import Kind
from notifications.services import audience_for, booking_key, notify
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

from .common import _back_to, _booking_days, _client, _seal, _worker




@login_required
def job_apply(request, pk: int):
    job = get_object_or_404(Job.objects.select_related("trade"), pk=pk)
    worker = _worker(request)
    if worker is None:
        messages.info(request, "Add a worker profile to apply for work.")
        return redirect("accounts:select_role")
    # Hiding the button is presentation; this is the rule. A private gig is
    # answered by accepting the offer, not applied to — including by the very
    # worker it was written for, who would otherwise end up holding both an
    # offer and an application for the same job.
    if job.is_private:
        raise Http404("No job matches the given query.")
    if not job.is_open:
        messages.error(request, "This job is no longer taking applications.")
        return _back_to(request, job)

    existing = job.application_from(worker)
    if request.method == "POST":
        form = ApplicationForm(request.POST, instance=existing)
        if form.is_valid():
            # One application, however many days the booking runs. A five-day
            # booking is five rows underneath — each day carries its own escrow
            # and its own sign-off — but nobody applies for Tuesday and thinks
            # they have not applied for Wednesday. Applying once applies to the
            # booking, so the row per day is bookkeeping the reader never meets.
            days = _booking_days(job)
            with transaction.atomic():
                # Re-read inside the transaction: the open check at the top of
                # this view judged a state from before the form was filled in.
                # Somebody confirmed in that window leaves an APPLIED row on a
                # job that is taken — an application nobody will ever answer,
                # sitting in that worker's list looking live.
                #
                # This narrows the window rather than closing it, and the
                # difference is worth being exact about: an application has no
                # status of its own to make the write conditional on. What is
                # guaranteed is the part that matters — who gets the job is
                # decided by the conditional UPDATE in _seal, and nothing
                # arriving late can win it there.
                if not Job.objects.filter(
                    pk=job.pk, state=JobState.POSTED
                ).exists():
                    messages.error(
                        request,
                        "Somebody was confirmed for this job while you were "
                        "writing — nothing has been sent.",
                    )
                    return redirect("jobs:list")
                for day in days:
                    application = form.save(commit=False)
                    application.pk = None
                    application._state.adding = True
                    application.job = day
                    application.worker = worker
                    # Re-applying after withdrawing puts you back in the running
                    # rather than creating a second row — see the unique
                    # constraint, which update_or_create honours per day.
                    Application.objects.update_or_create(
                        job=day,
                        worker=worker,
                        defaults={
                            "message": application.message,
                            "status": ApplicationStatus.APPLIED,
                            "responded_at": None,
                        },
                    )
            notify(
                job.client.user,
                Kind.APPLICATION,
                job=job,
                actor=request.user,
                # The applicant is in the key: two people applying to one
                # booking is two pieces of news, and collapsing them would tell
                # the client about the first and swallow the second.
                dedupe=booking_key("application", job, worker.pk),
                worker=str(request.user),
                job_title=job.title,
                note=form.cleaned_data.get("message", "")[:300],
                path=reverse("jobs:applicants", args=[job.pk]),
            )

            if len(days) > 1:
                messages.success(
                    request,
                    _("Applied for all %(count)s days.") % {"count": len(days)},
                )
            else:
                messages.success(request, _("Application sent."))
            return redirect("jobs:detail", pk=job.pk)
    else:
        form = ApplicationForm(instance=existing)

    return render(
        request, "jobs/job_apply.html", {"job": job, "form": form, "existing": existing}
    )




@login_required
@require_POST
def application_withdraw(request, pk: int):
    application = get_object_or_404(Application, pk=pk, worker=_worker(request))

    # Only from APPLIED, and claimed rather than assumed. There was no state
    # check here at all, so a worker who had just been picked could withdraw
    # the application that got them the job — leaving the job saying they
    # accepted it and the application saying they walked away. The job is
    # sealed by then; nothing here can unseal it, and the record should not
    # pretend otherwise.
    if not claim(
        Application,
        application.pk,
        field="status",
        expect=ApplicationStatus.APPLIED,
        to=ApplicationStatus.WITHDRAWN,
        responded_at=timezone.now(),
    ):
        application.refresh_from_db()
        messages.info(
            request,
            "That application has already been answered — nothing has been "
            "changed."
            if application.status != ApplicationStatus.WITHDRAWN
            else "Already withdrawn.",
        )
        return redirect("jobs:detail", pk=application.job_id)

    messages.success(request, "Withdrawn.")
    return redirect("jobs:detail", pk=application.job_id)




# ---------------------------------------------------------------------------
# Reviewing applicants
# ---------------------------------------------------------------------------


@login_required
def job_applicants(request, pk: int):
    job = get_object_or_404(Job, pk=pk, client=_client(request))
    applications = list(
        job.applications.select_related("worker__user", "worker__region")
        .prefetch_related("worker__trades")
    )

    # The question this page exists to answer is "who should I pick", and for a
    # dated gig the first filter is "who can actually make the day". Computed
    # here rather than in the template because it needs the job's date as an
    # argument, and a template cannot pass one.
    for application in applications:
        application.free_on_date = (
            application.worker.is_free_on(job.gig_date) if job.gig_date else None
        )

    # Who is asking for something other than the posted price. Fetched in one
    # query and attached, rather than a property the template would call once
    # per row — this page is a list, and a per-row lookup is how a list gets
    # slow without anybody noticing.
    asking = {c.worker_id: c for c in job.live_counters}
    for application in applications:
        counter = asking.get(application.worker_id)
        application.live_counter = counter
        # Whose move it is, per applicant. A client who has countered is
        # waiting on the worker, and must not be shown a button that accepts
        # their own proposal on the worker's behalf — see the template.
        application.needs_client_answer = (
            counter is not None and counter.answered_by == Party.CLIENT
        )

    # Whoever can make it, first. Then the maybes, then the clashes — the
    # ordering a person would apply by hand anyway.
    rank = {True: 0, None: 1, False: 2}
    applications.sort(key=lambda a: (rank[a.free_on_date], -a.pk))

    return render(
        request,
        "jobs/job_applicants.html",
        {"job": job, "applications": applications},
    )




@login_required
@require_POST
def application_select(request, pk: int):
    """Pick this applicant, and answer the others.

    One transaction: assigning the worker, closing the job, and passing over
    everyone else are a single decision. A crash halfway through must not
    leave a job with a worker assigned and the rest still waiting.
    """
    application = get_object_or_404(
        Application.objects.select_related("job", "worker__user"),
        pk=pk,
        job__client=_client(request),
    )
    job = application.job
    if not job.is_open:
        messages.error(request, "Someone has already been selected for this job.")
        return redirect("jobs:applicants", pk=job.pk)

    # Somebody who has asked for a different price has not agreed to the posted
    # one, and this button would book them at it. Their terms are a decision the
    # client has to make explicitly — there is an accept button for exactly that
    # on the applicants page.
    if job.live_counter_from(application.worker) is not None:
        messages.error(
            request,
            f"{application.worker.user} has asked for different terms — "
            "answer those first.",
        )
        return redirect("jobs:applicants", pk=job.pk)

    # Confirming somebody books the booking, not one day of it. They applied
    # once for all five days; being given Tuesday and left waiting on Wednesday
    # is not an answer to what they asked.
    days = _booking_days(job)

    # And not if they are already spoken for on any of those days. Said here,
    # by name and with the dates, rather than left to the constraint — "that
    # job isn't available" would be a lie about which of the two things went
    # wrong, and the client's next move (pick somebody else, or shift a day)
    # depends on knowing which days are the problem.
    clash = clashing_dates(application.worker, days)
    if clash:
        messages.error(
            request,
            _("%(who)s is already booked on %(days)s. Nothing has been changed.")
            % {"who": application.worker.user, "days": describe_dates(clash)},
        )
        return redirect("jobs:applicants", pk=job.pk)

    now = timezone.now()
    sealed = []
    with transaction.atomic():
        for day in days:
            result = _seal(day.pk, application.worker, None, now, actor=Actor.CLIENT)
            if result is not None:
                sealed.append(result)

    if not sealed:
        messages.error(request, _("Someone has already been selected for this job."))
        return redirect("jobs:applicants", pk=application.job_id)

    notify(
        application.worker.user,
        Kind.SELECTED,
        job=sealed[0],
        actor=request.user,
        dedupe=booking_key("selected", sealed[0], application.worker_id),
        client=str(request.user),
        job_title=sealed[0].title,
        pay=str(sealed[0].fixed_pay),
        hours=str(sealed[0].gig_hours),
    )

    if len(sealed) > 1:
        messages.success(
            request,
            _("%(who)s has all %(count)s days.")
            % {"who": application.worker.user, "count": len(sealed)},
        )
    else:
        messages.success(
            request, _("%(who)s has the job.") % {"who": application.worker.user}
        )
    return redirect("jobs:detail", pk=sealed[0].pk)
