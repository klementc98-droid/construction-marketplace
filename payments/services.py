"""Escrow operations: the rules, the state changes, and the Stripe calls.

Views stay thin and call in here. Phase 5's check-in and dispute flow, and the
admin queue in phase 7, will call the same functions — a release triggered by a
client tapping approve, by the 48-hour timer, or by an admin resolving a
dispute must move exactly the same money in exactly the same way.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from config import business_rules as rules
from notifications.models import Kind
from notifications.services import booking_key, notify

from core.money import money
from core.state_machine import claim, Actor, JobState, assert_transition
from jobs.models import Job, JobType

from . import gateway
from .models import EscrowPayment, EscrowStatus, StripeAccount


logger = logging.getLogger(__name__)


class EscrowError(RuntimeError):
    """A refusal the user should see, phrased for them rather than for a log."""


# ---------------------------------------------------------------------------
# Worker onboarding
# ---------------------------------------------------------------------------


def ensure_stripe_account(worker) -> StripeAccount:
    """Fetch or create the worker's Connect account.

    The read and the write cannot be made one operation, because between them
    sits a call to somebody else's server — so this is not "check then insert",
    which two requests arriving together both pass. It is protected at both
    ends instead.

    At Stripe's end, an idempotency key derived from the worker: two concurrent
    creations return the *same* account rather than opening a second one. That
    is the half that matters, because an orphaned Connect account is the one
    outcome this database cannot clean up — nothing here would ever point at
    it, and only Stripe knows it exists.

    At ours, the OneToOne. Whichever request loses the insert reads back the
    row the winner wrote, and both callers end up with the same account, which
    is also the same account Stripe returned to each of them.

    The key is stable for the life of the worker on purpose. Stripe keys expire
    after 24 hours, so this stops being a shortcut around a retry a day later
    and starts being what it is meant to be: protection against the same
    creation running twice at once.
    """
    existing = StripeAccount.objects.filter(worker=worker).first()
    if existing and existing.is_open:
        return existing

    # The row is claimed *before* Stripe is asked, and this is the half that
    # was missing. The idempotency key below protects two requests arriving
    # together; it does nothing for a process that dies between Stripe
    # answering and the insert, because Stripe forgets the key after 24 hours
    # and the retry a day later opens a second account. A row with a blank id
    # is a note saying "an account was being made for this worker" — which is
    # what turns an orphan from invisible into findable.
    claimed_now = False
    if existing is None:
        try:
            with transaction.atomic():
                existing = StripeAccount.objects.create(worker=worker)
                claimed_now = True
        except IntegrityError:
            existing = StripeAccount.objects.get(worker=worker)
            if existing.is_open:
                return existing

    # A blank row we did *not* just write means an earlier attempt reached
    # Stripe and never came back. Only then is there anything to adopt — and
    # only then is a listing worth its round trip.
    account_id = None if claimed_now else _adopt_lost_account(worker)

    if not account_id:
        account_id = gateway.create_express_account(
            email=worker.user.email,
            country=worker.region.country,
            idempotency_key=f"connect-account:{worker.pk}",
            metadata={"worker_id": str(worker.pk)},
        )

    StripeAccount.objects.filter(pk=existing.pk, account_id="").update(
        account_id=account_id, updated_at=timezone.now()
    )
    existing.refresh_from_db()
    return existing


def _adopt_lost_account(worker) -> str | None:
    """An account Stripe already holds for this worker, if there is one.

    Asked only when a row exists with no id — which means a previous attempt
    reached Stripe and did not come back. Answering "no" is normal and cheap;
    answering "yes" is what stops a second account being opened for somebody
    who already has one they cannot use.

    Silent on failure. This is a recovery nicety in the middle of somebody
    trying to get paid, and a listing that times out must not stop them: the
    worst case is the orphan staying an orphan for another attempt, which is
    where they already were.
    """
    try:
        return gateway.find_account_for(worker.pk)
    except gateway.StripeNotConfigured:
        raise
    except Exception:                        # noqa: BLE001 - see the docstring
        logger.warning("Could not check Stripe for a lost account", exc_info=True)
        return None


def refresh_account_flags(account: StripeAccount) -> StripeAccount:
    """Re-read Stripe's view of the account and cache it."""
    flags = gateway.retrieve_account(account.account_id)
    account.details_submitted = flags["details_submitted"]
    account.charges_enabled = flags["charges_enabled"]
    account.payouts_enabled = flags["payouts_enabled"]
    account.save(
        update_fields=[
            "details_submitted",
            "charges_enabled",
            "payouts_enabled",
            "updated_at",
        ]
    )
    return account


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------


