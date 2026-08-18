"""Writing notifications, and the one rule about who does not get them.

Called from inside whatever transaction caused the event. Every function here
is cheap and silent: one insert, no network, and never an exception that could
take down the thing being announced. An email that failed to be *queued* must
not lose somebody their job application.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from .models import Kind, Notification


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

    Four reasons nothing is written, and all of them are ordinary rather than
    exceptional — hence None rather than a raise:

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
