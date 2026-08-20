"""Phase 2 views: posting, browsing, searching, applying, and selecting.

Role is not a field on the user (see ``accounts.models``), so these views ask
"does this person have the profile this action needs?" rather than "what role
are they?". The same account can post a gig in the morning and apply to one in
the afternoon.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.template.defaultfilters import floatformat
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import AvailabilityStatus, WorkerProfile
from notifications.models import Kind
from notifications.services import audience_for, booking_key, notify
from config import business_rules as rules
from assistant.conversation import take_handoff
from core.models import Region
from core.state_machine import Actor, JobState, assert_transition, claim

from .forms import (
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
from .services import (
    ReviewError,
    booked_days_among,
    clashing_dates,
    describe_dates,
    leave_review,
)
from .waiting import waiting_for
from .models import (
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


def _worker(request) -> WorkerProfile | None:
    if not request.user.is_authenticated:
        return None
    return getattr(request.user, "worker_profile", None)


def _client(request):
    if not request.user.is_authenticated:
        return None
    return getattr(request.user, "client_profile", None)


# ---------------------------------------------------------------------------
# Browse and search
# ---------------------------------------------------------------------------


def job_list(request):
    """The board. Open to signed-out visitors — the jobs are the shop window.

    Applying needs an account; reading does not. Making people sign in before
    they can see whether there is any work worth signing in for is how a
    two-sided marketplace fails to start.
    """
    form = JobFilterForm(request.GET or None)
    jobs = form.filtered(
        Job.objects.public().select_related("trade", "region", "client__user")
    )
    # One row per booking, not per day — see collapse_groups. The count follows
    # the rows for the same reason: "12 open posts" for four bookings is a
    # number that does not match anything the reader can see.
    rows = collapse_groups(jobs.order_by("gig_date", "-created_at"))
    return render(
        request,
        "jobs/job_list.html",
        {"form": form, "jobs": rows, "total": len(rows)},
    )


def worker_list(request):
    """The other half of "search/filter": clients looking for people."""
    form = WorkerFilterForm(request.GET or None)
    workers = WorkerProfile.objects.select_related("user", "region").prefetch_related(
        "trades"
    )

    if form.is_valid():
        term = form.cleaned_data.get("q")
        if term:
            workers = workers.filter(
                Q(user__full_name__icontains=term)
                | Q(bio__icontains=term)
                | Q(service_area__icontains=term)
            )
        trade = form.cleaned_data.get("trade")
        if trade:
            workers = workers.filter(trades=trade)
        if form.cleaned_data.get("available_now"):
            workers = workers.filter(
                availability_status=AvailabilityStatus.AVAILABLE_NOW
            )
        if form.cleaned_data.get("full_time"):
            # Only an explicit yes. A worker who was never asked is not a "no",
            # but they are not a lead for a permanent role either.
            workers = workers.filter(open_to_full_time=True)

    # distinct() because filtering on the trades M2M can otherwise return the
    # same worker once per matching trade.
    workers = workers.distinct()
    return render(
        request,
        "jobs/worker_list.html",
        {"form": form, "workers": workers, "total": workers.count()},
    )


def job_detail(request, pk: int):
    job = get_object_or_404(
        Job.objects.select_related(
            "trade", "region", "client__user", "assigned_worker__user"
        ),
        pk=pk,
    )
    worker = _worker(request)
    client = _client(request)
    is_owner = client is not None and job.client_id == client.pk

    # A direct offer is not a listing. 404 rather than a "not allowed" page:
    # confirming the post exists would leak that this client is hiring and
    # roughly for what, to anyone willing to walk the ID range.
    if not job.is_visible_to(request.user):
        raise Http404("No job matches the given query.")

    my_offer = (
        job.offers.filter(worker=worker).select_related("worker__user").first()
        if worker is not None
        else None
    )

    # The sibling days of a multi-day booking, for the note on this one.
    group_dates: list = []
    group_days = 1
    group_pay = job.fixed_pay
    group_hours = job.gig_hours
    if job.offer_group:
        siblings = list(Job.objects.filter(offer_group=job.offer_group))
        group_dates = sorted(j.gig_date for j in siblings if j.gig_date)
        group_days = max(len(siblings), 1)
        # Summed, not multiplied: a counter is agreed per day, so the days of
        # one booking can end up on different numbers.
        group_pay = sum((j.fixed_pay or 0) for j in siblings)
        group_hours = sum((j.gig_hours or 0) for j in siblings)

    # The negotiation, from whichever side is looking.
    #
    # A worker sees their own thread. The client sees the one thread there is
    # when the gig is a direct offer, and nothing here when it is a public post
    # — several people may be asking several prices, and that belongs on the
    # applicants page where they can be compared, not stacked on the job.
    counter_worker = worker if worker is not None else (
        job.pending_offer.worker if is_owner and job.pending_offer else None
    )
    my_party = None
    if counter_worker is not None:
        my_party = Party.WORKER if worker is not None else Party.CLIENT

    return render(
        request,
        "jobs/job_detail.html",
        {
            "job": job,
            "is_owner": is_owner,
            "my_application": job.application_from(worker),
            # A private job is not applied to — the worker it was written for
            # answers the offer instead, and nobody else can see it at all.
            "can_apply": (
                worker is not None
                and job.is_open
                and not is_owner
                and not job.is_private
            ),
            "my_offer": my_offer,
            "pending_offer": job.pending_offer if is_owner else None,
            "offer_response_form": OfferResponseForm(),
            # Negotiation state, shared by both sides' panels.
            "my_party": my_party,
            "counter_worker": counter_worker,
            "live_counter": job.live_counter_from(counter_worker),
            "my_turn": (
                _turn(job, counter_worker) == my_party if my_party else False
            ),
            "terms": _effective_terms(job, counter_worker) if counter_worker else None,
            "counter_history": (
                list(job.counters.filter(worker=counter_worker))
                if counter_worker
                else []
            ),
            # The third answer on any open gig: not "yes" and not "no" but
            # "not at that price". Shown wherever applying is.
            "can_negotiate": job.can_negotiate(worker)
            and _may_propose(job, worker, Party.WORKER),
            "applicant_count": (
                job.applications.filter(status=ApplicationStatus.APPLIED).count()
                if is_owner
                else None
            ),
            # Decided here because a template cannot pass the viewer to a
            # method, and the answer depends on who is looking.
            # Who the work went to is between the two of them. Anyone else who
            # can open this page — somebody who applied and was passed over,
            # somebody who declined an offer — reads the job without reading
            # the name of whoever ended up with it.
            "is_a_party": job.parties_only(request.user),
            "can_review": job.can_be_reviewed_by(request.user),
            "my_review": job.review_from(request.user),
            # Which booking this day belongs to, if it belongs to one. Both
            # sides get it: the worker needs to know three days were agreed,
            # not one, and the client needs the same picture back.
            # The assigned worker specifically, not any worker looking at it —
            # the "job done" button belongs to the person doing the job.
            "is_worker_here": (
                worker is not None and job.assigned_worker_id == worker.pk
            ),
            "group_days": group_days,
            "group_pay": group_pay,
            "group_hours": group_hours,
            "group_first": group_dates[0] if group_dates else None,
            "group_last": group_dates[-1] if group_dates else None,
        },
    )


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


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _back_to(request, job):
    """The job page if they can still see it, the board if they cannot.

    A refusal that redirects to the thing being refused used to be harmless:
    every public post was readable by everyone. Now that a taken job is only
    open to the people it is between, sending a stranger there answers "you
    can't apply" with a 404 — technically the truth and useless as an answer.
    """
    if job.is_visible_to(request.user):
        return redirect("jobs:detail", pk=job.pk)
    return redirect("jobs:list")


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


# ---------------------------------------------------------------------------
# Direct offers
# ---------------------------------------------------------------------------
# The mirror of applying: a client writes a gig for one named worker instead of
# advertising it. The job is real from the moment it is sent — same table, same
# escrow, same lifecycle — it is simply flagged private so it never reaches the
# board. Accepting is then a state transition on a job that already exists,
# rather than a conversion step that copies six fields and could get one wrong.


def _offerable_jobs(client):
    """The client's own open gigs, as candidates to send somebody directly.

    Two filters, and the second is the one that is easy to miss. ``POSTED``
    because a gig that is taken, expired or cancelled is not work to offer.
    And nothing already holding a pending offer: ``one_pending_offer_per_job``
    exists so two people cannot both be sitting on an answer to the same gig,
    so listing one here would be offering a button that fails on submit.

    Gigs only. An offer is a dated shift with a price on it — that is what the
    worker is being asked to say yes to, and what the accept path writes. A
    standing position has neither, and inviting somebody into one is a
    different conversation than this screen is having.

    A job already offered to this worker and declined stays in the list. Asking
    again after a no is a normal thing to do, and the offer row is reused
    rather than stacked — see the unique constraint.
    """
    return (
        Job.objects.filter(
            client=client, state=JobState.POSTED, job_type=JobType.GIG
        )
        .exclude(offers__status=OfferStatus.PENDING)
        .select_related("trade")
        .order_by("gig_date", "pk")
    )


def _mark_unofferable(worker, bookings):
    """Tell each suggested booking which of its days this worker cannot take.

    Answered here, once, off the days already collapsed onto the row, rather
    than per row in the template — and it is the same question ``_seal`` asks
    at the other end, so a booking that reads as blocked here is exactly the
    one that would be refused there.

    A worker with a pending *offer* on that day is not blocked: two clients may
    both ask, and it is the answer that makes the second impossible. Only a day
    they have actually said yes to counts.
    """
    for booking in bookings:
        dates = getattr(booking, "group_dates", None) or [booking.gig_date]
        booking.clash_dates = booked_days_among(worker, dates)
        booking.clash_said = describe_dates(booking.clash_dates)
    return bookings


def _offer_existing(request, *, worker, offerable, bookings):
    """Send one of the client's existing posts, note and all.

    The whole point is that nothing is copied. The gig keeps its own title,
    date, hours and price; all this adds is an Offer row against it and the
    covering note as the first message. A retyped gig would be a second job
    meaning the same work, and the two would drift the first time either was
    edited.

    A booking is offered whole. Somebody sent Tuesday of a four-day job and
    left wondering about Wednesday is the exact confusion ``_booking_days``
    exists to prevent, and it is the same rule applying already follows.

    The post stays public. This is an invitation, not a withdrawal from the
    board: other people can still apply, and whoever the client ends up
    confirming, ``_seal`` gives the rest a definite answer.
    """
    from messaging.models import Conversation, Message

    if request.method != "POST":
        return render(
            request,
            "jobs/offer_choose.html",
            {"form": OfferExistingForm(offerable=offerable), "worker": worker,
             "bookings": bookings},
        )

    form = OfferExistingForm(request.POST, offerable=offerable)
    if not form.is_valid():
        return render(
            request,
            "jobs/offer_choose.html",
            {"form": form, "worker": worker, "bookings": bookings},
        )

    job = form.cleaned_data["job"]
    note = form.cleaned_data["note"]
    days = _booking_days(job)

    # The row for this one is disabled on the page, so reaching here means the
    # page was stale or the control was edited. Either way the answer is the
    # same one the accept would give, said earlier and with the days named.
    clash = clashing_dates(worker, days)
    if clash:
        form.add_error(
            "job",
            _("%(who)s is already booked on %(days)s.")
            % {"who": worker.user, "days": describe_dates(clash)},
        )
        return render(
            request,
            "jobs/offer_choose.html",
            {"form": form, "worker": worker, "bookings": bookings},
        )

    try:
        with transaction.atomic():
            # Same reasoning as applying, from the other side: the list this
            # was chosen from was drawn before the form was filled in, and a
            # job confirmed in that window would otherwise end up with a
            # pending offer sitting on it — two people apparently owed an
            # answer about work that is already somebody's.
            if not Job.objects.filter(
                pk__in=[day.pk for day in days], state=JobState.POSTED
            ).exists():
                messages.error(
                    request,
                    "That job was answered while you were writing — nothing "
                    "has been sent.",
                )
                return redirect("jobs:mine")

            for day in days:
                # update_or_create, because asking again after a decline should
                # reuse that row rather than stack a second one — the same rule
                # re-applying follows, and the same unique constraint behind it.
                Offer.objects.update_or_create(
                    job=day,
                    worker=worker,
                    defaults={
                        "note": note,
                        "status": OfferStatus.PENDING,
                        "response_note": "",
                        "responded_at": None,
                    },
                )
                conversation, _created = Conversation.objects.get_or_create(
                    job=day, worker=worker
                )
                if note:
                    message = Message.objects.create(
                        conversation=conversation, sender=request.user, body=note
                    )
                    conversation.last_message_at = message.created_at
                    conversation.save(
                        update_fields=["last_message_at", "updated_at"]
                    )
    except IntegrityError:
        # one_pending_offer_per_job, lost between rendering the list and
        # submitting it. Somebody else is holding an answer to this gig now.
        messages.error(
            request,
            "That job already has an offer out with somebody. Withdraw it "
            "first, or pick another.",
        )
        return redirect("jobs:offer", worker_pk=worker.pk)

    notify(
        worker.user,
        Kind.OFFER_RECEIVED,
        job=days[0],
        actor=request.user,
        dedupe=booking_key("offer", days[0], worker.pk),
        client=str(request.user),
        job_title=days[0].title,
        # The figures, not a sentence built from them. A rendered "€50 for 8
        # hours" would be frozen in whichever language the client happened to
        # be using, and read back to a Greek worker in English. Numbers survive
        # the trip; the words are chosen when it is sent.
        pay=str(days[0].fixed_pay),
        hours=str(days[0].gig_hours),
        note=note[:300],
    )

    who = worker.user.short_name or worker.user
    if len(days) > 1:
        messages.success(
            request,
            _("Offered to %(who)s — all %(count)s days.")
            % {"who": who, "count": len(days)},
        )
    else:
        messages.success(request, _("Offered to %(who)s.") % {"who": who})
    return redirect("jobs:detail", pk=days[0].pk)


@login_required
def offer_create(request, worker_pk: int):
    """Write and send a direct offer to one worker."""
    # Local import: messaging imports from jobs, so taking the dependency at
    # module level would close the cycle. Only this one view needs it.
    from messaging.models import Conversation, Message

    worker = get_object_or_404(
        WorkerProfile.objects.select_related("user", "region"), pk=worker_pk
    )
    client = _client(request)
    if client is None:
        messages.info(request, "Add a client profile to offer work.")
        return redirect("accounts:select_role")
    if worker.user_id == request.user.pk:
        messages.error(request, "You can't offer yourself a job.")
        return redirect("accounts:worker_detail", pk=worker.pk)
    if not worker.can_receive_offers:
        messages.info(
            request,
            f"{worker.user.short_name or worker.user} isn't taking work right now.",
        )
        return redirect("accounts:worker_detail", pk=worker.pk)

    # Two ways to reach one person, and the shorter one first. A client with
    # posts already up is usually trying to put one of those in front of
    # somebody rather than to write a second copy of it, so the list is what
    # opens and writing a new gig is a link off it rather than the only door.
    #
    # One URL, and a submit says outright which of the two it is: the chooser
    # sends ``pick``, the writer sends a whole gig. Inferring it from the query
    # string instead would make the answer depend on a round trip surviving,
    # and would quietly reroute anything that posted here without one.
    #
    # ``?new=`` only steers a GET — it is how somebody leaves the list, and it
    # has to stay on the URL so that a form which fails validation redisplays
    # as the writer rather than bouncing back to the list.
    offerable = _offerable_jobs(client)
    bookings = _mark_unofferable(worker, collapse_groups(list(offerable)))
    choosing = "pick" in request.POST or (
        request.method == "GET" and bookings and not request.GET.get("new")
    )
    if choosing:
        return _offer_existing(request, worker=worker, offerable=offerable,
                               bookings=bookings)

    if request.method == "POST":
        form = OfferForm(request.POST, worker=worker)
        if form.is_valid():
            days = form.cleaned_data["gig_dates"]
            note = form.cleaned_data["note"]

            # One gig per day. A gig is one dated shift with its own escrow and
            # its own sign-off, so two days cannot share a row: either can be
            # finished, disputed or called off while the other runs normally,
            # and a single job would have nowhere to record that.
            #
            # All of them in one transaction — a client who picked three days
            # and got two is worse off than one who got an error.
            # One group id across the days, so an answer about the arrangement
            # rather than the day — "escrow, not cash" — can find its siblings.
            # Only when there is more than one: a single-day offer has no group
            # to belong to and a stray id would imply otherwise.
            group = uuid4() if len(days) > 1 else None

            with transaction.atomic():
                jobs = []
                for day in days:
                    job = form.save(commit=False)
                    # save(commit=False) hands back the same instance each time,
                    # so it carries the previous day's primary key. Clearing both
                    # makes the next save an INSERT rather than an overwrite.
                    job.pk = None
                    job._state.adding = True
                    job.gig_date = day
                    job.client = client
                    job.is_private = True
                    job.offer_group = group
                    job.save()
                    jobs.append(job)

                    offer = Offer.objects.create(job=job, worker=worker, note=note)

                    # A thread from the start. An offer someone wants to ask one
                    # question about should not need them to say no to get a
                    # reply channel — and the client's covering note is the
                    # natural first message, so it lands where the answer will.
                    conversation = Conversation.objects.create(
                        job=job, worker=worker
                    )
                    if offer.note:
                        message = Message.objects.create(
                            conversation=conversation,
                            sender=request.user,
                            body=offer.note,
                        )
                        conversation.last_message_at = message.created_at
                        conversation.save(
                            update_fields=["last_message_at", "updated_at"]
                        )

            if len(jobs) == 1:
                messages.success(
                    request,
                    _("Offer sent to %(worker)s. You'll hear back here and in "
                      "messages.") % {"worker": worker.user},
                )
                return redirect("jobs:detail", pk=jobs[0].pk)

            messages.success(
                request,
                _("%(count)s offers sent to %(worker)s, one for each day. They "
                  "can take all of them or only the days that suit.")
                % {"count": len(jobs), "worker": worker.user},
            )
            return redirect("jobs:mine")
    else:
        form = OfferForm(worker=worker)

    return render(
        request,
        "jobs/offer_form.html",
        {"form": form, "worker": worker, "has_open_jobs": bool(bookings)},
    )


@login_required
@require_POST
def offer_respond(request, pk: int):
    """The worker's yes or no.

    Accepting is what fills the job, so it runs in one transaction and is
    guarded by the state machine: if anything else took the gig in the seconds
    the worker spent deciding, the transition raises rather than quietly
    overwriting whoever already has it.
    """
    worker = _worker(request)
    offer = get_object_or_404(
        Offer.objects.select_related("job__client__user", "worker__user"),
        pk=pk,
        worker=worker,
    )
    job = offer.job

    if not offer.is_pending:
        messages.info(request, "You've already answered that offer.")
        return redirect("jobs:detail", pk=job.pk)

    form = OfferResponseForm(request.POST)
    note = form.cleaned_data["response_note"] if form.is_valid() else ""
    accepting = request.POST.get("answer") == "accept"
    now = timezone.now()

    if not accepting:
        # No answers the booking, the same as yes does. They were shown one
        # offer and turned it down once; leaving the other days pending meant
        # the app went on saying a job was waiting for them after they had
        # said no to it, and the client went on waiting for an answer that had
        # already been given.
        Offer.objects.filter(
            job__in=_booking_days(job),
            worker=worker,
            status=OfferStatus.PENDING,
        ).update(
            status=OfferStatus.DECLINED,
            response_note=note,
            responded_at=now,
            updated_at=now,
        )
        notify(
            job.client.user,
            Kind.OFFER_ANSWERED,
            job=job,
            actor=request.user,
            dedupe=booking_key("answered", job, worker.pk),
            worker=str(request.user),
            job_title=job.title,
            accepted=False,
            note=note[:300],
        )
        messages.success(request, _("Declined. The client has been told."))
        return redirect("jobs:mine")

    if not job.is_open:
        # Cancelled or filled while they were reading it. Claimed like the
        # rest: if they answered it in the same moment, their answer stands.
        claim(
            Offer,
            offer.pk,
            field="status",
            expect=OfferStatus.PENDING,
            to=OfferStatus.WITHDRAWN,
            responded_at=now,
        )
        messages.error(request, "That offer is no longer available.")
        return redirect("jobs:mine")

    # Yes to the booking, not to one day of it. A four-day offer is four rows
    # because each day carries its own escrow and its own sign-off, but nobody
    # was offered four things — they were offered a week, answered once, and
    # the answer has to reach every day of it. Otherwise the client is left
    # holding three live offers nobody will ever answer and three days still
    # sitting on the board, which is exactly what a "yes" was meant to end.
    #
    # The same rule the applying side has always followed; see the note in
    # application_select, and collapse_rows for why the reader only ever saw
    # one offer to say yes to in the first place.
    #
    # Each day's own offer row is the one that gets answered — status, note and
    # timestamp belong to the day — and each day's own counter is what gets
    # written, because terms are agreed per day.
    days = _booking_days(job)

    # Not if they have already said yes to somebody else on one of these days.
    # The dates are named because the answer is usually "so decline this one",
    # and that decision needs to know which days overlap.
    clash = clashing_dates(worker, days)
    if clash:
        messages.error(
            request,
            _("You're already booked on %(days)s, so this one can't be accepted "
              "as it stands. Nothing has been changed.")
            % {"days": describe_dates(clash)},
        )
        return redirect("jobs:detail", pk=job.pk)

    day_offers = {
        o.job_id: o
        for o in Offer.objects.filter(job__in=days, worker=worker, status=OfferStatus.PENDING)
    }

    sealed = []
    with transaction.atomic():
        for day in days:
            day_offer = day_offers.get(day.pk)
            if day_offer is None:
                continue
            day_offer.response_note = note
            result = _seal(
                day.pk,
                worker,
                day_offer.live_counter,
                now,
                offer=day_offer,
                actor=Actor.WORKER,
            )
            if result is not None:
                sealed.append(result)

    if not sealed:
        messages.error(request, "That offer is no longer available.")
        return redirect("jobs:mine")

    notify(
        job.client.user,
        Kind.OFFER_ANSWERED,
        job=sealed[0],
        actor=request.user,
        dedupe=booking_key("answered", sealed[0], worker.pk),
        worker=str(request.user),
        job_title=sealed[0].title,
        accepted=True,
        note=note[:300],
    )

    # Honest about a partial answer. Every day of the booking is claimed on its
    # own — one can be taken, cancelled or blocked while the others go through
    # — and saying "all N days are yours" while one of them is still sitting
    # open on the board is how somebody ends up believing they have a week they
    # have not got.
    missed = len(day_offers) - len(sealed)
    if missed:
        messages.warning(
            request,
            _("Took %(taken)s of the %(total)s days. %(missed)s could not be "
              "accepted — it was taken, cancelled or clashes with work you "
              "already have. The rest are yours.")
            % {"taken": len(sealed), "total": len(day_offers), "missed": missed},
        )
    elif len(sealed) > 1:
        messages.success(
            request,
            _("Accepted — all %(count)s days are yours.") % {"count": len(sealed)},
        )
    else:
        messages.success(request, _("Accepted — the job is yours."))
    return redirect("jobs:detail", pk=sealed[0].pk)


# ---------------------------------------------------------------------------
# Counter-offers
# ---------------------------------------------------------------------------
# Either side may put revised terms on the table, and they alternate until one
# of them says yes. The job keeps the terms it was posted with throughout — a
# counter is a proposal *about* the job, and only accepting writes to it. That
# ordering is the whole safety story: the job's fixed_pay is what the client's
# card is charged, so it must never move while nobody has agreed to it.


def _booking_days(job):
    """Every job in this booking, oldest day first — or just this one.

    A multi-day booking is several rows and has to be: each day carries its own
    escrow, sign-off and expiry. Anything a person does to "the job" as a whole
    — applying, being confirmed — has to reach all of them, or the reader ends
    up half in and half out of something they answered once.
    """
    return booking_of(job, states=[JobState.POSTED])


def _effective_terms(job, worker):
    """The terms as they stand for this pair: their live counter over the job.

    What a new counter is measured against, and what "accept" would agree to.
    """
    counter = job.live_counter_from(worker)
    return SimpleNamespace(
        fixed_pay=(counter.fixed_pay if counter and counter.fixed_pay is not None else job.fixed_pay),
        gig_hours=(counter.gig_hours if counter and counter.gig_hours is not None else job.gig_hours),
        gig_date=(counter.gig_date if counter and counter.gig_date is not None else job.gig_date),
        use_escrow=(
            counter.use_escrow
            if counter and counter.use_escrow is not None
            else job.use_escrow
        ),
    )


def _party(request, job, worker) -> str | None:
    """Which side of a (job, worker) negotiation the viewer is, if either."""
    me = _worker(request)
    client = _client(request)
    if me is not None and worker is not None and me.pk == worker.pk:
        return Party.WORKER
    if client is not None and job.client_id == client.pk:
        return Party.CLIENT
    return None


def _turn(job, worker) -> str | None:
    """Whose move it is in this thread.

    ``None`` means nobody owes an answer — an open public gig that this worker
    has not said anything about yet. They may open a negotiation from there;
    the client may not, because approaching a specific worker unprompted is
    what a direct offer already is.
    """
    counter = job.live_counter_from(worker)
    if counter is not None:
        return counter.answered_by
    offer = job.offers.filter(worker=worker, status=OfferStatus.PENDING).first()
    return Party.WORKER if offer is not None else None


def _may_propose(job, worker, party) -> bool:
    turn = _turn(job, worker)
    return turn == party or (turn is None and party == Party.WORKER)


def _seal(job_id, worker, counter, now, offer=None, actor=Actor.WORKER):
    """Assign the worker and close the job. The one place a deal is struck.

    Every route to a filled job goes through here — accepting an offer,
    accepting a counter on a direct offer, accepting a counter on a public
    gig — so none of them can drift from the others on the things that must
    always happen: the state transition, the losing applicants getting a
    definite answer, and any agreed terms being written before the job closes.

    The job is re-read inside the caller's transaction. Between rendering the
    button and this running, the gig may have been cancelled or taken, and the
    state machine has to judge the row as it is now rather than as it was on
    the page.

    Two people can be answering the same gig in the same second — a client
    picking an applicant while a worker accepts their offer — so the claim
    itself is a single conditional UPDATE, not a read followed by a write:

        UPDATE ... SET state = 'accepted', assigned_worker = ... WHERE state = 'posted'

    The database decides, and it decides once. Nought rows back means somebody
    else got there first, and the caller says so.

    This replaced ``select_for_update``, which reads like a lock and is not one
    everywhere: SQLite has no FOR UPDATE, and Django drops the clause silently
    rather than raising, so on the development database the guard was a plain
    read-check-write with a real window between the two halves. The failure it
    let through is the expensive kind — two workers both told the job is theirs
    and the second write quietly overwriting the first, with no constraint to
    catch it, because a job has one assigned_worker field and no unique index
    saying so. A conditional UPDATE needs no lock and is atomic on both.

    Nothing is written before the claim succeeds. Returning early inside the
    caller's ``atomic`` block commits whatever already happened, so accepting
    the counter above the claim would mark it accepted for a job somebody else
    just took.
    """
    job = Job.objects.get(pk=job_id)
    if not job.is_open:
        return None

    assert_transition(job.state, JobState.ACCEPTED, actor)

    fields = ["state", "assigned_worker", "filled_at", "updated_at"]
    if counter is not None:
        fields += counter.apply_to(job)

    job.state = JobState.ACCEPTED
    job.assigned_worker = worker
    job.filled_at = now
    # auto_now does not fire on .update(), so the stamp is passed by hand —
    # the same as every other .update() in this file.
    job.updated_at = now

    try:
        # Its own savepoint. The claim can now fail on the double-booking
        # index — two clients confirming the same worker for the same day in
        # the same second, which is the case the pre-flight check in the views
        # above cannot see — and an IntegrityError left to escape would mark
        # the whole surrounding transaction as broken, taking the other days of
        # the booking with it. Losing the race means the same thing here as
        # losing it to a state change: nothing was claimed.
        with transaction.atomic():
            claimed = Job.objects.filter(pk=job_id, state=JobState.POSTED).update(
                **{name: getattr(job, name) for name in dict.fromkeys(fields)}
            )
    except IntegrityError:
        return None
    if not claimed:
        return None

    # Past this line the job is ours and the rest of the deal can be written.
    if counter is not None:
        counter.status = CounterStatus.ACCEPTED
        counter.responded_at = now
        counter.save(update_fields=["status", "responded_at", "updated_at"])

    # An agreed payment method covers the whole offer, not the one day being
    # accepted. Escrow on Tuesday and cash on Wednesday is not an arrangement
    # anybody asked for, and it is what a per-day answer would produce on a
    # three-day offer. The price and the hours stay per day — those genuinely
    # can differ, and countering one day's money says nothing about another's.
    #
    # Only the days still open: a sibling already accepted or cancelled has its
    # own settled terms and is none of this decision's business.
    if counter is not None and counter.use_escrow is not None and job.offer_group:
        Job.objects.filter(
            offer_group=job.offer_group, state=JobState.POSTED
        ).exclude(pk=job.pk).update(
            use_escrow=counter.use_escrow, updated_at=now
        )

    # A definite "no" beats an application that just goes quiet — the same
    # courtesy application_select has always paid, now owed by every route.
    selected = job.applications.filter(worker=worker).first()
    if selected is not None:
        selected.status = ApplicationStatus.SELECTED
        selected.responded_at = now
        selected.save(update_fields=["status", "responded_at", "updated_at"])
    job.applications.filter(status=ApplicationStatus.APPLIED).exclude(
        worker=worker
    ).update(status=ApplicationStatus.PASSED_OVER, responded_at=now, updated_at=now)

    # Everyone else's asking price is moot now the gig is taken. Left pending
    # they would sit in those workers' lists looking like live decisions.
    job.counters.filter(status=CounterStatus.PENDING).exclude(worker=worker).update(
        status=CounterStatus.SUPERSEDED, responded_at=now, updated_at=now
    )

    if offer is not None:
        offer.status = OfferStatus.ACCEPTED
        offer.responded_at = now
        # response_note is included because the worker's accept path sets it on
        # the instance just before calling in.
        offer.save(
            update_fields=["status", "response_note", "responded_at", "updated_at"]
        )
    return job


def _post_counter_to_thread(counter, *, job, worker, sender):
    """Put a worker's proposed terms into that pair's message thread.

    Naming a different price is the one kind of application that arrives with
    figures attached, and until now it landed only as a row in the applicant
    list — somewhere the client goes when they are already thinking about this
    job, not somewhere that tells them anything happened. The thread is what
    drives the unread badge in the header, so this is what makes a counter
    reach a client who is not currently looking.

    The same shape as the expiry notice in ``jobs.services``, and for the same
    reason given there: this app has no notification system, and a message in a
    thread both sides already read beats inventing one for a sentence. The
    worker is the sender, because they are — which also means the client can
    answer the terms by replying to them.

    ``get_or_create`` because a public gig has no thread until somebody speaks.
    Creating one here is safe: naming a price is exactly what earns this pair a
    channel under ``can_converse``.
    """
    from messaging.models import Conversation, Message

    # Every field on a counter is nullable, and a null means "unchanged" — not
    # "empty". A worker who accepts the day and the hours and asks only for
    # more money leaves the date null, and formatting that null crashed the
    # whole submission: the counter was written, the thread message raised, and
    # the reader saw a 500 for an offer that had in fact gone through.
    #
    # So each term falls back to the job's own, which is what the null says.
    # The message then describes the arrangement being proposed rather than
    # the half of it that was typed.
    #
    # Django's own formatters, so a figure in a message reads the way the same
    # figure reads on the page it came from. date_format's "D j M" is also the
    # portable choice — strftime's unpadded "%-d" raises on Windows.
    when = date_format(counter.gig_date or job.gig_date, "D j M")
    hours = floatformat(
        counter.gig_hours if counter.gig_hours is not None else job.gig_hours, "-2"
    )
    amount = counter.fixed_pay if counter.fixed_pay is not None else job.fixed_pay
    pay = f"{rules.CURRENCY_SYMBOL}{amount:,.2f}"

    # use_escrow is nullable: a counter that only moves the money says nothing
    # about how it is handled, and inventing an answer here would put words in
    # somebody's mouth about the one term that decides whose money is held.
    if counter.use_escrow is None:
        settle = ""
    elif counter.use_escrow:
        settle = ", held in escrow until the day is signed off"
    else:
        settle = ", settled directly rather than through escrow"

    body = (
        f"Applied at different terms: {pay} for the day, {hours} hours, "
        f"{when}{settle}."
    )
    if counter.note:
        body = f"{body}\n\n{counter.note}"

    conversation, _created = Conversation.objects.get_or_create(job=job, worker=worker)
    message = Message.objects.create(
        conversation=conversation, sender=sender, body=body
    )
    conversation.last_message_at = message.created_at
    conversation.save(update_fields=["last_message_at", "updated_at"])
    return message


@login_required
def counter_create(request, pk: int, worker_pk: int | None = None):
    """Put revised terms on the table. One view, both sides, any open gig.

    ``pk`` is the job. A worker is negotiating for themselves, so they need no
    second identifier; a client is answering one particular person, so they
    name them. Two URLs into one view rather than two views, because the work
    either side does here is identical.
    """
    job = get_object_or_404(
        Job.objects.select_related("client__user", "trade"), pk=pk
    )
    if not job.is_visible_to(request.user):
        raise Http404("No job matches the given query.")

    worker = (
        get_object_or_404(WorkerProfile.objects.select_related("user"), pk=worker_pk)
        if worker_pk is not None
        else _worker(request)
    )
    party = _party(request, job, worker)
    if party is None or worker is None:
        raise Http404("No job matches the given query.")

    if not job.is_negotiable:
        messages.error(
            request,
            "Only an open dated gig has terms to negotiate."
            if job.is_gig
            else "A standing position has a rate range rather than one price — "
            "message them about it instead.",
        )
        return redirect("jobs:detail", pk=job.pk)
    if party == Party.WORKER and not job.can_negotiate(worker):
        raise Http404("No job matches the given query.")

    # Turn-taking within the pair, enforced here as well as in the template.
    # The database constraint stops two live proposals existing; this is the
    # readable version of the same rule, with a sentence the user can act on.
    if not _may_propose(job, worker, party):
        messages.info(request, "You're waiting on the other side to answer that one.")
        return redirect("jobs:detail", pk=job.pk)

    terms = _effective_terms(job, worker)
    if request.method == "POST":
        form = CounterForm(request.POST, terms=terms)
        if form.is_valid():
            # The negotiable check at the top read a state from before this
            # form was filled in. Somebody confirmed in that window leaves a
            # PENDING counter on a job that is taken — terms nobody will ever
            # answer, shown to both sides as a live negotiation. Same narrowing
            # as applying and offering, and the same reason it is only a
            # narrowing: a counter has no prior status of its own to claim.
            if not Job.objects.filter(pk=job.pk, state=JobState.POSTED).exists():
                messages.error(
                    request,
                    "That job was answered while you were writing — nothing "
                    "has been sent.",
                )
                return redirect("jobs:detail", pk=job.pk)
            now = timezone.now()
            with transaction.atomic():
                # Supersede rather than delete: the sequence of numbers is the
                # negotiation, and either side should be able to see how the
                # figure they are being asked to accept was arrived at.
                job.counters.filter(
                    worker=worker, status=CounterStatus.PENDING
                ).update(
                    status=CounterStatus.SUPERSEDED, responded_at=now, updated_at=now
                )
                counter = form.save(commit=False)
                counter.job = job
                counter.worker = worker
                counter.proposed_by = party
                counter.save()

                # Naming your price on a public gig *is* putting yourself
                # forward, so it lands in the client's applicant list like any
                # other application. Making somebody apply first at a price
                # they have already said no to would be a step for its own sake.
                if party == Party.WORKER and not job.is_private:
                    Application.objects.update_or_create(
                        job=job,
                        worker=worker,
                        defaults={"status": ApplicationStatus.APPLIED},
                    )

                # And as a message, so it reaches the client rather than
                # waiting to be found. Every worker-side counter, private
                # offer included: a thread is where a price is discussed, and
                # having the message appear for some of them and not others
                # would be a hole with no rule behind it.
                if party == Party.WORKER:
                    _post_counter_to_thread(
                        counter, job=job, worker=worker, sender=request.user
                    )

            other = job.client.user if party == Party.WORKER else worker.user
            messages.success(request, f"Sent. {other} has your terms to answer.")
            return redirect("jobs:detail", pk=job.pk)
    else:
        form = CounterForm(terms=terms)

    return render(
        request,
        "jobs/counter_form.html",
        {
            "form": form,
            "job": job,
            "worker": worker,
            "terms": terms,
            "party": party,
            "opening": job.live_counter_from(worker) is None,
        },
    )


@login_required
@require_POST
def counter_respond(request, pk: int):
    """Say yes or no to the terms the other side put up."""
    counter = get_object_or_404(
        Counter.objects.select_related("job__client__user", "worker__user"), pk=pk
    )
    job, worker = counter.job, counter.worker
    party = _party(request, job, worker)

    if party is None or party != counter.answered_by:
        raise Http404("No job matches the given query.")
    if not counter.is_pending or not job.is_open:
        messages.info(request, "That's already been answered.")
        return redirect("jobs:detail", pk=job.pk)

    now = timezone.now()
    offer = job.offers.filter(worker=worker, status=OfferStatus.PENDING).first()

    if request.POST.get("answer") != "accept":
        # Turning down a price is not turning down the person. The application
        # stands at the posted terms and a direct offer stays open at its
        # original ones — either side still has a plain "no" elsewhere if they
        # want one, and conflating the two would end deals over a number.
        if not claim(
            Counter,
            counter.pk,
            field="status",
            expect=CounterStatus.PENDING,
            to=CounterStatus.DECLINED,
            responded_at=now,
        ):
            messages.info(request, "Those terms have already been answered.")
            return redirect("jobs:detail", pk=job.pk)
        messages.success(
            request,
            "Turned those terms down. The job's original terms still stand.",
        )
        return redirect("jobs:detail", pk=job.pk)

    # Agreed terms cover the booking, the same as an accepted offer and the
    # same as a confirmed application. A counter is answered once because the
    # reader was only ever shown one to answer.
    #
    # Only the first day carries the counter and the offer rows — those are the
    # ones being responded to. The remaining days are sealed on the terms the
    # counter just wrote across them, which _seal already spreads for the
    # payment method.
    days = _booking_days(job)

    # Accepting a counter seals the booking too, so the same rule applies. The
    # wording follows who is pressing it: the worker is told about their own
    # diary, the client about the worker's.
    clash = clashing_dates(worker, days)
    if clash:
        if party == Party.WORKER:
            messages.error(
                request,
                _("You're already booked on %(days)s, so this one can't be "
                  "accepted as it stands. Nothing has been changed.")
                % {"days": describe_dates(clash)},
            )
        else:
            messages.error(
                request,
                _("%(who)s is already booked on %(days)s. Nothing has been changed.")
                % {"who": worker.user, "days": describe_dates(clash)},
            )
        return redirect("jobs:detail", pk=job.pk)

    actor = Actor.WORKER if party == Party.WORKER else Actor.CLIENT

    all_sealed = []
    with transaction.atomic():
        for index, day in enumerate(days):
            result = _seal(
                day.pk,
                worker,
                counter if index == 0 else None,
                now,
                offer=offer if index == 0 else None,
                actor=actor,
            )
            if result is not None:
                all_sealed.append(result)

    sealed = all_sealed[0] if all_sealed else None
    if sealed is None:
        messages.error(request, "That job isn't available any more.")
        return redirect("jobs:mine")

    messages.success(
        request,
        f"Agreed at {sealed.pay_display}. "
        + (
            "The client funds escrow next."
            if party == Party.WORKER
            else f"{worker.user} has the job."
        ),
    )
    return redirect("jobs:detail", pk=sealed.pk)


@login_required
@require_POST
def offer_withdraw(request, pk: int):
    """The client pulling an offer back before it is answered."""
    offer = get_object_or_404(
        Offer.objects.select_related("job", "worker__user"),
        pk=pk,
        job__client=_client(request),
    )
    if not offer.is_pending:
        messages.info(request, "That offer has already been answered.")
        return redirect("jobs:detail", pk=offer.job_id)

    # The is_pending check above reads a status the worker can change while
    # the client is deciding. Claimed, so an accept landing in between wins and
    # this becomes a message rather than a withdrawal written over a yes.
    if not claim(
        Offer,
        offer.pk,
        field="status",
        expect=OfferStatus.PENDING,
        to=OfferStatus.WITHDRAWN,
        responded_at=timezone.now(),
    ):
        messages.info(request, "They answered that offer first — it stands.")
        return redirect("jobs:detail", pk=offer.job_id)

    messages.success(request, f"Offer to {offer.worker.user} withdrawn.")
    return redirect("jobs:detail", pk=offer.job_id)


@login_required
@require_POST
def offer_publish(request, pk: int):
    """Turn a turned-down offer into an ordinary public post.

    Saves the client retyping a gig that is already written. Only once nobody
    is holding an answer to it, or the post would appear on the board while a
    worker still believes it is theirs to accept.
    """
    job = get_object_or_404(Job, pk=pk, client=_client(request))
    if not job.is_private:
        messages.info(request, "That post is already on the board.")
        return redirect("jobs:detail", pk=job.pk)
    if not job.is_open:
        messages.error(request, "That job isn't open any more.")
        return redirect("jobs:detail", pk=job.pk)
    if job.pending_offer is not None:
        messages.error(
            request,
            "Withdraw the outstanding offer first — otherwise it would go up "
            "on the board while somebody still thinks it is theirs.",
        )
        return redirect("jobs:detail", pk=job.pk)

    # Conditional on both of the things checked above still being true. The
    # three guards judge a row read before this request did anything, and the
    # one that matters can change underneath: a worker accepting in that window
    # would leave an accepted job published to the board, advertised to
    # everyone as available.
    published = Job.objects.filter(
        pk=job.pk, state=JobState.POSTED, is_private=True
    ).update(is_private=False, updated_at=timezone.now())
    if not published:
        messages.error(
            request,
            "That job was answered while you were publishing it — it has not "
            "gone on the board.",
        )
        return redirect("jobs:detail", pk=job.pk)

    messages.success(request, "Posted to the board. Anyone can apply now.")
    return redirect("jobs:detail", pk=job.pk)


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