def funding_blocker(job: Job) -> str | None:
    """Why this job cannot be funded right now, or ``None`` if it can.

    Returned as a message rather than a boolean because every one of these is
    something the client has to *do* something about, and "you can't fund this"
    with no reason is the least useful sentence in a payments flow.
    """
    if job.job_type != JobType.GIG:
        return "Only gigs are paid through the platform."
    if job.state != JobState.ACCEPTED:
        if job.state == JobState.POSTED:
            return "Pick a worker first."
        return "This job is past the funding stage."
    if job.assigned_worker is None:
        return "Pick a worker first."

    account = StripeAccount.objects.filter(worker=job.assigned_worker).first()
    if account is None or not account.is_ready:
        return (
            f"{job.assigned_worker.user} hasn't finished setting up payouts yet. "
            "They need to do that before you can fund the job."
        )

    if job.gig_date is not None:
        days_ahead = (job.gig_date - timezone.localdate()).days
        if days_ahead > rules.ESCROW_AUTHORIZATION_MAX_DAYS:
            return (
                f"This gig is {days_ahead} days away. Card holds expire, so "
                f"funding opens {rules.ESCROW_AUTHORIZATION_MAX_DAYS} days "
                "before the date."
            )
    return None


def _live_checkout(escrow: EscrowPayment) -> str | None:
    """The checkout already open for this escrow, if there still is one.

    Asked of Stripe rather than assumed from our own row, because only Stripe
    knows whether a session is still open, has been paid, or has expired — and
    the three want different answers. One network call, on the second press of
    a button, to avoid opening a second way to pay for the same job.

    A session Stripe has never heard of is gone, and starting again is right.
    Anything else is not an answer and is not treated as one: this used to
    catch every exception and open a new checkout, so a Stripe timeout re-made
    the very hole the idempotency key was added to close. A timeout means
    Stripe may hold that session, may have taken money on it, and simply did
    not get a reply to us — the one moment when opening a second way to pay is
    worst. The error travels up instead, and the caller shows "try again".
    """
    if not (escrow.checkout_session_id and escrow.checkout_url):
        return None
    try:
        live = gateway.retrieve_session(escrow.checkout_session_id)
    except gateway.ObjectMissing:
        # Stripe answered, and there is no such session. Starting again is the
        # right move and the only one that leaves the job fundable.
        return None

    if live.get("status") == "open":
        return escrow.checkout_url

    # Paid, and our webhook has not landed yet. The one thing that must not
    # happen here is a second session: the client has already committed a card
    # to this job. Recording it is idempotent, so doing it now simply means the
    # browser got here first.
    if live.get("payment_status") == "paid" or live.get("status") == "complete":
        intent = live.get("payment_intent")
        if intent:
            mark_authorized(escrow, intent)
        raise EscrowError("This job is already funded.")
    return None


