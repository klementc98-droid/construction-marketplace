"""Escrow records.

Stripe is the source of truth for money. These rows are our *record* of what
Stripe was asked to do and what it said back — never a second ledger we compute
independently. Anywhere the two could disagree, Stripe wins and we reconcile;
that is why every row carries the Stripe identifier that produced it.

Amounts are stored as ``Decimal`` dollars, and converted to integer cents at
the boundary in :mod:`payments.gateway`. Stripe speaks cents; the rest of the
codebase, the templates and the business rules speak dollars, and doing the
conversion in one place means no other module has to remember.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.models import WorkerProfile
from core.money import money
from core.models import TimestampedModel
from jobs.models import Job


class StripeAccount(TimestampedModel):
    """A worker's Stripe Connect account.

    Express accounts: Stripe owns the onboarding UI, the identity checks and
    the payout schedule. We hold the id and a cached copy of the three flags
    that decide whether this person can legally be paid — cached because they
    are read on every funding attempt and a Stripe round trip there would put
    a network call in the middle of a checkout.
    """

    worker = models.OneToOneField(
        WorkerProfile, on_delete=models.CASCADE, related_name="stripe_account"
    )
    #: Blank between the moment this row claims the worker and the moment
    #: Stripe answers with an id. That gap is the whole reason the row is
    #: written first: a crash inside it used to leave an account open at Stripe
    #: with nothing here pointing at it, and the next attempt a day later —
    #: past the idempotency window — would open a second one. An empty id is a
    #: record saying "an account was being made for this worker", which is
    #: exactly what reconciliation needs to go and find it.
    #:
    #: Unique, but not against other blanks: two workers may each be mid-flight.
    account_id = models.CharField(max_length=64, blank=True, default="")

    #: Mirrors of Stripe's flags, refreshed from `account.updated` webhooks and
    #: whenever the worker returns from onboarding. Never authoritative.
    details_submitted = models.BooleanField(default=False)
    charges_enabled = models.BooleanField(default=False)
    payouts_enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # One row per account, still — but blanks are exempt, because a
            # blank is not an account yet. Partial, so the database enforces
            # what the removed ``unique=True`` did without forbidding the
            # in-flight state that makes the orphan findable.
            models.UniqueConstraint(
                fields=["account_id"],
                condition=~models.Q(account_id=""),
                name="one_row_per_stripe_account",
            ),
        ]

    @property
    def is_open(self) -> bool:
        """Has Stripe actually answered with an account?"""
        return bool(self.account_id)

    def __str__(self) -> str:
        return f"{self.worker.user} ({self.account_id or 'not yet opened'})"

    @property
    def is_ready(self) -> bool:
        """Can this worker actually receive money?

        Both flags, not just one: an account can be able to take charges while
        payouts are still blocked pending documents, and paying into it would
        strand the worker's money inside Stripe.
        """
        return self.charges_enabled and self.payouts_enabled


class EscrowStatus(models.TextChoices):
    PENDING = "pending", _("Awaiting payment")
    """A checkout session exists; the client has not completed it."""

    AUTHORIZED = "authorized", _("Funds held")
    """The card is authorised and the money is committed but not taken."""

    RELEASED = "released", _("Released to worker")
    """Captured. Terminal."""

    REFUNDED = "refunded", _("Returned to client")
    """The authorisation was cancelled or the charge refunded. Terminal."""

    FAILED = "failed", _("Failed")
    """Stripe refused. ``last_error`` says why. Terminal for this attempt."""


class EscrowPayment(TimestampedModel):
    """One gig's money, from authorisation to release.

    One per job, not one per attempt: a client who abandons a checkout and
    starts again is still funding the same gig, and two rows here would make
    "is this job funded?" ambiguous at exactly the moment it must not be.
    """

    job = models.OneToOneField(Job, on_delete=models.PROTECT, related_name="escrow")
    #: Snapshot of who is being paid. The job's ``assigned_worker`` could in
    #: principle change; the person whose Stripe account this money is destined
    #: for must not.
    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.PROTECT, related_name="escrow_payments"
    )

    #: Amounts frozen when the payment is created. If the platform fee changes
    #: next month, an in-flight gig must still settle on the terms both parties
    #: saw — recomputing at release would quietly change the deal.
    amount = models.DecimalField(max_digits=9, decimal_places=2)
    platform_fee = models.DecimalField(max_digits=9, decimal_places=2)
    worker_payout = models.DecimalField(max_digits=9, decimal_places=2)

    #: What was actually settled. Three columns, not one, and they are a
    #: different thing from the three above.
    #:
    #: The agreed figures are a snapshot of the deal: they must not move when
    #: the platform fee changes next month. These are what happened — and on a
    #: day that ended early they are smaller, because less was captured and the
    #: fee follows the capture down.
    #:
    #: Keeping only ``captured_amount`` left the split un-recorded, so every
    #: reader afterwards had to choose between a payout that was agreed and a
    #: capture that happened, with no way to say what the worker was actually
    #: paid. The notification chose wrong and told somebody they had received
    #: the full figure on a day that settled at two thirds of it.
    #:
    #: Null until release. A hold that has never been captured has no actual
    #: anything, and zero would be a claim rather than an absence.
    captured_amount = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )
    captured_fee = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )
    captured_payout = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )

    status = models.CharField(
        max_length=16,
        choices=EscrowStatus.choices,
        default=EscrowStatus.PENDING,
        db_index=True,
    )

    checkout_session_id = models.CharField(max_length=255, blank=True, db_index=True)
    #: Where that session sends the client. Stored rather than rebuilt, so a
    #: second press of Fund can hand back the checkout already open instead of
    #: opening another one — see ``start_funding``.
    checkout_url = models.URLField(max_length=500, blank=True)
    payment_intent_id = models.CharField(max_length=255, blank=True, db_index=True)

    #: How many times funding has been *started*, not how many times it
    #: succeeded. It exists to be part of the Stripe idempotency key: within
    #: one attempt a repeated call must return the same session, and a client
    #: who abandons checkout and comes back tomorrow must be able to get a new
    #: one. A counter is the smallest thing that says which of the two this is.
    funding_attempts = models.PositiveIntegerField(default=0)

    authorized_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{money(self.amount, 2)} for {self.job}"

    @property
    def is_held(self) -> bool:
        return self.status == EscrowStatus.AUTHORIZED

    @property
    def net_to_worker(self) -> Decimal:
        """What the worker receives, given what was actually captured."""
        from config import business_rules as rules

        if self.captured_amount is None:
            return self.worker_payout
        return rules.worker_payout_for(self.captured_amount)


class WebhookEvent(models.Model):
    """Every Stripe event id we have already handled.

    Stripe retries, and it does not promise to deliver an event only once. A
    replayed `payment_intent.succeeded` that released a payout a second time
    would be an unrecoverable mistake, so the id is the primary key and
    handling is a no-op if it is already there.
    """

    event_id = models.CharField(max_length=255, primary_key=True)
    event_type = models.CharField(max_length=80)
    received_at = models.DateTimeField(auto_now_add=True)
    payload_summary = models.CharField(max_length=500, blank=True)

    #: When the work finished. Null means somebody is still doing it — and that
    #: distinction is the difference between this row being a receipt and being
    #: a lease.
    #:
    #: Without it, a duplicate delivery arriving mid-flight had one answer
    #: available: 200, meaning "already handled". If the run it collided with
    #: then failed, that 200 had already told Stripe the event was delivered
    #: and no retry was coming. The event was lost — the opposite failure from
    #: the one this table exists to prevent, and the quieter one.
    handled_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ("-received_at",)

    @property
    def is_handled(self) -> bool:
        return self.handled_at is not None

    def __str__(self) -> str:
        state = "handled" if self.is_handled else "in flight"
        return f"{self.event_type} {self.event_id} ({state})"
