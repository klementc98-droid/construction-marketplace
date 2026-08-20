"""Counter-offers: revised terms, and the turn-taking around them.

Either side may put a number on the table and they alternate until one says
yes. The job keeps its posted terms throughout — a counter is a proposal
*about* the job, and only accepting writes to it.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, models, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import floatformat
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from accounts.models import AvailabilityStatus, WorkerProfile
from config import business_rules as rules
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

from .common import _booking_days, _effective_terms, _may_propose, _party, _seal, _worker




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
        form = CounterForm(request.POST, terms=terms, worker=worker)
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
        form = CounterForm(terms=terms, worker=worker)

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




def _reshape_booking(job, booking, wanted):
    """Make the booking run on ``wanted``, and return its rows, oldest first.

    Reuse before create, and create before cancel:

    * a row already on a wanted day stays exactly as it is;
    * a row on a day nobody wants is re-dated onto a wanted day that has no row
      yet — which is what makes "move it to Tuesday" one row with a new date on
      it, the same as it has always been, rather than a cancellation and a
      creation that would read as two events in both parties' lists;
    * anything still wanted is created, cloned from the job being answered;
    * anything still spare is cancelled.

    All of this is safe only because a counter lives while the job is POSTED:
    no escrow exists, no day has been worked, and no row is assigned to
    anybody. There is nothing to reconcile — see Counter's own docstring, which
    is where that guarantee is written down.

    The client is party to the change either way round. They either proposed
    these days or they are the one accepting them, so cancelling a day their
    own posting no longer covers is their action, and the state machine is
    asked as such rather than being bypassed.
    """
    by_day = {row.gig_date: row for row in booking}
    keep = [by_day[d] for d in wanted if d in by_day]
    spare = [row for row in booking if row.gig_date not in set(wanted)]
    needed = [d for d in wanted if d not in by_day]

    rows = list(keep)

    # Re-date what can be re-dated.
    while spare and needed:
        row, day = spare.pop(0), needed.pop(0)
        row.gig_date = day
        row.full_clean()
        row.save(update_fields=["gig_date", "updated_at"])
        rows.append(row)

    # Create what is left wanting.
    for day in needed:
        rows.append(_clone_day(job, day))

    # Retire what is left over. Claimed rather than assigned, because another
    # request can move a posted row while this one is deciding — and losing
    # that race must leave the row alone rather than overwrite whatever it
    # became.
    for row in spare:
        assert_transition(row.state, JobState.CANCELLED, Actor.CLIENT)
        claim(Job, row.pk, expect=row.state, to=JobState.CANCELLED)

    rows.sort(key=lambda row: row.gig_date)
    return rows


def _clone_day(job, day):
    """A new day of an existing booking, posted and ready to be sealed.

    Everything about the work is copied from the job being answered; only the
    date differs. ``offer_group`` is set from the job's own group, or from the
    job itself when it had none — a single-day gig that gains a second day
    becomes a booking of two, and both rows have to agree on which.
    """
    group = job.offer_group or job.pk
    if not job.offer_group:
        Job.objects.filter(pk=job.pk).update(offer_group=group)
        job.offer_group = group

    clone = Job.objects.get(pk=job.pk)
    clone.pk = None
    clone._state.adding = True
    clone.gig_date = day
    clone.offer_group = group
    clone.state = JobState.POSTED
    clone.assigned_worker = None
    clone.filled_at = None
    clone.full_clean()
    clone.save()
    return clone


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

    # Which days are being agreed to.
    #
    # A counter names the days it wants, and that is the whole point of it: an
    # offer can be a week, and the commonest honest answer to a week is "all of
    # it except Wednesday". So the days the counter proposes become the days
    # the booking runs — a counter is a proposal about this job, and accepting
    # it agrees those terms for this job, days included.
    #
    # A counter that says nothing about days leaves the booking exactly as it
    # was, the same as one that says nothing about the price.
    booking = _booking_days(job)
    wanted = counter.proposed_days or [d.gig_date for d in booking]

    # Accepting a counter seals the booking too, so the same rule applies. The
    # wording follows who is pressing it: the worker is told about their own
    # diary, the client about the worker's.
    # Against the days being agreed, which is not the same list as the rows the
    # booking currently holds — a counter can name a day nobody has posted yet.
    clash = sorted(set(wanted) & set(worker.booked_dates))
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
        days = _reshape_booking(job, booking, wanted)

        for index, day in enumerate(days):
            # The counter and the offer rows hang off the job that was being
            # answered, wherever it ended up in the reshaped list. Writing the
            # agreed price onto some other day of the booking would put it on a
            # row nobody had proposed anything about.
            answered = day.pk == job.pk
            result = _seal(
                day.pk,
                worker,
                counter if answered else None,
                now,
                offer=offer if answered else None,
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
