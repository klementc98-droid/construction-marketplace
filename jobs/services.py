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

from core.state_machine import Actor, IllegalTransition, JobState, assert_transition

from .models import Application, ApplicationStatus, Job, JobType

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
    locked = Job.objects.select_for_update().get(pk=job.pk)
    assert_transition(locked.state, JobState.EXPIRED, Actor.SYSTEM)

    _notify_applicants(locked)

    locked.state = JobState.EXPIRED
    locked.save(update_fields=["state", "updated_at"])
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
    the queryset and locking the row is the ordinary race, not an error: the
    transition simply refuses and we move on.
    """
    expired = []
    for job in due_for_expiry(today=today):
        try:
            expired.append(expire(job))
        except IllegalTransition:
            continue
    return expired
