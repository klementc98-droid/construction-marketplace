"""Email notifications: what was worth telling somebody, and whether it went.

The app has no notification system in the usual sense — no bell, no feed. What
it has is a header badge for things waiting on you, which only works while you
are looking at the page. This is the other half: the message that reaches
somebody who is on a roof and will not open the site until tonight.

**Written in the request, sent out of it.** A row goes down in the same
transaction as the thing it describes, so an email cannot exist for a job that
was rolled back, and a job cannot be committed while the email that should have
announced it is lost. Sending is then a separate pass — ``manage.py
send_notifications`` — because SMTP is a network call to somebody else's server
and no part of somebody applying for work should wait on it.

**Rendered at send time, not at write time.** The row stores what happened and
who it is for; the words are made when it goes out. Two reasons. The recipient
reads it in their own language, which is not necessarily the language of the
person whose action caused it — a Greek worker gets Greek even when an English
client pressed the button. And a wording fix reaches everything still queued
rather than only what happens next.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import TimestampedModel


class Kind(models.TextChoices):
    """What happened. One per thing a person would want telling about.

    Kept as data rather than as separate models: they differ only in wording,
    and a table per kind would be seven tables that all say "somebody did a
    thing to a job you are part of".
    """

    MESSAGE = "message", _("New message")
    #: The only one that goes to people with no connection to the job yet.
    #: Everything else here is addressed to somebody already involved, which is
    #: why this is the one with a matching rule in front of it — see
    #: ``audience_for`` in services.
    JOB_POSTED = "job_posted", _("New work in your trade")
    #: Not an event. A nudge about events that already happened and were
    #: ignored, sent by its own command rather than by anything a person did.
    REMINDER = "reminder", _("Something is waiting for you")
    #: The day before a booked day. Also not an event — the calendar reaching a
    #: date is what causes it, so it too has its own command.
    TOMORROW = "tomorrow", _("You're working tomorrow")
    OFFER_RECEIVED = "offer_received", _("You were offered a job")
    OFFER_ANSWERED = "offer_answered", _("Your offer was answered")
    APPLICATION = "application", _("Somebody applied to your job")
    SELECTED = "selected", _("You were picked for a job")
    WORK_FINISHED = "work_finished", _("The work was marked finished")
    JOB_CLOSED = "job_closed", _("A job was closed")
    ESCROW_FUNDED = "escrow_funded", _("Escrow was funded")
    PAYMENT_RELEASED = "payment_released", _("A payment was released")
    DISPUTE = "dispute", _("A dispute was raised")
    RATING = "rating", _("You were rated")


class Notification(TimestampedModel):
    """One email owed to one person.

    ``payload`` carries whatever the wording needs that is not already reachable
    from the job — an amount, a name, the first line of a message. Denormalised
    on purpose: an email describes a moment, and looking the figures up again at
    send time would describe the present instead. A price that moved between the
    counter being sent and the mail going out must not silently rewrite what the
    counter said.
    """

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)

    #: What it is about. Nullable because not everything worth an email is a
    #: job, and SET_NULL rather than CASCADE because a sent email is a record of
    #: something that happened — deleting the job does not unsend it.
    job = models.ForeignKey(
        "jobs.Job", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="notifications",
    )
    #: The booking a job belongs to, copied down at write time. Reference only;
    #: what actually collapses a booking into one email is dedupe_key.
    booking = models.UUIDField(null=True, blank=True, db_index=True)

    #: What would make a second row of this a repeat rather than a new thing.
    #: The caller composes it, because only the caller knows: two people
    #: applying to the same booking are two emails and must carry the applicant
    #: in the key, while five days of one offer are one email and must not.
    #:
    #: Blank opts out, for the events where every occurrence is its own news.
    dedupe_key = models.CharField(max_length=200, blank=True)

    payload = models.JSONField(default=dict, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            # The sending command's only query: what is still owed, oldest first.
            models.Index(fields=["sent_at", "created_at"]),
        ]
        constraints = [
            # One unsent email per person per key. A five-day booking is five
            # Job rows and every step through it writes five times; without
            # this the worker gets five identical "you were offered a job"
            # emails, which is the surest way to have somebody mute the lot.
            #
            # Enforced here rather than left to the writing code because the
            # writing code is seven call sites and will one day be eight.
            #
            # Partial, on unsent rows only, for two reasons. The same thing can
            # legitimately happen again next month, and the row from last time
            # has already gone out and must not block it. And a key is about
            # what is still owed — once it is delivered it stops being a
            # question of repeats and starts being history.
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=models.Q(sent_at__isnull=True) & ~models.Q(dedupe_key=""),
                name="one_pending_notification_per_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} -> {self.recipient}"

    @property
    def is_sent(self) -> bool:
        return self.sent_at is not None
