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
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from config import business_rules as rules
from core.money import money
from core.state_machine import can_transition, claim, Actor, JobState, assert_transition
from jobs.models import Job, JobType, booking_of
from notifications.models import Kind
from notifications.services import booking_key, notify
from payments.models import EscrowPayment, EscrowStatus
from payments.services import EscrowError, release

from .models import CheckIn, Completion, Dispute, DisputeStatus, EndedBy, payable_for


class WorkflowError(RuntimeError):
    """A refusal phrased for the person who hit it."""


def _claim_booking(job, *, to: str, actor: str) -> list[Job]:
    """Move every day of this booking that legally can, and say which moved.

    A booking is one arrangement split into a row per day, and the split is
    storage — the day is what carries an escrow and a sign-off. Finishing and
    closing are not per-day facts: the pair agreed one booking and they finish
    one booking, so one press has to reach all of it.

    Days that cannot make the move are skipped rather than refused. On a week
    where Tuesday is already disputed, or Friday has not been checked into yet,
    the honest answer is to move what can move — the alternative is one stuck
    day blocking a client from closing the other four.

    **And a day that has not arrived is not one of them.** Both routes through
    here are somebody saying the work happened, and nobody can say that about
    next Thursday. This used to sign off the whole booking on its first
    morning, which had two consequences and both were wrong: a week's work was
    recorded as done before six days of it existed, and the worker's diary was
    handed back to her — free to be booked by somebody else on days she is in
    fact committed to. Signing off a Monday says nothing about the Tuesday
    after it, so Tuesday waits its turn.

    Nothing here releases money for a day that has not happened; the settlement
    path decides that for itself, off the completions that exist.
    """
    today = timezone.localdate()
    moved = []
    for day in booking_of(job):
        # Standing positions have no date and are not caught by this — there is
        # no day for them to be ahead of.
        if day.gig_date is not None and day.gig_date > today:
            continue
        if not can_transition(day.state, to, actor):
            continue
        if not claim(Job, day.pk, expect=day.state, to=to):
            continue
        day.state = to
        moved.append(day)
    return moved


def _too_early(job) -> str | None:
    """The refusal for a booking nobody can sign off yet, or None.

    What separates "somebody beat you to it" from "that day has not happened".
    Both come back from the claim as an empty list and read identically to
    whoever pressed the button, and the second one is not a failure at all —
    it is an answer about the calendar, so it names the day to come back on.
    """
    today = timezone.localdate()
    ahead = [
        day
        for day in booking_of(job)
        if day.gig_date is not None and day.gig_date > today
    ]
    if not ahead or len(ahead) != len([d for d in booking_of(job) if d.gig_date]):
        # Some day of this booking has arrived, so the empty claim is about
        # state rather than about the calendar.
        return None
    first = min(day.gig_date for day in ahead)
    return _(
        "That day hasn't come yet — you can mark it done from %(day)s."
    ) % {"day": date_format(first, "D j M")}


