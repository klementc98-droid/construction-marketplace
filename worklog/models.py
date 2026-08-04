"""What actually happened on site, and what it means for the money.

Three records, each written once and then read as evidence:

* :class:`CheckIn` — the worker arrived. Carries GPS if the phone offered it.
* :class:`Completion` — the day finished, in full or early, and how many hours
  it ran. This is what the payout is computed from.
* :class:`Dispute` — somebody objected. Freezes the money until a human looks.

Deadlines are stored, not recomputed. If the approval window is shortened next
month, a job already waiting must still get the window it was promised —
recomputing on read would silently move a deadline that a worker is counting
on, which is exactly the sort of quiet change that money code must not make.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from math import asin, cos, radians, sin, sqrt

from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import WorkerProfile
from config import business_rules as rules
from core.models import TimestampedModel
from jobs.models import Job

EARTH_RADIUS_M = 6_371_000


def metres_between(lat1, lon1, lat2, lon2) -> int:
    """Great-circle distance, rounded to whole metres.

    Haversine rather than anything fancier: at the scale of "is this person at
    the right building?", the error from treating the earth as a sphere is
    metres, and the GPS reading is already worse than that.
    """
    p1, p2 = radians(float(lat1)), radians(float(lat2))
    dp = p2 - p1
    dl = radians(float(lon2)) - radians(float(lon1))
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return int(2 * EARTH_RADIUS_M * asin(sqrt(a)))


class CheckIn(TimestampedModel):
    """The worker says they are on site."""

    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="check_in")
    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="check_ins"
    )
    arrived_at = models.DateTimeField(default=timezone.now)

    #: Whatever the browser gave us, if the worker allowed it. Absent is normal
    #: and carries no penalty — see the note on ``looks_on_site``.
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    accuracy_m = models.PositiveIntegerField(null=True, blank=True)

    #: Distance from the site, when both ends have coordinates.
    distance_m = models.PositiveIntegerField(null=True, blank=True)

    #: Tri-state on purpose. True/False are "we checked and it looked
    #: right/odd"; NULL is "we could not check". Never a gate: GPS on a job
    #: site is unreliable — steel, basements, cheap handsets — and a worker who
    #: is genuinely present must not be blocked because their phone put them
    #: across the street. It exists so a dispute has something to read.
    looks_on_site = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ("-arrived_at",)

    def __str__(self) -> str:
        return f"{self.worker.user} on site for {self.job}"

    def evaluate_location(self) -> None:
        """Fill in distance and the soft verdict, if both ends have a fix."""
        job_has_site = (
            self.job.site_latitude is not None and self.job.site_longitude is not None
        )
        if not (job_has_site and self.latitude is not None and self.longitude is not None):
            self.distance_m = None
            self.looks_on_site = None
            return
        self.distance_m = metres_between(
            self.latitude, self.longitude, self.job.site_latitude, self.job.site_longitude
        )
        # The phone's own error is added to the allowance rather than ignored:
        # a 400 m accuracy circle genuinely cannot tell you more than that.
        allowance = rules.CHECKIN_GEOFENCE_RADIUS_M + (self.accuracy_m or 0)
        self.looks_on_site = self.distance_m <= allowance


class EndedBy(models.TextChoices):
    WORKER = "worker", "Worker"
    CLIENT = "client", "Client"


class Completion(TimestampedModel):
    """The day is over. How long it ran, and what that is worth."""

    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="completion")
    finished_at = models.DateTimeField(default=timezone.now)

    #: Hours actually worked. For a normal full day this is the gig's booked
    #: hours; for an early finish it is what the flagging party reported.
    hours_worked = models.DecimalField(max_digits=4, decimal_places=1)

    ended_early = models.BooleanField(default=False)
    ended_early_by = models.CharField(
        max_length=8, choices=EndedBy.choices, blank=True
    )
    early_end_note = models.CharField(max_length=300, blank=True)

    #: The amount this completion entitles the worker to, frozen here. Computed
    #: once from the hours and the guaranteed minimum in force at the time.
    payable_amount = models.DecimalField(max_digits=9, decimal_places=2)

    #: When the money moves on its own if nobody acts. For a full day this is
    #: the client's approval window; for an early finish, the shorter dispute
    #: window. Stored so a later change to the rules cannot move it.
    settles_at = models.DateTimeField(db_index=True)

    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-finished_at",)

    def __str__(self) -> str:
        kind = "ended early" if self.ended_early else "completed"
        return f"{self.job} {kind} — ${self.payable_amount}"

    @property
    def is_overdue(self) -> bool:
        return self.settled_at is None and timezone.now() >= self.settles_at

    @property
    def window_label(self) -> str:
        return "Dispute window" if self.ended_early else "Approval window"


def payable_for(job: Job, hours_worked: Decimal) -> Decimal:
    """What a worker is owed for ``hours_worked`` on ``job``.

    Three rules, in order:

    1. Pay is prorated against the booked day — half a day is half the price.
    2. Floored at the guaranteed minimum hours. Someone who travelled across
       town and was sent home after twenty minutes is not paid for twenty
       minutes; that floor is the whole reason a worker can risk the trip.
    3. Capped at the full price. Working late does not silently charge the
       client more than the number both sides agreed to, and it must never
       exceed what is actually held in escrow.
    """
    booked = Decimal(job.gig_hours or 0)
    total = Decimal(job.fixed_pay or 0)
    if booked <= 0:
        return total

    hours = max(Decimal(hours_worked), Decimal(rules.MINIMUM_GUARANTEED_HOURS))
    hours = min(hours, booked)
    return (total * hours / booked).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class DisputeStatus(models.TextChoices):
    OPEN = "open", "Awaiting review"
    RESOLVED_WORKER = "resolved_worker", "Resolved — paid to worker"
    RESOLVED_CLIENT = "resolved_client", "Resolved — refunded to client"


class Dispute(TimestampedModel):
    """Somebody objected. The money stops until a person decides.

    Deliberately has no timer out of it. An auto-resolve from a dispute would
    defeat the point of raising one — see the note in ``core.state_machine``.
    Phase 7 builds the queue that works through these; this is the record it
    will read.
    """

    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="dispute")
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="disputes_raised",
    )
    reason = models.TextField(max_length=2000)
    status = models.CharField(
        max_length=20, choices=DisputeStatus.choices,
        default=DisputeStatus.OPEN, db_index=True,
    )

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="disputes_resolved",
    )
    resolution_note = models.TextField(max_length=2000, blank=True)
    #: What was actually released to the worker, if anything. Null until
    #: resolved.
    resolved_amount = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Dispute on {self.job}"

    @property
    def is_open(self) -> bool:
        return self.status == DisputeStatus.OPEN
