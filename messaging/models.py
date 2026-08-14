"""In-app messaging between a client and a worker, about a job.

Every conversation hangs off a **job**, never off a bare pair of users. Two
reasons, and both are about what happens later:

* Context. "Are you free Thursday?" means nothing in an inbox without the gig
  it refers to. Attaching the thread to the job means the date, the price and
  the trade are always one line above the message.
* Evidence. Phase 5's dispute queue and phase 6's ratings both ask "what was
  actually agreed?" A thread scoped to the job is the answer; a general DM
  history between two people is not.

Who may talk to whom is deliberately narrow — see :func:`can_converse`. An
open marketplace where anyone can message anyone is a spam surface, and the
people it costs most are the workers, who cannot afford to ignore their phone.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.models import WorkerProfile
from core.models import TimestampedModel
from jobs.models import ApplicationStatus, Job


def can_converse(job: Job, worker: WorkerProfile) -> bool:
    """Has this pair earned a channel?

    Mutual interest means one of:

    * the worker applied (and has not withdrawn), or
    * the client already picked them, or
    * the client opened the thread themselves — direct contact, which the
      spec allows for gig posts and which is how a client reaches someone who
      has not applied yet.

    The last case is why an existing Conversation row is itself sufficient:
    creating it is the client's deliberate act, and it is only ever creatable
    through :func:`start_direct`, which checks the client owns the job.
    """
    if job.assigned_worker_id == worker.pk:
        return True
    if Conversation.objects.filter(job=job, worker=worker).exists():
        return True
    return job.applications.filter(
        worker=worker,
        status__in=[ApplicationStatus.APPLIED, ApplicationStatus.SELECTED],
    ).exists()


class ConversationQuerySet(models.QuerySet):
    def for_user(self, user):
        """Threads this user is a party to — as either side."""
        return self.filter(
            models.Q(job__client__user=user) | models.Q(worker__user=user)
        )

    def with_unread_for(self, user):
        return self.annotate(
            unread_count=models.Count(
                "messages",
                filter=models.Q(messages__read_at__isnull=True)
                & ~models.Q(messages__sender=user),
            )
        )

    def with_preview(self):
        """Annotate the newest message's body and sender for the inbox row.

        An inbox that shows only "3 new" makes you open every thread to find
        out which one matters. Showing the last line is what lets someone
        triage a list of threads without opening any of them.

        Two correlated subqueries rather than a prefetch: this returns one row
        per conversation with no second query and no N+1, and both are covered
        by the (conversation, created_at) index the Message model already
        declares. ``last_sender`` comes back as a raw id — enough to decide
        whether to prefix the line with "You:", without another join for a
        name the row does not print.
        """
        newest = Message.objects.filter(conversation=models.OuterRef("pk")).order_by(
            "-created_at"
        )
        return self.annotate(
            last_body=models.Subquery(newest.values("body")[:1]),
            last_sender=models.Subquery(newest.values("sender_id")[:1]),
        )


class Conversation(TimestampedModel):
    """One thread, between one client and one worker, about one job."""

    job = models.ForeignKey(
        Job, on_delete=models.CASCADE, related_name="conversations"
    )
    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="conversations"
    )

    #: Touched on every send so the inbox can sort by real activity without
    #: reaching into the messages table for each row.
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ConversationQuerySet.as_manager()

    class Meta:
        ordering = ("-last_message_at", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=["job", "worker"], name="one_thread_per_job_per_worker"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker.user} & {self.job.client} re {self.job}"

    @property
    def client(self):
        return self.job.client

    def other_party(self, user):
        """The person on the far end, from ``user``'s point of view."""
        return self.job.client.user if self.worker.user_id == user.pk else self.worker.user

    def includes(self, user) -> bool:
        return user.pk in (self.worker.user_id, self.job.client.user_id)


class Message(TimestampedModel):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_messages"
    )
    body = models.TextField(max_length=4000)

    #: Set when the *other* party opens the thread. Null means unread. With
    #: exactly two participants this is enough; a group thread would need a
    #: per-recipient receipt instead.
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self) -> str:
        return f"{self.sender}: {self.body[:40]}"
