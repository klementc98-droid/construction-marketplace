"""Phase 2 views: posting, browsing, searching, applying, and selecting.

Role is not a field on the user (see ``accounts.models``), so these views ask
"does this person have the profile this action needs?" rather than "what role
are they?". The same account can post a gig in the morning and apply to one in
the afternoon.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import AvailabilityStatus, WorkerProfile
from assistant.conversation import take_handoff
from core.models import Region
from core.state_machine import Actor, JobState, assert_transition

from .forms import (
    JOB_FORMS,
    ApplicationForm,
    CounterForm,
    JobFilterForm,
    OfferForm,
    OfferResponseForm,
    WorkerFilterForm,
)
from .models import (
    Application,
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
    return render(
        request, "jobs/job_list.html", {"form": form, "jobs": jobs, "total": jobs.count()}
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
        },
    )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


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
            job = form.save(commit=False)
            job.client = client
            job.job_type = job_type
            if job.region_id is None:
                job.region = Region.objects.filter(is_active=True).first()
            job.full_clean()
            job.save()
            messages.success(request, "Posted. Workers can see it now.")
            return redirect("jobs:detail", pk=job.pk)
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
            job = form.save(commit=False)
            job.full_clean()
            job.save()
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
    job.state = JobState.CANCELLED
    job.save(update_fields=["state", "updated_at"])
    messages.success(request, "Cancelled.")
    return redirect("jobs:mine")


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


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
        return redirect("jobs:detail", pk=job.pk)

    existing = job.application_from(worker)
    if request.method == "POST":
        form = ApplicationForm(request.POST, instance=existing)
        if form.is_valid():
            application = form.save(commit=False)
            application.job = job
            application.worker = worker
            # Re-applying after withdrawing puts you back in the running
            # rather than creating a second row — see the unique constraint.
            application.status = ApplicationStatus.APPLIED
            application.save()
            messages.success(request, "Application sent.")
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
    application.status = ApplicationStatus.WITHDRAWN
    application.responded_at = timezone.now()
    application.save(update_fields=["status", "responded_at", "updated_at"])
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

    with transaction.atomic():
        job = _seal(job.pk, application.worker, None, timezone.now(), actor=Actor.CLIENT)

    if job is None:
        messages.error(request, "Someone has already been selected for this job.")
        return redirect("jobs:applicants", pk=application.job_id)

    if job.is_gig:
        messages.success(
            request,
            f"{application.worker.user} has the job. Funding escrow comes next — "
            "that flow arrives in phase 4.",
        )
    else:
        messages.success(request, f"{application.worker.user} has the job.")
    return redirect("jobs:detail", pk=job.pk)


# ---------------------------------------------------------------------------
# Direct offers
# ---------------------------------------------------------------------------
# The mirror of applying: a client writes a gig for one named worker instead of
# advertising it. The job is real from the moment it is sent — same table, same
# escrow, same lifecycle — it is simply flagged private so it never reaches the
# board. Accepting is then a state transition on a job that already exists,
# rather than a conversion step that copies six fields and could get one wrong.


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
        {"form": form, "worker": worker},
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
        offer.status = OfferStatus.DECLINED
        offer.response_note = note
        offer.responded_at = now
        offer.save(update_fields=["status", "response_note", "responded_at", "updated_at"])
        messages.success(request, "Declined. The client has been told.")
        return redirect("jobs:mine")

    if not job.is_open:
        # Cancelled or filled while they were reading it.
        offer.status = OfferStatus.WITHDRAWN
        offer.responded_at = now
        offer.save(update_fields=["status", "responded_at", "updated_at"])
        messages.error(request, "That offer is no longer available.")
        return redirect("jobs:mine")

    # Accepting the offer as it stands also accepts whatever the client last
    # put on the table, so the terms the worker is looking at are the terms
    # that get written. Shared with the counter path — see _seal.
    with transaction.atomic():
        offer.response_note = note
        job = _seal(
            job.pk, worker, offer.live_counter, now, offer=offer, actor=Actor.WORKER
        )

    if job is None:
        messages.error(request, "That offer is no longer available.")
        return redirect("jobs:mine")

    messages.success(
        request,
        "Accepted — the job is yours. The client funds escrow next; "
        "you'll see it on the job page when the money is held.",
    )
    return redirect("jobs:detail", pk=job.pk)


# ---------------------------------------------------------------------------
# Counter-offers
# ---------------------------------------------------------------------------
# Either side may put revised terms on the table, and they alternate until one
# of them says yes. The job keeps the terms it was posted with throughout — a
# counter is a proposal *about* the job, and only accepting writes to it. That
# ordering is the whole safety story: the job's fixed_pay is what the client's
# card is charged, so it must never move while nobody has agreed to it.


def _effective_terms(job, worker):
    """The terms as they stand for this pair: their live counter over the job.

    What a new counter is measured against, and what "accept" would agree to.
    """
    counter = job.live_counter_from(worker)
    return SimpleNamespace(
        fixed_pay=(counter.fixed_pay if counter and counter.fixed_pay is not None else job.fixed_pay),
        gig_hours=(counter.gig_hours if counter and counter.gig_hours is not None else job.gig_hours),
        gig_date=(counter.gig_date if counter and counter.gig_date is not None else job.gig_date),
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

    The job is re-read under ``select_for_update`` inside the caller's
    transaction. Between rendering the button and this running, the gig may
    have been cancelled or taken, and the state machine has to judge the row as
    it is now rather than as it was on the page.
    """
    job = Job.objects.select_for_update().get(pk=job_id)
    if not job.is_open:
        return None

    assert_transition(job.state, JobState.ACCEPTED, actor)

    fields = ["state", "assigned_worker", "filled_at", "updated_at"]
    if counter is not None:
        fields += counter.apply_to(job)
        counter.status = CounterStatus.ACCEPTED
        counter.responded_at = now
        counter.save(update_fields=["status", "responded_at", "updated_at"])

    job.state = JobState.ACCEPTED
    job.assigned_worker = worker
    job.filled_at = now
    job.save(update_fields=list(dict.fromkeys(fields)))

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
        counter.status = CounterStatus.DECLINED
        counter.responded_at = now
        counter.save(update_fields=["status", "responded_at", "updated_at"])
        messages.success(
            request,
            "Turned those terms down. The job's original terms still stand.",
        )
        return redirect("jobs:detail", pk=job.pk)

    with transaction.atomic():
        sealed = _seal(
            job.pk,
            worker,
            counter,
            now,
            offer=offer,
            actor=Actor.WORKER if party == Party.WORKER else Actor.CLIENT,
        )

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

    offer.status = OfferStatus.WITHDRAWN
    offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "responded_at", "updated_at"])
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

    job.is_private = False
    job.save(update_fields=["is_private", "updated_at"])
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
            "posted": (
                Job.objects.filter(client=client)
                .select_related("trade", "assigned_worker__user")
                .with_applicant_counts()
                if client
                else None
            ),
            "applications": (
                Application.objects.filter(worker=worker).select_related(
                    "job__trade", "job__client__user"
                )
                if worker
                else None
            ),
            # Top of the page for the worker: somebody is waiting on an answer,
            # and a gig offered for Thursday is worth nothing if it is read on
            # Friday. Only live ones — an offer for a job that has since been
            # cancelled is not a decision anyone still has to make.
            "offers": (
                Offer.objects.filter(worker=worker, status=OfferStatus.PENDING)
                .filter(job__state=JobState.POSTED)
                .select_related("job__trade", "job__client__user")
                if worker
                else None
            ),
            # And for the client: what is still out with somebody.
            "offers_sent": (
                Offer.objects.filter(
                    job__client=client, status=OfferStatus.PENDING
                ).select_related("job__trade", "worker__user")
                if client
                else None
            ),
        },
    )