def start_funding(job: Job, *, success_url: str, cancel_url: str) -> str:
    """Create (or reuse) the escrow row and return a Stripe Checkout URL.

    The escrow row is one per job, not one per attempt — but *starting* an
    attempt was not atomic, and that is a different thing. Two requests a
    double-click apart both read a row that is not yet AUTHORIZED, both call
    Stripe, and two Checkout Sessions exist for one job. Each carries its own
    PaymentIntent; if the client completes both, there are two holds on a real
    card and this database has a record of one. Nothing here would ever release
    the other — it would sit on their card until it expired.

    So an attempt is claimed rather than assumed, in two layers.

    A checkout already open is handed back instead of a second one being made.
    That alone covers the double-click, which is how this happens in practice.

    Underneath it, Stripe's own idempotency key, scoped to the attempt. Two
    calls that get past the check above compute the same key — they read the
    same counter — so Stripe returns one session to both rather than creating
    two. The counter is what lets a client who abandoned a checkout yesterday
    still start a genuinely new one today, and it is incremented with a
    conditional UPDATE so that concurrent callers do not count the same attempt
    twice.
    """
    blocker = funding_blocker(job)
    if blocker:
        raise EscrowError(blocker)

    amount = job.fixed_pay
    escrow, _ = EscrowPayment.objects.get_or_create(
        job=job,
        defaults={
            "worker": job.assigned_worker,
            "amount": amount,
            "platform_fee": rules.platform_fee_for(amount),
            "worker_payout": rules.worker_payout_for(amount),
        },
    )
    if escrow.status == EscrowStatus.AUTHORIZED:
        raise EscrowError("This job is already funded.")
    if escrow.status in (EscrowStatus.RELEASED, EscrowStatus.REFUNDED):
        raise EscrowError("This job's payment is already settled.")

    open_already = _live_checkout(escrow)
    if open_already:
        return open_already

    attempt = escrow.funding_attempts + 1
    account = job.assigned_worker.stripe_account
    session_id, url = gateway.create_checkout_session(
        job_title=job.title,
        amount=escrow.amount,
        platform_fee=escrow.platform_fee,
        destination_account_id=account.account_id,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"job_id": str(job.pk), "escrow_id": str(escrow.pk)},
        idempotency_key=f"escrow:{escrow.pk}:attempt:{attempt}",
    )

    # Conditional on the attempt we read, so the loser of a race records
    # nothing rather than counting the same attempt a second time. Both are
    # holding the same session either way — that is what the key bought.
    EscrowPayment.objects.filter(
        pk=escrow.pk, funding_attempts=escrow.funding_attempts
    ).update(
        funding_attempts=attempt,
        checkout_session_id=session_id,
        checkout_url=url,
        status=EscrowStatus.PENDING,
        updated_at=timezone.now(),
    )
    escrow.refresh_from_db()
    return url


@transaction.atomic
def mark_authorized(escrow: EscrowPayment, payment_intent_id: str) -> EscrowPayment:
    """Record that the hold is in place and move the job to ESCROW_HELD.

    Idempotent: the webhook and the browser returning from Checkout race each
    other constantly, and both call this. Whichever arrives second must be a
    no-op, not a second state change.
    """
    if escrow.status == EscrowStatus.AUTHORIZED:
        return escrow

    job = Job.objects.get(pk=escrow.job_id)
    assert_transition(job.state, JobState.ESCROW_HELD, Actor.CLIENT)

    # The status check above is read from an instance somebody else may already
    # have moved. This is the same question asked of the database at the moment
    # of writing, which is the only place it can be answered honestly when the
    # webhook and the browser are both here — the collision this docstring
    # promises is constant, so it is worth being exact about.
    #
    # A lost claim is a no-op and not a refusal: both callers are doing the
    # right thing, and the second one arriving late is the expected case.
    authorized_at = timezone.now()
    if not claim(
        EscrowPayment,
        escrow.pk,
        field="status",
        expect=escrow.status,
        to=EscrowStatus.AUTHORIZED,
        payment_intent_id=payment_intent_id,
        authorized_at=authorized_at,
    ):
        escrow.refresh_from_db()
        return escrow

    escrow.payment_intent_id = payment_intent_id
    escrow.status = EscrowStatus.AUTHORIZED
    escrow.authorized_at = authorized_at

    # Checked, unlike before — but not reordered, and the difference from
    # release is worth being exact about. There, claiming the job first means a
    # lost race costs nothing because Stripe has not been called yet. Here the
    # hold already exists: this function runs *because* Stripe said so. There
    # is no ordering that makes the money go away.
    #
    # So the two failures are told apart. The record of the hold is kept
    # whatever happens — an authorisation on a real card with nothing in this
    # database pointing at it is the one outcome nobody can clean up. What is
    # not done is pretend the job is funded when it is no longer the job we
    # read: ``expire_stale_gigs`` moves ACCEPTED gigs whose day has passed, and
    # a client funding one as the hourly sweep runs is exactly this collision.
    if not claim(Job, job.pk, expect=job.state, to=JobState.ESCROW_HELD):
        job.refresh_from_db()
        divergence = (
            f"Hold recorded, but the job had moved to {job.state} and was not "
            f"marked funded. The authorisation is live and needs a decision."
        )
        EscrowPayment.objects.filter(pk=escrow.pk).update(
            last_error=divergence[:500], updated_at=timezone.now()
        )
        escrow.last_error = divergence
        # Loud, because nothing else will notice: no state is wrong enough for
        # a constraint to catch, and both rows are individually plausible.
        logger.error(
            "escrow %s authorized on job %s in state %s — not marked funded",
            escrow.pk,
            job.pk,
            job.state,
        )
        # And no "the money is held" email. On a job that has expired or been
        # called off that sentence is worse than silence: it is an invitation
        # to turn up.
        return escrow

    job.state = JobState.ESCROW_HELD

    # The one an escrow worker most needs. ACCEPTED and ESCROW_HELD are
    # deliberately different states because only the second is worth crossing
    # town for, and until now the difference was visible only to somebody
    # already looking at the page.
    notify(
        escrow.worker.user,
        Kind.ESCROW_FUNDED,
        job=job,
        dedupe=booking_key("funded", job),
        job_title=job.title,
        pay=str(job.fixed_pay),
        hours=str(job.gig_hours),
    )
    return escrow


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


