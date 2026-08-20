"""A client approaching one named worker, and the answer to it.

The mirror of applying. The job is real from the moment it is sent — same
table, same escrow, same lifecycle — and flagged private so it never reaches
the board.
"""

from uuid import uuid4
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from accounts.models import AvailabilityStatus, WorkerProfile
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

from .common import _booking_days, _client, _seal, _worker




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
