"""The vocabulary the other view modules share.

Two profile lookups, the booking helpers, the negotiation predicates the job
page needs to render, and _seal — the one function that decides who owns a
job. Everything here is used by more than one module; anything used by one
lives with it.
"""

from types import SimpleNamespace
from django.db import IntegrityError, models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from accounts.models import AvailabilityStatus, WorkerProfile
from core.state_machine import Actor, JobState, assert_transition, claim
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




def _worker(request) -> WorkerProfile | None:
    if not request.user.is_authenticated:
        return None
    return getattr(request.user, "worker_profile", None)




def _client(request):
    if not request.user.is_authenticated:
        return None
    return getattr(request.user, "client_profile", None)




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
