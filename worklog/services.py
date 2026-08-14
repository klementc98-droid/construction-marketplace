"""The on-site lifecycle: arrive, finish, settle, or argue.

Every state change goes through :func:`core.state_machine.assert_transition`
before anything is written, and every money movement goes through
:mod:`payments.services`. Nothing here captures a payment itself — there is one
function that moves money, and this module calls it.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from config import business_rules as rules
from core.state_machine import Actor, JobState, assert_transition
from jobs.models import Job, JobType
from payments.models import EscrowPayment, EscrowStatus
from payments.services import EscrowError, release

from .models import CheckIn, Completion, Dispute, DisputeStatus, EndedBy, payable_for


class WorkflowError(RuntimeError):
    """A refusal phrased for the person who hit it."""


def _escrow(job: Job) -> EscrowPayment | None:
    return EscrowPayment.objects.filter(job=job).first()


def _notify(job: Job, sender, body: str) -> None:
    """Drop a line into the job's thread so the other side sees it.

    The spec wants an early finish to notify the other party immediately. In
    app, that means the thread they already have about this job — a
    notification with nowhere to reply is not much use when the disagreement
    is about how many hours got worked.
    """
    from messaging.models import Conversation, Message

    if job.assigned_worker_id is None:
        return
    conversation, _ = Conversation.objects.get_or_create(
        job=job, worker=job.assigned_worker
    )
    message = Message.objects.create(
        conversation=conversation, sender=sender, body=body
    )
    conversation.last_message_at = message.created_at
    conversation.save(update_fields=["last_message_at", "updated_at"])


# ---------------------------------------------------------------------------
# Arriving
# ---------------------------------------------------------------------------


@transaction.atomic
def check_in(
    job: Job,
    worker,
    *,
    latitude=None,
    longitude=None,
    accuracy_m: int | None = None,
) -> CheckIn:
    """Worker taps "arrived". Starts the job; GPS is recorded, never enforced."""
    if job.assigned_worker_id != worker.pk:
        raise WorkflowError("This isn't your job.")
    if hasattr(job, "check_in"):
        return job.check_in

    locked = Job.objects.select_for_update().get(pk=job.pk)

    # The row-level half of the rule the transition table cannot state. Once
    # ACCEPTED -> IN_PROGRESS became legal — it has to be, or a deal settled
    # directly could never start — the table stopped being able to say "not
    # until the money is in". That guarantee only ever applied to a job with
    # escrow on it, and it still holds: nobody travels to a site on the promise
    # of a hold that was never funded.
    if locked.is_escrowed and locked.state == JobState.ACCEPTED:
        raise WorkflowError(
            "The client hasn't funded this gig yet — you'll be able to check "
            "in once the money is held."
        )

    assert_transition(locked.state, JobState.IN_PROGRESS, Actor.WORKER)

    record = CheckIn(
        job=job,
        worker=worker,
        arrived_at=timezone.now(),
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
    )
    record.evaluate_location()
    record.save()

    locked.state = JobState.IN_PROGRESS
    locked.save(update_fields=["state", "updated_at"])
    return record


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------


def _create_completion(
    job: Job,
    *,
    hours: Decimal,
    ended_early: bool,
    ended_by: str = "",
    note: str = "",
) -> Completion:
    window = (
        rules.EARLY_END_DISPUTE_WINDOW if ended_early else rules.CLIENT_APPROVAL_WINDOW
    )
    return Completion.objects.create(
        job=job,
        finished_at=timezone.now(),
        hours_worked=hours,
        ended_early=ended_early,
        ended_early_by=ended_by,
        early_end_note=note,
        payable_amount=payable_for(job, hours),
        settles_at=timezone.now() + window,
    )


@transaction.atomic
def complete(job: Job, worker) -> Completion:
    """Worker marks a normal full day done. The approval window starts."""
    if job.assigned_worker_id != worker.pk:
        raise WorkflowError("This isn't your job.")
    if hasattr(job, "completion"):
        return job.completion

    locked = Job.objects.select_for_update().get(pk=job.pk)
    assert_transition(locked.state, JobState.COMPLETED, Actor.WORKER)

    completion = _create_completion(
        job, hours=Decimal(job.gig_hours or 0), ended_early=False
    )
    locked.state = JobState.COMPLETED
    locked.save(update_fields=["state", "updated_at"])

    _notify(
        job,
        worker.user,
        f"Marked the job complete. The client has "
        f"{int(rules.CLIENT_APPROVAL_WINDOW.total_seconds() // 3600)} hours to "
        "approve, after which payment releases automatically.",
    )
    return completion


@transaction.atomic
def mark_work_finished(job: Job, worker) -> Job:
    """The worker says the day is done, on a gig with no escrow.

    Straight from ACCEPTED, because the no-escrow route has no check-in: a
    check-in exists to start the clock on a hold, and there is no hold. No
    Completion row either — that record is a payout calculation, and there is
    no payout to calculate.

    Nothing is settled by this. It puts the job in front of the client and
    waits, and it waits indefinitely: with no money held there is no window to
    lapse and nothing for a timer to release.
    """
    if job.assigned_worker_id != worker.pk:
        raise WorkflowError("This isn't your job.")
    if job.is_escrowed:
        raise WorkflowError(
            "This job runs through escrow — check in on site, then mark it done."
        )

    locked = Job.objects.select_for_update().get(pk=job.pk)
    assert_transition(locked.state, JobState.COMPLETED, Actor.WORKER)
    locked.state = JobState.COMPLETED
    locked.save(update_fields=["state", "updated_at"])

    _notify(
        job,
        worker.user,
        "Marked the work finished. Confirm it when you agree and the job is "
        "closed — payment is between us, so nothing releases on its own.",
    )
    return locked


@transaction.atomic
def confirm_closed(job: Job, user) -> Job:
    """The client agrees the work happened. Closes a no-escrow gig.

    The second half of the only agreement this route has. CLOSED rather than
    PAID_OUT: we did not pay anybody, and a state claiming we did would put a
    payment in the record that never happened.

    Counters move here exactly as they do on a release, so a job settled
    directly still counts on both track records. Not counting it would make
    the pair who trust each other look like the pair who have never worked.
    """
    if job.client.user_id != user.pk:
        raise WorkflowError("Only the client who posted this can confirm it.")
    if job.is_escrowed:
        raise WorkflowError("This job has money held — approve the release instead.")

    locked = Job.objects.select_for_update().get(pk=job.pk)
    assert_transition(locked.state, JobState.CLOSED, Actor.CLIENT)
    locked.state = JobState.CLOSED
    locked.save(update_fields=["state", "updated_at"])

    worker = job.assigned_worker
    if worker is not None:
        type(worker).objects.filter(pk=worker.pk).update(
            jobs_completed=models.F("jobs_completed") + 1
        )
    client = job.client
    type(client).objects.filter(pk=client.pk).update(
        jobs_completed=models.F("jobs_completed") + 1
    )

    _notify(
        job,
        user,
        "Confirmed — the job is closed. You can both leave a rating now.",
    )
    return locked


@transaction.atomic
def flag_early_end(job: Job, user, *, hours_worked: Decimal, note: str = "") -> Completion:
    """Either side flags that the day ended early.

    Opens the short dispute window rather than paying out at once, because the
    number of hours is exactly what the two sides are most likely to disagree
    about, and it is the number the payout is computed from.
    """
    if hasattr(job, "completion"):
        raise WorkflowError("This job has already been closed out.")

    is_worker = job.assigned_worker and job.assigned_worker.user_id == user.pk
    is_client = job.client.user_id == user.pk
    if not (is_worker or is_client):
        raise WorkflowError("This isn't your job.")

    hours = Decimal(hours_worked)
    if hours < 0:
        raise WorkflowError("Hours worked can't be negative.")
    if job.gig_hours is not None and hours > Decimal(job.gig_hours):
        raise WorkflowError(
            "That's more than the booked day — mark it complete instead."
        )

    actor = Actor.WORKER if is_worker else Actor.CLIENT
    locked = Job.objects.select_for_update().get(pk=job.pk)
    assert_transition(locked.state, JobState.ENDED_EARLY, actor)

    completion = _create_completion(
        job,
        hours=hours,
        ended_early=True,
        ended_by=EndedBy.WORKER if is_worker else EndedBy.CLIENT,
        note=note,
    )
    locked.state = JobState.ENDED_EARLY
    locked.save(update_fields=["state", "updated_at"])

    hours_label = f"{hours.normalize()}"
    window_hours = int(rules.EARLY_END_DISPUTE_WINDOW.total_seconds() // 3600)
    _notify(
        job,
        user,
        f"Flagged this as ended early after {hours_label} hours. "
        f"${completion.payable_amount} would be released. If that's wrong, "
        f"raise a dispute within {window_hours} hours."
        + (f"\n\nNote: {note}" if note else ""),
    )
    return completion


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------


def _release_for(completion: Completion, *, actor: str) -> Completion:
    job = completion.job
    escrow = _escrow(job)
    if escrow is None or escrow.status != EscrowStatus.AUTHORIZED:
        raise WorkflowError("There is no held payment on this job to release.")

    # A full day captures the whole hold; an early finish captures the prorated
    # part and Stripe returns the rest to the client automatically.
    amount = None if completion.payable_amount >= escrow.amount else completion.payable_amount
    release(escrow, actor=actor, amount=amount)

    completion.settled_at = timezone.now()
    completion.save(update_fields=["settled_at", "updated_at"])
    return completion


@transaction.atomic
def approve(job: Job, user) -> Completion:
    """Client approves release before the window lapses."""
    if job.client.user_id != user.pk:
        raise WorkflowError("Only the client who posted this can approve it.")
    completion = getattr(job, "completion", None)
    if completion is None:
        raise WorkflowError("Nothing to approve yet.")
    if completion.settled_at:
        return completion
    return _release_for(completion, actor=Actor.CLIENT)


@transaction.atomic
def auto_settle(completion: Completion) -> Completion:
    """Release because nobody acted in time.

    Silence is approval. A client who never logs in again must not be able to
    hold a worker's pay indefinitely — and on an early finish, the same window
    protects the client, who had their chance to object to the hours.
    """
    if completion.settled_at:
        return completion
    return _release_for(completion, actor=Actor.SYSTEM)


def settle_due(now=None) -> list[Completion]:
    """Every completion whose window has lapsed. Driven by the cron command."""
    now = now or timezone.now()
    due = Completion.objects.filter(
        settled_at__isnull=True,
        settles_at__lte=now,
        job__state__in=[JobState.COMPLETED, JobState.ENDED_EARLY],
    ).select_related("job")

    settled = []
    for completion in due:
        try:
            settled.append(auto_settle(completion))
        except (WorkflowError, EscrowError):
            # A job with no usable escrow is a reconciliation problem, not a
            # reason to abandon the rest of the batch.
            continue
    return settled


# ---------------------------------------------------------------------------
# Disputing
# ---------------------------------------------------------------------------


@transaction.atomic
def raise_dispute(job: Job, user, *, reason: str) -> Dispute:
    """Freeze the money and hand it to a human."""
    if hasattr(job, "dispute"):
        raise WorkflowError("This job is already under review.")

    is_worker = job.assigned_worker and job.assigned_worker.user_id == user.pk
    is_client = job.client.user_id == user.pk
    if not (is_worker or is_client):
        raise WorkflowError("This isn't your job.")
    if not reason.strip():
        raise WorkflowError("Say what the problem is — a human has to read this.")

    actor = Actor.WORKER if is_worker else Actor.CLIENT
    locked = Job.objects.select_for_update().get(pk=job.pk)
    assert_transition(locked.state, JobState.DISPUTED, actor)

    dispute = Dispute.objects.create(job=job, raised_by=user, reason=reason.strip())
    locked.state = JobState.DISPUTED
    locked.save(update_fields=["state", "updated_at"])

    _notify(job, user, f"Raised a dispute on this job.\n\n{reason.strip()}")
    return dispute


def can_still_act(job: Job) -> bool:
    """Is this gig in a phase where the work buttons make sense at all?"""
    return job.job_type == JobType.GIG and job.state in {
        JobState.ESCROW_HELD,
        JobState.IN_PROGRESS,
        JobState.ENDED_EARLY,
        JobState.COMPLETED,
    }
