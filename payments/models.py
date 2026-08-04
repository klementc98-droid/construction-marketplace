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

from accounts.models import WorkerProfile
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
    account_id = models.CharField(max_length=64, unique=True)

    #: Mirrors of Stripe's flags, refreshed from `account.updated` webhooks and
    #: whenever the worker returns from onboarding. Never authoritative.
    details_submitted = models.BooleanField(default=False)
    charges_enabled = models.BooleanField(default=False)
    payouts_enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.worker.user} ({self.account_id})"

    @property
    def is_ready(self) -> bool:
        """Can this worker actually receive money?

        Both flags, not just one: an account can be able to take charges while
        payouts are still blocked pending documents, and paying into it would
        strand the worker's money inside Stripe.
        """
        return self.charges_enabled and self.payouts_enabled


class EscrowStatus(models.TextChoices):
    PENDING = "pending", "Awaiting payment"
    """A checkout session exists; the client has not completed it."""

    AUTHORIZED = "authorized", "Funds held"
    """The card is authorised and the money is committed but not taken."""

    RELEASED = "released", "Released to worker"
    """Captured. Terminal."""

    REFUNDED = "refunded", "Returned to client"
    """The authorisation was cancelled or the charge refunded. Terminal."""

    FAILED = "failed", "Failed"
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

    #: What was actually captured. Differs from ``amount`` when phase 5
    #: releases a prorated amount for a job that ended early.
    captured_amount = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )

    status = models.CharField(
        max_length=16,
        choices=EscrowStatus.choices,
        default=EscrowStatus.PENDING,
        db_index=True,
    )

    checkout_session_id = models.CharField(max_length=255, blank=True, db_index=True)
    payment_intent_id = models.CharField(max_length=255, blank=True, db_index=True)

    authorized_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"${self.amount} for {self.job}"

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

    class Meta:
        ordering = ("-received_at",)

    def __str__(self) -> str:
        return f"{self.event_type} {self.event_id}"