@transaction.atomic
def release(
    escrow: EscrowPayment, *, actor: str, amount: Decimal | None = None
) -> EscrowPayment:
    """Capture the hold and pay the worker, less the platform fee.

    ``amount`` under the full total captures part and releases the rest back to
    the client — the prorated path phase 5 needs for a job that ended early.
    """
    if escrow.status == EscrowStatus.RELEASED:
        return escrow
    if escrow.status != EscrowStatus.AUTHORIZED:
        raise EscrowError("There is no held payment to release on this job.")

    job = Job.objects.get(pk=escrow.job_id)
    assert_transition(job.state, JobState.PAID_OUT, actor)

    if amount is not None and amount > escrow.amount:
        raise EscrowError("Cannot release more than was held.")

    # Claimed BEFORE Stripe is called, and this is the ordering that matters
    # most in the codebase. A client tapping approve in the same second the
    # settlement cron reaches this job is not a hypothetical — both read an
    # AUTHORIZED hold from their own instance, both pass the checks above, and
    # both would call capture on the same intent. Stripe refuses the second,
    # so the money is safe either way, but the app takes a gateway exception on
    # a job that was in fact paid correctly, which is a bad night for whoever
    # has to work out what happened.
    #
    # Rolling back is what makes this safe to do first: if the capture throws,
    # @transaction.atomic puts the status back to AUTHORIZED and the next run
    # tries again.
    released_at = timezone.now()

    # The job is claimed first, and its result is checked. Both halves of that
    # sentence were missing, and the second one is the bug.
    #
    # Two rows have to agree here, and only one of them was being decided
    # honestly. The escrow was claimed with a conditional UPDATE; the job was
    # moved with one whose answer was thrown away. So a client approving in the
    # same second as somebody raising a dispute could leave the money captured,
    # the escrow RELEASED, and the job sitting in DISPUTED — a dispute that can
    # never be honoured, because what it is disputing has already been paid.
    #
    # Claiming the job first is what makes losing that race cost nothing:
    # nothing external has happened yet, so the answer is simply "somebody
    # moved this, stop". Doing it after the capture would mean discovering the
    # collision with the money already gone.
    if not claim(Job, job.pk, expect=job.state, to=JobState.PAID_OUT):
        raise EscrowError(
            "This job moved while the payment was being released — nothing has "
            "been captured. Open it and see where it stands before trying again."
        )

    if not claim(
        EscrowPayment,
        escrow.pk,
        field="status",
        expect=EscrowStatus.AUTHORIZED,
        to=EscrowStatus.RELEASED,
        released_at=released_at,
    ):
        escrow.refresh_from_db()
        return escrow

    # The fee follows the capture down, and this is a money bug rather than a
    # detail. It was fixed on the session at the full amount, and Stripe does
    # not prorate it when less is taken — it charges the whole original fee
    # against the smaller capture, so every cent of the difference comes out of
    # the worker's half. A €100 day ending early at €60 turned a €7.20 fee into
    # €12. Recomputing on what is actually captured keeps the split the one
    # both sides agreed to.
    captured = amount if amount is not None else escrow.amount
    fee = rules.platform_fee_for(captured)
    result = gateway.capture_payment_intent(
        escrow.payment_intent_id, amount=amount, application_fee=fee
    )

    settled = result["amount_received"]
    escrow.status = EscrowStatus.RELEASED
    escrow.captured_amount = settled
    # Off what Stripe says it took, not off what we asked for. If those differ
    # the money is the authority.
    escrow.captured_fee = rules.platform_fee_for(settled)
    escrow.captured_payout = rules.worker_payout_for(settled)
    escrow.released_at = released_at
    escrow.save(
        update_fields=[
            "captured_amount",
            "captured_fee",
            "captured_payout",
            "updated_at",
        ]
    )

    # Already claimed above; this only brings the instance in hand up to date.
    job.state = JobState.PAID_OUT

    notify(
        escrow.worker.user,
        Kind.PAYMENT_RELEASED,
        job=job,
        # Not keyed on the booking: each day of a week is its own capture and
        # its own amount, and rolling them into one email would tell somebody
        # they had been paid once for five days' work.
        dedupe="",
        job_title=job.title,
        # What landed, not what was agreed. On a day that ended early those are
        # different numbers, and this one used to send the agreed figure —
        # telling somebody they had been paid in full for a day that settled at
        # two thirds. A payment notification naming the wrong amount is worse
        # than none: it is the number they will check their bank against.
        amount=money(escrow.captured_payout, decimals=2),
    )
    return escrow


