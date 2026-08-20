"""Writing notifications, and the one rule about who does not get them.

Called from inside whatever transaction caused the event. Every function here
is cheap and silent: one insert, no network, and never an exception that could
take down the thing being announced. An email that failed to be *queued* must
not lose somebody their job application.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Kind, Notification

#: What is actually emailed. Everything else in :class:`Kind` is written and
#: translated and stays inert until it is named here.
#:
#: The short list is the decision, not the limit of what was built. An inbox
#: that fills with mail nobody asked for gets the sender filtered, and once
#: somebody has filtered you the important one does not arrive either — so the
#: two that survive are the two where an email genuinely changes what a person
#: does: work that starts tomorrow, and somebody asking them to take a job.
#:
#: Turning one back on is adding a line here. The events are already wired, so
#: nothing else has to be found and re-plumbed.
ENABLED: frozenset = frozenset(
    {
        Kind.OFFER_RECEIVED,
        Kind.TOMORROW,
    }
)


def notify(
    recipient,
    kind: str,
    *,
    job=None,
    dedupe: str | None = None,
    actor=None,
    **payload,
) -> Notification | None:
    """Queue one email. Returns the row, or None if it was not owed.

    Five reasons nothing is written, and all of them are ordinary rather than
    exceptional — hence None rather than a raise:

    * This kind is not one of the ones being emailed. See ``ENABLED``.
    * There is nobody to write to.
    * They turned email off.
    * They have no address. Sign-in is Google-only so this should not happen,
      but a blank email is not worth a crash in a background concern.
    * It is their own doing. Nobody needs telling what they just did, and this
      is the check that stops a client emailing themselves every time they
      press a button on their own job.

    ``dedupe`` collapses repeats while the email is still owed — see the
    constraint on the model. Passing the booking in it is what makes a five-day
    arrangement one email; leaving out the actor is what would wrongly make two
    applicants one email, so the callers compose the whole key themselves.
    """
    if kind not in ENABLED:
        return None
    if recipient is None or not recipient.email:
        return None
    if not recipient.email_notifications:
        return None
    if actor is not None and getattr(actor, "pk", None) == recipient.pk:
        return None

    booking = getattr(job, "offer_group", None) if job is not None else None

    row = Notification(
        recipient=recipient,
        kind=kind,
        job=job,
        booking=booking,
        dedupe_key=dedupe or "",
        payload=payload,
    )
    try:
        # Its own savepoint. A duplicate is the constraint doing its job, not a
        # failure — but an IntegrityError left unhandled marks the whole
        # surrounding transaction as broken, and the surrounding transaction is
        # somebody accepting a job.
        with transaction.atomic():
            row.save()
    except IntegrityError:
        # A second event under the same key while the first is still queued.
        # One row is the point of the key — a five-day booking is one email —
        # but the row should carry the *latest* words, and it was keeping the
        # first. Three messages before the queue drained sent the recipient the
        # oldest one while two newer ones sat unread behind it, which is the
        # opposite of what a notification is for.
        #
        # The payload only. created_at stays where it was, because the person
        # has been waiting since then and the queue is ordered by it.
        Notification.objects.filter(
            recipient=recipient, dedupe_key=dedupe, sent_at__isnull=True
        ).update(payload=payload, updated_at=timezone.now())
        return None
    return row


def audience_for(job):
    """Who should hear that this job exists.

    The only broadcast in the app, and the only notification sent to people
    with no connection to the job — so it is the only one that can annoy
    somebody who never asked for it. Four filters, each one earning its place:

    * Public posts only. A direct offer is written for one person by name and
      announcing it would both mislead everyone else and leak who is being
      offered what.
    * The trade. Somebody who listed themselves as a plumber does not want
      every roofing job in the city, and one irrelevant email is all it takes
      for the next one to go unread.
    * The region. Same reasoning, geographically.
    * Not people who said they are not working. "Not currently available" is an
      answer to this question as much as to any other.

    The client is excluded by :func:`notify` itself, which is where the "never
    tell somebody what they just did" rule lives — a client who is also a
    worker in the same trade would otherwise be told about their own post.
    """
    from accounts.models import AvailabilityStatus, WorkerProfile

    if job.is_private:
        return WorkerProfile.objects.none()

    return (
        WorkerProfile.objects.filter(trades=job.trade, region=job.region)
        .exclude(availability_status=AvailabilityStatus.UNAVAILABLE)
        .select_related("user")
    )


def booking_key(kind: str, job, *parts) -> str:
    """A dedupe key that treats a booking as one thing.

    Falls back to the job's own id when it is not part of a booking, so a
    single-day job still gets a key and still cannot double up.
    """
    scope = job.offer_group if getattr(job, "offer_group", None) else f"job{job.pk}"
    return ":".join([kind, str(scope), *(str(p) for p in parts)])
