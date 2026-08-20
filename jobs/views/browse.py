"""The board, the worker directory, and one job's page.

Everything here is readable signed out — the shop window. What a given reader
may see of a *particular* job is decided by Job.is_visible_to, not here.
"""

from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from accounts.models import AvailabilityStatus, WorkerProfile
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

from .common import _client, _effective_terms, _may_propose, _turn, _worker




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