def _raced(job_id) -> str:
    """What to say when a claim is lost.

    Every write below is conditional on the job still being in the state it was
    read in, so two people acting on the same job in the same second get one
    winner and this sentence.

    It names the state rather than the culprit. Who moved it is genuinely not
    knowable from here — the other party, the settlement cron and their own
    second tab are all live possibilities, and a message that guesses wrong is
    worse than one that does not guess. Where the job ended up is knowable, is
    the thing they actually need, and answers "what do I do now" without them
    having to ask. One extra query, on a path that only runs when two people
    have collided.
    """
    state = Job.objects.filter(pk=job_id).values_list("state", flat=True).first()
    if state is None:
        return "That job is no longer there. Open your jobs to see where things stand."
    return (
        f"This job moved while you were working on it. It now reads: "
        f"{JobState(state).label}. Open it again to see where it stands."
    )


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

    locked = Job.objects.get(pk=job.pk)

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

    # Rolls the CheckIn row back with it: raising inside @transaction.atomic is
    # what undoes the work above, and a check-in recorded against a job that
    # moved on is a site visit the record cannot explain.
    if not claim(Job, job.pk, expect=locked.state, to=JobState.IN_PROGRESS):
        raise WorkflowError(_raced(job.pk))
    locked.state = JobState.IN_PROGRESS
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

    locked = Job.objects.get(pk=job.pk)
    assert_transition(locked.state, JobState.COMPLETED, Actor.WORKER)

    completion = _create_completion(
        job, hours=Decimal(job.gig_hours or 0), ended_early=False
    )
    if not claim(Job, job.pk, expect=locked.state, to=JobState.COMPLETED):
        raise WorkflowError(_raced(job.pk))
    locked.state = JobState.COMPLETED

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

    locked = Job.objects.get(pk=job.pk)
    assert_transition(locked.state, JobState.COMPLETED, Actor.WORKER)

    # The whole booking. Nothing unique backs this route — no check-in, no
    # Completion row — so the claim inside is also what stands between a double
    # tap and the client being told twice.
    finished = _claim_booking(job, to=JobState.COMPLETED, actor=Actor.WORKER)
    if not finished:
        raise WorkflowError(_too_early(job) or _raced(job.pk))

    days = len(finished)
    said = (
        f"Marked all {days} days finished."
        if days > 1
        else "Marked the work finished."
    )
    _notify(
        job,
        worker.user,
        f"{said} Confirm it when you agree and the job is "
        "closed — payment is between us, so nothing releases on its own.",
    )
    notify(
        job.client.user,
        Kind.WORK_FINISHED,
        job=job,
        actor=worker.user,
        dedupe=booking_key("finished", job),
        worker=str(worker.user),
        job_title=job.title,
    )
    return next((d for d in finished if d.pk == job.pk), finished[0])


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

    locked = Job.objects.get(pk=job.pk)
    assert_transition(locked.state, JobState.CLOSED, Actor.CLIENT)

    # Closed is closed, for every day of it. Claimed before the counters move,
    # and this is the one where that ordering earns its keep: jobs_completed is
    # reputation, and an F() + 1 that ran when it should not have leaves both
    # track records permanently overstated with nothing in the data to show it.
    closed = _claim_booking(job, to=JobState.CLOSED, actor=Actor.CLIENT)
    if not closed:
        raise WorkflowError(_too_early(job) or _raced(job.pk))
    locked.state = JobState.CLOSED

    # Once for the booking, not once per day. A week worked for one client is
    # one job done by both of them — counting it five times would inflate the
    # only number on a profile that is supposed to mean "how much has this
    # person actually seen through", and the same reasoning is why there is one
    # rating for it rather than five.
    worker = job.assigned_worker
    if worker is not None:
        type(worker).objects.filter(pk=worker.pk).update(
            jobs_completed=models.F("jobs_completed") + 1
        )
    client = job.client
    type(client).objects.filter(pk=client.pk).update(
        jobs_completed=models.F("jobs_completed") + 1
    )

    if worker is not None:
        notify(
            worker.user,
            Kind.JOB_CLOSED,
            job=job,
            actor=user,
            dedupe=booking_key("closed", job),
            job_title=job.title,
        )

    days = len(closed)
    _notify(
        job,
        user,
        (
            f"Confirmed all {days} days — the booking is closed. "
            if days > 1
            else "Confirmed — the job is closed. "
        )
        + "You can both leave a rating now.",
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
    locked = Job.objects.get(pk=job.pk)
    assert_transition(locked.state, JobState.ENDED_EARLY, actor)

    completion = _create_completion(
        job,
        hours=hours,
        ended_early=True,
        ended_by=EndedBy.WORKER if is_worker else EndedBy.CLIENT,
        note=note,
    )
    if not claim(Job, job.pk, expect=locked.state, to=JobState.ENDED_EARLY):
        raise WorkflowError(_raced(job.pk))
    locked.state = JobState.ENDED_EARLY

    hours_label = f"{hours.normalize()}"
    window_hours = int(rules.EARLY_END_DISPUTE_WINDOW.total_seconds() // 3600)
    _notify(
        job,
        user,
        f"Flagged this as ended early after {hours_label} hours. "
        f"{money(completion.payable_amount)} would be released. If that's wrong, "
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
    """Client approves release before the window lapses.

    Approving pays the booking, not the day. One press on a week's work
    releases every day of it that is waiting — the client agreed one
    arrangement and is answering it once, and making them press the same button
    on five consecutive screens is not five decisions, it is one decision typed
    out five times.

    The limit, and it is deliberate: only days with a completion on them. A
    completion exists because somebody said that day's work was done, so this
    can never release money for a day nobody has worked yet. On a week where
    Monday is finished and Thursday has not happened, approving pays Monday and
    leaves Thursday's hold exactly where it is.
    """
    if job.client.user_id != user.pk:
        raise WorkflowError("Only the client who posted this can approve it.")

    completion = getattr(job, "completion", None)
    if completion is None:
        raise WorkflowError("Nothing to approve yet.")

    due = (
        Completion.objects.filter(job__in=booking_of(job), settled_at__isnull=True)
        .select_related("job")
        .order_by("job__gig_date")
    )
    for pending in due:
        _release_for(pending, actor=Actor.CLIENT)

    completion.refresh_from_db()
    return completion


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
    locked = Job.objects.get(pk=job.pk)
    assert_transition(locked.state, JobState.DISPUTED, actor)

    # Claimed first, so the loser never gets as far as the OneToOne and the
    # refusal is a sentence rather than an IntegrityError page. Both sides can
    # be typing a reason at once here — that is the normal shape of a dispute.
    if not claim(Job, job.pk, expect=locked.state, to=JobState.DISPUTED):
        raise WorkflowError(_raced(job.pk))
    locked.state = JobState.DISPUTED

    dispute = Dispute.objects.create(job=job, raised_by=user, reason=reason.strip())

    other = job.client.user if is_worker else (
        job.assigned_worker.user if job.assigned_worker else None
    )
    notify(
        other,
        Kind.DISPUTE,
        job=job,
        actor=user,
        dedupe=booking_key("dispute", job),
        job_title=job.title,
        reason=reason.strip()[:500],
    )

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
