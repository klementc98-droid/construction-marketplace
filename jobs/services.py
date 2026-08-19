"""Gig expiry: retiring dated posts whose day has been and gone.

A gig is one dated shift. Once that date is behind us and nobody is committed
to it, the post is not merely stale — it is unfillable, and leaving it on the
board wastes the time of every worker who opens it.

**Only gigs.** A standing position has no date; it stays open until the client
fills or cancels it. Nothing here touches one.

**Only before the money is in.** The two states that expire are:

``POSTED``
    Nobody was ever booked.

``ACCEPTED``
    A worker was confirmed but the client never funded escrow. The state
    machine has permitted this route from the start ("Never funded before gig
    date"), and its own note explains why: an unfunded acceptance is not yet a
    promise worth enforcing. Left alone, these sit forever — past their date,
    off the public board because they are no longer POSTED, and invisible to
    every cleanup.

From ``ESCROW_HELD`` onward the client's money is committed and the normal
lifecycle owns the job, whatever the original date says. A worker who checked
in a day late still gets paid. There is deliberately no route from those
states to EXPIRED, and this module does not invent one — it asks the state
machine, which refuses.

Driven by ``manage.py expire_stale_gigs``, on a schedule. The state is written
to the row rather than computed at render time, so "is this expired?" gives
the same answer to the board, the apply view, an admin and a query — see the
note in core.state_machine about job and payment state being one machine.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from config import business_rules as rules
from core.state_machine import (
    Actor,
    IllegalTransition,
    JobState,
    assert_transition,
    claim,
)

from .models import (
    Application,
    ApplicationStatus,
    Job,
    JobType,
    Review,
    ReviewDirection,
)

#: The states a gig can expire out of. Both are "no money committed yet".
EXPIRABLE_STATES = (JobState.POSTED, JobState.ACCEPTED)


def _notify_applicants(job: Job) -> int:
    """Tell anyone still waiting on an answer that the gig has expired.

    Posted into the job's own thread rather than raised as a notification of
    its own: there is no notification system in this app, and inventing one
    for a single message would be a lot of machinery for a sentence. The
    thread is somewhere they already look, it drives the unread badge that is
    already in the header, and — unlike an email — they can reply to it.

    ``get_or_create`` because a thread only exists if somebody spoke first;
    plenty of applications never got one. Creating it here is safe: applying
    is exactly what earns this pair a channel under ``can_converse``.

    The client is the sender. The platform has no user row to speak as, and
    attributing it to the worker themselves would be nonsense. Returns how
    many people were told.
    """
    from messaging.models import Conversation, Message

    pending = (
        Application.objects.filter(job=job, status=ApplicationStatus.APPLIED)
        .select_related("worker")
    )

    # Django's formatter, not strftime: "D j M" is the same format string the
    # templates use for a gig date, so the message reads like the rest of the
    # app. It is also the portable choice — strftime's unpadded "%-d" is a
    # glibc extension and raises ValueError on Windows.
    when = date_format(job.gig_date, "D j M")
    body = (
        f"This gig expired without being filled — its date ({when}) has "
        f"passed. Nothing was charged and nothing is owed. The post stays on "
        f"record, and the client can repost it if the work is still going ahead."
    )

    told = 0
    for application in pending:
        conversation, _ = Conversation.objects.get_or_create(
            job=job, worker=application.worker
        )
        message = Message.objects.create(
            conversation=conversation, sender=job.client.user, body=body
        )
        conversation.last_message_at = message.created_at
        conversation.save(update_fields=["last_message_at", "updated_at"])
        told += 1
    return told


@transaction.atomic
def expire(job: Job) -> Job:
    """Move one gig to EXPIRED and tell anyone left waiting.

    Raises :class:`core.state_machine.IllegalTransition` if the job is not in
    a state that may expire — the caller decides whether that is a bug or just
    a row that moved on since the queryset was built.
    """
    locked = Job.objects.get(pk=job.pk)
    assert_transition(locked.state, JobState.EXPIRED, Actor.SYSTEM)

    # Claimed before anyone is told. Two overlapping runs of the cron — a slow
    # one still going when the next fires — would otherwise both walk the same
    # applicant list, and being told twice that a gig expired reads as the app
    # being broken rather than as the gig being over.
    #
    # IllegalTransition rather than a quiet return: this runs unattended, and a
    # caller that cannot tell "already expired" from "expired by me" has no way
    # to report what it did. expire_stale_gigs already catches it per row.
    if not claim(Job, job.pk, expect=locked.state, to=JobState.EXPIRED):
        raise IllegalTransition("This gig was already retired by another run.")

    _notify_applicants(locked)
    locked.state = JobState.EXPIRED
    return locked


def due_for_expiry(*, today=None):
    """Gigs whose date has fully passed and which nobody has committed to.

    ``gig_date < today`` is the whole of "end of day has passed": a DateField
    holds a calendar day, so the day is over precisely when today's date has
    moved past it. ``timezone.localdate()`` reads settings.TIME_ZONE, which is
    what makes this the market's local midnight rather than UTC's — the same
    reasoning as the note on Region in core.models.
    """
    today = today or timezone.localdate()
    return (
        Job.objects.filter(
            job_type=JobType.GIG,
            gig_date__lt=today,
            state__in=EXPIRABLE_STATES,
        )
        .select_related("client__user")
        .order_by("gig_date", "pk")
    )


def expire_stale_gigs(*, today=None) -> list[Job]:
    """Expire every gig that is due. Driven by the cron command.

    One row failing does not abandon the batch — same reasoning as
    ``worklog.services.settle_due``. A job that changed state between building
    the queryset and claiming the row is the ordinary race, not an error: the
    claim simply refuses and we move on.
    """
    expired = []
    for job in due_for_expiry(today=today):
        try:
            expired.append(expire(job))
        except IllegalTransition:
            continue
    return expired


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------
#
# The trust system the whitepaper sells rests on two columns — rating_sum and
# rating_count — that until now nothing ever wrote. Profiles rendered
# average_rating, eight templates fell through to "New", and they would have
# stayed New forever. This is the code that moves them.
#
# Two rules shape the whole thing:
#
# * You may only rate a job that has been paid out. Rating before the money
#   moves would make the review a lever on the payment — "five stars and I'll
#   approve" is a conversation the timing makes impossible.
# * One review per side per job, enforced by a unique constraint rather than a
#   check in a view, because the view is not the only way rows get made.


# ---------------------------------------------------------------------------
# Double booking
# ---------------------------------------------------------------------------
#
# A worker has one of each day. Nothing used to say so: a booking was sealed a
# day at a time, and no step asked whether that day was already spoken for — so
# the same person could be confirmed for the 19th to the 25th twice over, by
# two clients who each believed they had them. The first anybody would learn of
# it is when nobody turned up.
#
# Enforced in two places on purpose. Here, before anything is written, so the
# person pressing the button is told which days clash and by whom they are
# already held; and in the database, as a partial unique index over the states
# that mean "committed", so that two clients confirming the same worker in the
# same second cannot both win. The check here is the explanation; the index is
# the guarantee.


#: The states in which a worker is spoken for. Starts at ACCEPTED rather than
#: at escrow for the same reason ``WorkerProfile.active_jobs`` does: from the
#: worker's side, having said yes is what makes them unavailable, funded or
#: not. Terminal states are absent — a finished day is history, and history is
#: allowed to contain the overlaps this now prevents.
COMMITTED_STATES = (
    JobState.ACCEPTED,
    JobState.ESCROW_HELD,
    JobState.IN_PROGRESS,
    JobState.ENDED_EARLY,
    JobState.COMPLETED,
)


def booked_days_among(worker, dates, *, ignore=()) -> list:
    """Which of these dates the worker is already committed to.

    The one question behind every "they can't take this" in the app, asked of
    dates so that it can be answered before a job exists — which is what the
    offer screens need. ``ignore`` drops job ids from the answer, for the case
    where the days being asked about are themselves the booking in question.
    """
    if worker is None:
        return []
    wanted = {day for day in dates if day}
    if not wanted:
        # A standing position has no date to collide on.
        return []
    taken = Job.objects.filter(
        assigned_worker=worker,
        gig_date__in=wanted,
        state__in=COMMITTED_STATES,
    )
    if ignore:
        taken = taken.exclude(pk__in=ignore)
    return sorted(set(taken.values_list("gig_date", flat=True)))


def clashing_dates(worker, days) -> list:
    """Which of ``days`` this worker is already committed to elsewhere.

    ``days`` is the booking about to be sealed — Job rows, not dates, because
    the days of the booking itself must not count as a clash with themselves.
    Returns the dates in order, so the caller can name them.
    """
    return booked_days_among(
        worker,
        [getattr(day, "gig_date", None) for day in days],
        ignore=[day.pk for day in days],
    )


def describe_dates(dates) -> str:
    """"19, 20 and 25 Aug" — dates as a person would say them.

    One string built here rather than joined in a template, because the last
    separator is a word and words are translated.
    """
    shown = [date_format(day, "j M") for day in dates]
    if not shown:
        # Callers ask this of every row, including the ones with nothing to
        # report, rather than guarding each call site.
        return ""
    if len(shown) == 1:
        return shown[0]
    return _("%(list)s and %(last)s") % {
        "list": ", ".join(shown[:-1]),
        "last": shown[-1],
    }


class ReviewError(RuntimeError):
    """A refusal phrased for the person who hit it."""


def _direction_for(job: Job, user) -> str:
    """Which way this person's review points, or a refusal.

    Membership is decided here rather than in the view: the same question is
    asked by the form, the view and the service, and one answer is how they
    stay consistent.
    """
    if job.client.user_id == user.pk:
        return ReviewDirection.CLIENT_ON_WORKER
    if job.assigned_worker and job.assigned_worker.user_id == user.pk:
        return ReviewDirection.WORKER_ON_CLIENT
    raise ReviewError("You weren't part of this job.")


def _booking_reviews(job: Job, direction: str):
    """Every review of this booking in that direction — usually none or one.

    The unit is the booking, not the day. A rating is written against the
    booking's first day and answers for the whole arrangement, so asking the
    day the reader happens to be on is asking the wrong row eight times out
    of nine.
    """
    found = Review.objects.filter(direction=direction)
    if job.offer_group:
        return found.filter(job__offer_group=job.offer_group)
    return found.filter(job=job)


#: When a rating becomes owed. Both terminal states where the work actually
#: happened — escrow paid out, or the two of them settling directly. It was
#: PAID_OUT alone here while the model's ``is_finished`` said both, which meant
#: the service refused ratings the button offered. One rule, one list.
RATEABLE = (JobState.PAID_OUT, JobState.CLOSED)


def can_review(job: Job, user) -> bool:
    """Is there a review this person could still leave on this booking?"""
    if job.state not in RATEABLE or job.assigned_worker_id is None:
        return False
    try:
        direction = _direction_for(job, user)
    except ReviewError:
        return False
    return not _booking_reviews(job, direction).exists()


def reviews_of(profile, *, limit: int = 20):
    """Every review written about this profile, newest first.

    The direction is derived from what kind of profile this is rather than
    passed in, for the same reason the review view derives it from who is
    asking: a caller that can choose the direction is a caller that can show a
    worker the reviews they *wrote* under the heading of reviews they received.

    Sliced rather than paginated. A profile page is a first impression, and the
    twenty most recent are the ones anybody reads; the average above them is
    what speaks for the rest.
    """
    from accounts.models import WorkerProfile

    if isinstance(profile, WorkerProfile):
        found = Review.objects.filter(
            direction=ReviewDirection.CLIENT_ON_WORKER, job__assigned_worker=profile
        )
    else:
        found = Review.objects.filter(
            direction=ReviewDirection.WORKER_ON_CLIENT, job__client=profile
        )
    # Ordering is the model's ("-created_at"), so the slice is the newest.
    return list(found.select_related("author", "job")[:limit])


def review_by(job: Job, user) -> "Review | None":
    """The review this person already left on this job, if any."""
    try:
        direction = _direction_for(job, user)
    except ReviewError:
        return None
    return _booking_reviews(job, direction).first()


@transaction.atomic
def leave_review(job: Job, author, *, rating: int, comment: str = "") -> "Review":
    """Record one side's verdict and fold it into the subject's average.

    The write and the counter update are one transaction. A review that landed
    without moving the average would be invisible on every card and profile in
    the app, which is worse than no review at all — it would look like the
    feature was working.
    """
    if job.state not in RATEABLE:
        raise ReviewError("You can rate a job once it's finished.")
    if job.assigned_worker_id is None:
        raise ReviewError("Nobody was booked on this job.")

    direction = _direction_for(job, author)
    if _booking_reviews(job, direction).exists():
        # Phrased as the booking, because that is what was rated. "You've
        # already rated this job" on a Thursday nobody has touched reads as a
        # bug rather than as the rule it is.
        raise ReviewError("You've already rated this booking.")
    if not rules.RATING_MIN <= rating <= rules.RATING_MAX:
        raise ReviewError(
            f"Ratings run from {rules.RATING_MIN} to {rules.RATING_MAX}."
        )

    review = Review.objects.create(
        job=job,
        direction=direction,
        author=author,
        rating=rating,
        comment=comment.strip(),
    )
    review.subject_profile.record_rating(rating)
    return review
