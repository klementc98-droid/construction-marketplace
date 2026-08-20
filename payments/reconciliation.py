"""Asking Stripe what actually happened, and making the database agree.

Everything this app believes about money is a local note about something that
happened on somebody else's server. Most of the time the two agree, because
every path that moves money claims its rows before it calls out and records the
answer when it comes back. This module is for the times they cannot.

They cannot because a database transaction does not reach across a network
call. A capture succeeds and the process dies before the commit: Stripe has the
money, the row still reads AUTHORIZED, and no amount of care in the writing
order prevents it — the two systems are not one transaction and never will be.
The answer to that is not a bigger transaction. It is asking.

So this is deliberately not clever. It walks the rows that could be behind, asks
Stripe about each one, and moves the local record to match what Stripe says.
Stripe is the authority throughout: where they disagree about money, the money
is right and this database is wrong.

Four kinds of disagreement, and the fourth is the one with a decision in it:

* **A hold Stripe has taken and we have not recorded.** The crash window. The
  local row catches up, including the amounts actually captured.
* **A hold Stripe has cancelled underneath us.** Recorded as refunded, so the
  job stops waiting for money that is not coming.
* **A checkout that was paid while nobody was looking.** The webhook never
  landed and the browser never came back; the hold is real and is recorded.
* **A hold on a job that has died.** Somebody's card is committed to a gig that
  expired or was called off — the divergence ``mark_authorized`` writes down
  and cannot resolve on its own. Here it is resolved the only way that is fair
  to the person whose money it is: the authorisation is cancelled and the hold
  released. Nothing is captured, nobody is charged, and the record says so.

Run it on a timer beside the other scheduled work. It is idempotent by
construction — every repair is a conditional claim, so a second run over a row
that is already right does nothing at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.utils import timezone

from config import business_rules as rules
from core.state_machine import Actor, JobState, can_transition, claim
from jobs.models import Job

from . import gateway
from .models import EscrowPayment, EscrowStatus, StripeAccount

logger = logging.getLogger(__name__)

#: Job states in which a held authorisation still has a future. Anything else
#: means the work is not going to happen, and money committed against it should
#: go back rather than sit on somebody's card until the hold expires by itself.
LIVE_STATES = (
    JobState.ACCEPTED,
    JobState.ESCROW_HELD,
    JobState.IN_PROGRESS,
    JobState.COMPLETED,
    JobState.ENDED_EARLY,
    JobState.DISPUTED,
)


@dataclass
class Report:
    """What one pass found and did. Printed by the command, asserted by tests."""

    checked: int = 0
    captured_recorded: list = field(default_factory=list)
    cancellations_recorded: list = field(default_factory=list)
    holds_released: list = field(default_factory=list)
    accounts_adopted: list = field(default_factory=list)
    states_repaired: list = field(default_factory=list)
    checkouts_abandoned: list = field(default_factory=list)
    unreachable: list = field(default_factory=list)

    @property
    def repaired(self) -> int:
        return (
            len(self.captured_recorded)
            + len(self.cancellations_recorded)
            + len(self.holds_released)
            + len(self.accounts_adopted)
            + len(self.states_repaired)
            + len(self.checkouts_abandoned)
        )

    def lines(self) -> list[str]:
        out = [f"Checked {self.checked} payment(s)."]
        for escrow_id, amount in self.captured_recorded:
            out.append(f"  escrow {escrow_id}: Stripe had captured {amount} — recorded")
        for escrow_id in self.cancellations_recorded:
            out.append(f"  escrow {escrow_id}: Stripe had cancelled the hold — recorded")
        for escrow_id, state in self.holds_released:
            out.append(
                f"  escrow {escrow_id}: job is {state}, hold released back to the client"
            )
        for worker_id, account_id in self.accounts_adopted:
            out.append(f"  worker {worker_id}: adopted lost account {account_id}")
        for escrow_id in self.checkouts_abandoned:
            out.append(f"  escrow {escrow_id}: checkout expired unpaid — recorded")
        for job_id, was, now in self.states_repaired:
            out.append(f"  job {job_id}: payment had settled — {was} moved to {now}")
        for what, why in self.unreachable:
            out.append(f"  {what}: could not be checked ({why})")
        if not self.repaired and not self.unreachable:
            out.append("  Nothing to repair.")
        return out


def _record_capture(escrow: EscrowPayment, settled) -> bool:
    """Bring a row that Stripe has already captured up to date.

    Claimed, not saved. A release running at this moment is doing the same work
    honestly and must win; losing here means the row was repaired by the code
    that owns it, which is the better outcome.
    """
    return claim(
        EscrowPayment,
        escrow.pk,
        field="status",
        expect=EscrowStatus.AUTHORIZED,
        to=EscrowStatus.RELEASED,
        captured_amount=settled,
        captured_fee=rules.platform_fee_for(settled),
        captured_payout=rules.worker_payout_for(settled),
        released_at=timezone.now(),
    )


def _escrows_to_check():
    return (
        EscrowPayment.objects.filter(
            status__in=(EscrowStatus.AUTHORIZED, EscrowStatus.PENDING)
        )
        .select_related("job", "worker__user")
        .order_by("pk")
    )


def reconcile(*, release_dead_holds: bool = True, dry_run: bool = False) -> Report:
    """One pass. Safe to run as often as you like.

    ``dry_run`` reads everything and writes nothing, on this same path rather
    than through a parallel one — a second implementation of "what would you
    do" is a second thing to keep true, and the one nobody runs.
    """
    report = Report()

    for escrow in _escrows_to_check():
        report.checked += 1

        # A pending row has no intent yet; what it has is a checkout, and the
        # question there is whether it was paid without anybody hearing.
        if escrow.status == EscrowStatus.PENDING:
            _reconcile_pending(escrow, report, dry_run=dry_run)
            continue

        try:
            intent = gateway.retrieve_payment_intent(escrow.payment_intent_id)
        except gateway.ObjectMissing:
            report.unreachable.append((f"escrow {escrow.pk}", "no such intent"))
            continue
        except Exception as exc:                # noqa: BLE001
            # Stripe not answering is not a finding. Leave the row alone and
            # look again next run — repairing on a guess is how a reconciler
            # becomes the thing that needs reconciling.
            report.unreachable.append((f"escrow {escrow.pk}", str(exc)[:120]))
            continue

        status = intent.get("status")

        if status == "succeeded":
            settled = intent.get("amount_received") or escrow.amount
            if dry_run:
                report.captured_recorded.append((escrow.pk, settled))
                continue
            if _record_capture(escrow, settled):
                report.captured_recorded.append((escrow.pk, settled))
                _match_job_to(escrow, JobState.PAID_OUT)
            continue

        if status == "canceled":
            if dry_run:
                report.cancellations_recorded.append(escrow.pk)
                continue
            if claim(
                EscrowPayment,
                escrow.pk,
                field="status",
                expect=EscrowStatus.AUTHORIZED,
                to=EscrowStatus.REFUNDED,
                refunded_at=timezone.now(),
            ):
                report.cancellations_recorded.append(escrow.pk)
                # And the job, which was the half being left behind. A payment
                # record reading refunded over a job still reading escrow_held
                # is the same disagreement this module exists to end, produced
                # by the module itself.
                _match_job_to(escrow, JobState.REFUNDED)
            continue

        # Still held. The only question left is whether it should be.
        if release_dead_holds and escrow.job.state not in LIVE_STATES:
            if dry_run:
                report.holds_released.append((escrow.pk, escrow.job.state))
            else:
                _release_dead_hold(escrow, report)

    _settled_jobs_left_behind(report, dry_run=dry_run)
    _reconcile_accounts(report, dry_run=dry_run)
    return report


def _match_job_to(escrow: EscrowPayment, state: str) -> bool:
    """Move the job to match what the payment now says, if that move is legal.

    Legality is asked of the state machine rather than assumed, because this
    runs over rows in states nobody planned for — that is the point of it — and
    forcing a job from cancelled to paid_out because Stripe captured something
    would be repairing a disagreement by inventing a worse one.

    Asked of every actor rather than of one, and that needs saying plainly.
    This is not the reconciler granting itself authority: it never originates a
    move. Every one it makes is a move somebody already made — a client who
    cancelled after funding, an admin who resolved a dispute, a timer that
    released a lapsed approval window — and whose only surviving evidence is at
    Stripe. Which of them it was is exactly what has been lost, so naming one
    would be inventing the answer: no single actor even covers the two cases
    here, since completed → paid_out is the client's or the timer's and
    disputed → paid_out is an admin's alone.
    
    What survives is the guarantee that matters. The move still has to be one
    the lifecycle permits *somebody* to make, so this can never put a job
    somewhere no actor could have put it — it can only fail to name which.
    """
    job = Job.objects.filter(pk=escrow.job_id).first()
    if job is None or job.state == state:
        return False
    if not any(
        can_transition(job.state, state, actor)
        for actor in (Actor.SYSTEM, Actor.CLIENT, Actor.ADMIN, Actor.WORKER)
    ):
        logger.warning(
            "escrow %s is %s but job %s cannot move from %s to %s",
            escrow.pk,
            escrow.status,
            job.pk,
            job.state,
            state,
        )
        return False
    return claim(Job, job.pk, expect=job.state, to=state)


def _settled_jobs_left_behind(report: Report, *, dry_run: bool) -> None:
    """Payments that finished while their job did not follow.

    The gap a reconciler is most likely to leave, because it is the one it
    creates: the escrow is claimed first and the job second, and a lost race on
    the second used to end there — the row was no longer AUTHORIZED, so no
    later pass would look at it again. A payment record and a job disagreeing
    for good, inside the pass whose whole job is to stop that.

    Cheap to check and exact: the escrow says what the job should say.
    """
    wanted = {
        EscrowStatus.RELEASED: JobState.PAID_OUT,
        EscrowStatus.REFUNDED: JobState.REFUNDED,
    }
    for status, state in wanted.items():
        behind = (
            EscrowPayment.objects.filter(status=status)
            .exclude(job__state=state)
            .select_related("job")
        )
        for escrow in behind:
            if dry_run:
                report.states_repaired.append((escrow.job_id, escrow.job.state, state))
                continue
            was = escrow.job.state
            if _match_job_to(escrow, state):
                report.states_repaired.append((escrow.job_id, was, state))


def _reconcile_pending(
    escrow: EscrowPayment, report: Report, *, dry_run: bool = False
) -> None:
    """A checkout nobody heard back about."""
    if not escrow.checkout_session_id:
        return
    try:
        session = gateway.retrieve_session(escrow.checkout_session_id)
    except gateway.ObjectMissing:
        return
    except Exception as exc:                    # noqa: BLE001
        report.unreachable.append((f"escrow {escrow.pk}", str(exc)[:120]))
        return

    intent_id = session.get("payment_intent")
    if session.get("payment_status") == "paid" and intent_id:
        if not dry_run:
            from . import services

            services.mark_authorized(escrow, intent_id)
        report.captured_recorded.append((escrow.pk, escrow.amount))
        return

    # A checkout that ended without being paid. Only the "paid" case was
    # handled, so one that expired or was declined sat at PENDING for ever:
    # the funding page kept offering to reuse a session Stripe had closed, and
    # nothing ever said what had happened. Recorded as failed, which is also
    # what lets the client start a fresh attempt.
    if session.get("status") == "expired":
        if not dry_run:
            claim(
                EscrowPayment,
                escrow.pk,
                field="status",
                expect=EscrowStatus.PENDING,
                to=EscrowStatus.FAILED,
                last_error="Checkout expired without being paid.",
            )
        report.checkouts_abandoned.append(escrow.pk)


def _release_dead_hold(escrow: EscrowPayment, report: Report) -> None:
    """Give back money committed to a job that is not going to happen.

    The one place here that decides something rather than copying an answer
    down, so it is worth saying whose interest it serves. A hold is the
    client's money, frozen. The job it was frozen for has expired or been
    called off, so nobody is owed it and nobody is going to earn it — and
    leaving it sitting there until the authorisation lapses of its own accord
    is a week of somebody's credit limit spent on nothing.

    Cancelling takes nothing from anyone: no capture happens, the worker was
    never going to be paid for a job that is not running, and the client's card
    is freed. Claimed first, so a real refund happening at the same moment wins
    and this becomes a no-op.
    """
    if not claim(
        EscrowPayment,
        escrow.pk,
        field="status",
        expect=EscrowStatus.AUTHORIZED,
        to=EscrowStatus.REFUNDED,
        refunded_at=timezone.now(),
    ):
        return
    try:
        gateway.cancel_payment_intent(escrow.payment_intent_id)
    except Exception as exc:                    # noqa: BLE001
        # Put it back and try again next run: a row saying refunded over a hold
        # that is still live is a worse lie than the one we started with.
        claim(
            EscrowPayment,
            escrow.pk,
            field="status",
            expect=EscrowStatus.REFUNDED,
            to=EscrowStatus.AUTHORIZED,
            refunded_at=None,
        )
        report.unreachable.append((f"escrow {escrow.pk}", str(exc)[:120]))
        return
    report.holds_released.append((escrow.pk, escrow.job.state))


def _reconcile_accounts(report: Report, *, dry_run: bool = False) -> None:
    """Connect accounts opened at Stripe whose id never reached this database.

    The 24-hour hole: a process that died between Stripe answering and the
    insert leaves an account nobody here can name, and Stripe forgets the
    idempotency key that would have made the retry safe. The blank row is the
    handle, the worker id in the account's metadata is the match.
    """
    for account in StripeAccount.objects.filter(account_id="").select_related("worker"):
        try:
            found = gateway.find_account_for(account.worker_id)
        except Exception as exc:                # noqa: BLE001
            report.unreachable.append((f"worker {account.worker_id}", str(exc)[:120]))
            continue
        if not found:
            continue
        if dry_run:
            report.accounts_adopted.append((account.worker_id, found))
            continue
        if StripeAccount.objects.filter(pk=account.pk, account_id="").update(
            account_id=found, updated_at=timezone.now()
        ):
            report.accounts_adopted.append((account.worker_id, found))