@transaction.atomic
def refund(escrow: EscrowPayment, *, actor: str) -> EscrowPayment:
    """Give the money back and move the job to REFUNDED.

    While the payment is only authorised this cancels the hold rather than
    refunding a charge — nothing ever left the client's account, so there is
    nothing to return and nothing to wait for on their statement.
    """
    if escrow.status == EscrowStatus.REFUNDED:
        return escrow
    if escrow.status != EscrowStatus.AUTHORIZED:
        raise EscrowError("There is no held payment to return on this job.")

    job = Job.objects.get(pk=escrow.job_id)
    assert_transition(job.state, JobState.REFUNDED, actor)

    # Claimed before the gateway, for the reason given on release: two admins
    # resolving the same dispute is rarer than a cron meeting a click, but it
    # cancels the same intent twice and reads exactly as badly.
    refunded_at = timezone.now()

    # The job first and checked, for the reason given at length on release: two
    # rows have to agree, and a claim whose answer is discarded decides
    # nothing. Losing it here costs nothing at all, because the hold has not
    # been cancelled yet.
    if not claim(Job, job.pk, expect=job.state, to=JobState.REFUNDED):
        raise EscrowError(
            "This job moved while the payment was being returned — the hold is "
            "untouched. Open it and see where it stands before trying again."
        )

    if not claim(
        EscrowPayment,
        escrow.pk,
        field="status",
        expect=EscrowStatus.AUTHORIZED,
        to=EscrowStatus.REFUNDED,
        refunded_at=refunded_at,
    ):
        escrow.refresh_from_db()
        return escrow

    gateway.cancel_payment_intent(escrow.payment_intent_id)

    escrow.status = EscrowStatus.REFUNDED
    escrow.refunded_at = refunded_at
    job.state = JobState.REFUNDED
    return escrow


def mark_failed(escrow: EscrowPayment, reason: str) -> EscrowPayment:
    escrow.status = EscrowStatus.FAILED
    escrow.last_error = reason[:500]
    escrow.save(update_fields=["status", "last_error", "updated_at"])
    return escrow
