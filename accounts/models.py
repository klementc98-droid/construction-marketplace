"""Users, roles, and the two profile types.

The central modelling decision: **role is not a field on the user.** A user is
a worker because a WorkerProfile exists for them, and a client because a
ClientProfile does — and both can be true at once. This is the norm in trades,
not an edge case: the carpenter who subs out a slab pour on Tuesday is a
client on Tuesday and a worker on Wednesday.

A ``role`` column would have forced that person into two accounts, split their
reputation across both, and made "is this the same person?" unanswerable. It
is also the kind of choice that is nearly impossible to undo later, once
foreign keys throughout the system point at role-scoped user rows.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import formats, timezone
from django.utils.translation import gettext_lazy as _

from config import business_rules as rules
from core.models import Region, TimestampedModel, Trade


class UserManager(BaseUserManager):
    """Email-keyed manager. There are no usernames — Google is the identity."""

    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("Users must have an email address.")
        user = self.model(email=self.normalize_email(email), **extra)
        # Google-authenticated users have no usable password. set_password(None)
        # stores an unusable hash, which is what we want: it means the account
        # cannot be logged into by password even if local login is re-enabled.
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Superusers must have is_staff and is_superuser set.")
        return self._create(email, password, **extra)


def avatar_upload_path(instance: "User", filename: str) -> str:
    return f"avatars/{instance.pk}/{filename}"


class User(AbstractUser):
    """An account. Roles live in the profile models, not here."""

    username = None  # Google gives us an email; a username is dead weight.
    email = models.EmailField(unique=True)

    full_name = models.CharField(max_length=150, blank=True)

    #: The person's picture, on the person — not on either role profile. The
    #: same human is a worker on Tuesday and a client on Wednesday, and they do
    #: not have two faces. Uploading it once is also one less thing to redo.
    avatar = models.ImageField(upload_to=avatar_upload_path, blank=True, null=True)

    #: Avatar URL supplied by Google at sign-in. The fallback when nothing has
    #: been uploaded, kept separate so an upload is never silently reverted the
    #: next time Google's copy is refreshed.
    google_picture_url = models.URLField(blank=True)

    phone = models.CharField(max_length=32, blank=True)

    #: Optional. Stored as a date rather than a number so it does not quietly
    #: go stale, and only ever displayed as an age — a birthday is not
    #: something a job board needs to publish.
    date_of_birth = models.DateField(null=True, blank=True)

    #: One line under the name. The trades version of a bio headline:
    #: "Framing and finish carpentry, own tools."
    headline = models.CharField(max_length=120, blank=True)

    #: Which dashboard to land on for a user who holds both roles. A UI
    #: convenience only — it grants nothing. Permission always derives from
    #: whether the corresponding profile exists.
    last_active_role = models.CharField(
        max_length=16,
        blank=True,
        choices=(("worker", "Worker"), ("client", "Client")),
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        ordering = ("-date_joined",)

    def __str__(self) -> str:
        return self.full_name or self.email

    # -- roles -------------------------------------------------------------

    @property
    def is_worker(self) -> bool:
        return hasattr(self, "worker_profile")

    @property
    def is_client(self) -> bool:
        return hasattr(self, "client_profile")

    @property
    def roles(self) -> list[str]:
        return [r for r, on in (("worker", self.is_worker), ("client", self.is_client)) if on]

    @property
    def needs_role_selection(self) -> bool:
        """True for a freshly signed-in Google user with no profile yet."""
        return not self.roles

    def display_photo(self) -> str:
        """Best available picture: uploaded, then legacy, then Google's.

        Returns "" when there is nothing — templates fall back to initials
        rather than rendering a broken image.
        """
        if self.avatar:
            return self.avatar.url
        # Predates the move of the picture onto the user; still honoured so
        # nobody's existing photo silently disappears.
        if self.is_worker and self.worker_profile.photo:
            return self.worker_profile.photo.url
        return self.google_picture_url

    @property
    def initials(self) -> str:
        """Two letters for the avatar placeholder.

        Falls back to the email's local part only — initials taken from the
        domain would spell the provider, not the person ("MC" for
        me@example.com).
        """
        source = (self.full_name or "").strip()
        if not source:
            source = self.email.split("@")[0].replace(".", " ").replace("_", " ")
        parts = [p for p in source.split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def age(self) -> int | None:
        """Whole years, or ``None`` if they did not say."""
        if self.date_of_birth is None:
            return None
        today = timezone.localdate()
        had_birthday = (today.month, today.day) >= (
            self.date_of_birth.month,
            self.date_of_birth.day,
        )
        return today.year - self.date_of_birth.year - (0 if had_birthday else 1)

    @property
    def short_name(self) -> str:
        """First name where there is one — for buttons and greetings."""
        return (self.full_name or self.email).split()[0] if (self.full_name or self.email) else ""

    @property
    def profile_url(self) -> str:
        """Where clicking this person's name should land anyone.

        Public — profiles are meant to be looked at by the people deciding
        whether to work with you, not only by you. Prefers the worker page
        because that is the one carrying a track record; the two pages
        cross-link for anyone who is both.

        Empty string when they hold no role yet, so templates can render the
        name as plain text rather than a link to nowhere.
        """
        from django.urls import reverse

        if self.is_worker:
            return reverse("accounts:worker_detail", args=[self.worker_profile.pk])
        if self.is_client:
            return reverse("accounts:client_detail", args=[self.client_profile.pk])
        return ""


# ---------------------------------------------------------------------------
# Reputation
# ---------------------------------------------------------------------------


class ReputationMixin(models.Model):
    """Denormalised trust counters shared by both profile types.

    Counters rather than live aggregates over the review table: these are read
    on every profile view and every search result row, and recomputing an
    average over a growing review set on each render is the kind of thing that
    is fine at launch and painful at scale. Phase 6 owns keeping them true.

    Rates are exposed as ``None`` — never 0 — until there is enough history to
    mean anything. A brand-new worker showing "0% completion rate" is a lie
    that costs them their first job; the template renders "New" instead.
    """

    rating_sum = models.PositiveIntegerField(default=0)
    rating_count = models.PositiveIntegerField(default=0)

    #: Room for the fraud work that v1 explicitly defers. Nothing sets these
    #: yet; they exist so that adding suspicious-pattern detection later is a
    #: background job writing two columns, not a migration on a hot table.
    flagged_for_review = models.BooleanField(default=False, db_index=True)
    flagged_reason = models.CharField(max_length=255, blank=True)
    flagged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def average_rating(self) -> Decimal | None:
        if not self.rating_count:
            return None
        return (Decimal(self.rating_sum) / Decimal(self.rating_count)).quantize(Decimal("0.1"))

    @property
    def is_new(self) -> bool:
        """Too little history for percentage stats to be meaningful."""
        return self.completed_job_count < rules.MIN_JOBS_FOR_PUBLIC_STATS

    def record_rating(self, score: int) -> None:
        """Fold one review's score into the running average.

        Written with F() expressions rather than read-modify-write: two
        reviews landing on the same profile at the same moment would otherwise
        both read the old sum and one would be lost. The database does the
        addition, so concurrency is its problem and not ours.

        Kept on the mixin so both profile types get it from one place — the
        counters are the same two columns either way.
        """
        type(self).objects.filter(pk=self.pk).update(
            rating_sum=models.F("rating_sum") + score,
            rating_count=models.F("rating_count") + 1,
        )
        self.refresh_from_db(fields=["rating_sum", "rating_count"])

    def flag_for_review(self, reason: str) -> None:
        self.flagged_for_review = True
        self.flagged_reason = reason
        self.flagged_at = timezone.now()
        self.save(update_fields=["flagged_for_review", "flagged_reason", "flagged_at"])

    @property
    def completed_job_count(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class RateType(models.TextChoices):
    HOURLY = "hourly", _("Per hour")
    DAILY = "daily", _("Per day")


class AvailabilityStatus(models.TextChoices):
    AVAILABLE_NOW = "available_now", _("Available now")
    SPECIFIC_DAYS = "specific_days", _("Specific days")
    ONGOING = "ongoing", _("Open to ongoing work")
    UNAVAILABLE = "unavailable", _("Not currently available")


def cv_upload_path(instance: "WorkerProfile", filename: str) -> str:
    return f"cvs/{instance.user_id}/{filename}"


def photo_upload_path(instance: "WorkerProfile", filename: str) -> str:
    return f"profile-photos/{instance.user_id}/{filename}"


class WorkerProfile(ReputationMixin, TimestampedModel):
    """Someone offering labour."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="worker_profile")

    photo = models.ImageField(upload_to=photo_upload_path, blank=True, null=True)

    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name="workers")
    #: Free text within the region — "north side, will travel to Riverdale".
    #: Deliberately not structured in v1: over-modelling service areas before
    #: seeing how one city's workers describe them tends to produce a taxonomy
    #: nobody fits.
    service_area = models.CharField(max_length=200, blank=True)

    trades = models.ManyToManyField(Trade, related_name="workers")

    years_experience = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0)]
    )

    # -- rate --------------------------------------------------------------
    # Modelled as a range with an optional upper bound: rate_max NULL means a
    # single flat rate, so "$30/hr" and "$28-35/hr" are the same two columns
    # rather than two different shapes to handle everywhere.
    rate_type = models.CharField(
        max_length=16, choices=RateType.choices, default=RateType.HOURLY
    )
    rate_min = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    rate_max = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )

    availability_status = models.CharField(
        max_length=20,
        choices=AvailabilityStatus.choices,
        default=AvailabilityStatus.AVAILABLE_NOW,
    )
    availability_note = models.CharField(max_length=200, blank=True)

    #: Whether this worker wants a permanent position, not just day gigs.
    #:
    #: Separate from ``availability_status``, which answers "when can you
    #: work?". This answers "what kind of arrangement are you after?" — and a
    #: worker free tomorrow may still only want day work, while one who is
    #: booked solid this week may be exactly who to call about a full-time
    #: role. Conflating the two would hide both groups from the wrong search.
    #:
    #: Nullable on purpose: NULL means "never asked", which must not be shown
    #: to clients as "no". Existing profiles predate the question.
    open_to_full_time = models.BooleanField(
        null=True,
        blank=True,
        verbose_name="Open to a full-time job?",
        help_text="Full-time positions are posted as standing jobs, not day gigs.",
    )

    #: What this worker is after *right now*, in their own words.
    #:
    #: The structured fields say what they can do and when they are free; this
    #: says what they actually want this week — "second fix, prefer south side"
    #: — which is the thing a client scanning profiles is trying to match and
    #: the thing no dropdown ever captures.
    seeking = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="What are you looking for right now?",
        help_text="Shown at the top of your profile. Change it as often as you like.",
    )

    bio = models.TextField(blank=True, max_length=2000)

    cv = models.FileField(
        upload_to=cv_upload_path,
        blank=True,
        null=True,
        validators=[FileExtensionValidator(list(rules.ALLOWED_CV_EXTENSIONS))],
        help_text="Optional PDF résumé.",
    )

    # -- reputation counters (written by phases 4-6) -----------------------
    jobs_accepted = models.PositiveIntegerField(default=0)
    jobs_completed = models.PositiveIntegerField(default=0)
    jobs_disputed = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.user} (worker)"

    # -- rate display ------------------------------------------------------

    @property
    def rate_display(self) -> str:
        if self.rate_min is None:
            return _("Rate on request")
        # The unit is translated on its own because it is a unit, not a
        # fragment of a sentence — "/hr" carries its whole meaning alone, and
        # the surrounding string is only digits and punctuation.
        unit = _("hr") if self.rate_type == RateType.HOURLY else _("day")
        if self.rate_max and self.rate_max != self.rate_min:
            return f"${self.rate_min:,.0f}-${self.rate_max:,.0f}/{unit}"
        return f"${self.rate_min:,.0f}/{unit}"

    # -- trust stats -------------------------------------------------------

    @property
    def completed_job_count(self) -> int:
        return self.jobs_completed

    @property
    def completion_rate(self) -> Decimal | None:
        """Completed / accepted. ``None`` while the profile is still New."""
        if self.is_new or not self.jobs_accepted:
            return None
        return (Decimal(self.jobs_completed) / Decimal(self.jobs_accepted)).quantize(
            Decimal("0.01")
        )

    @property
    def dispute_rate(self) -> Decimal | None:
        if self.is_new or not self.jobs_accepted:
            return None
        return (Decimal(self.jobs_disputed) / Decimal(self.jobs_accepted)).quantize(
            Decimal("0.01")
        )

    @property
    def regulated_trades(self):
        """Trades this worker claims that call for a licence number."""
        return self.trades.filter(requires_license=True)

    # -- what they're after right now --------------------------------------

    @property
    def active_jobs(self):
        """Jobs this worker is committed to and has not finished.

        "Committed" starts at ACCEPTED rather than at escrow: from the worker's
        side, having said yes is what makes them unavailable, whether or not
        the client has funded it yet.
        """
        from core.state_machine import JobState

        return self.assigned_jobs.filter(
            state__in=[
                JobState.ACCEPTED,
                JobState.ESCROW_HELD,
                JobState.IN_PROGRESS,
                JobState.ENDED_EARLY,
                JobState.COMPLETED,
            ]
        ).select_related("trade", "client__user")

    @property
    def is_on_a_job(self) -> bool:
        return self.active_jobs.exists()

    @property
    def busy_until(self):
        """Last date this worker is committed, or ``None`` if not committed.

        Dates only. *Which* job somebody is on is between them and the client
        who hired them — a rival client browsing profiles has no business
        reading it off a page. What they legitimately need is when this person
        frees up, and that is a date.

        ``None`` with :attr:`has_open_ended_commitment` set means "committed,
        but nobody knows for how long" — a standing position has no end date.
        """
        dates = [
            job.gig_date for job in self.active_jobs if job.gig_date is not None
        ]
        return max(dates) if dates else None

    @property
    def has_open_ended_commitment(self) -> bool:
        """On a standing position, which has no finish date to quote."""
        return self.active_jobs.filter(gig_date__isnull=True).exists()

    @property
    def available_from(self):
        """The first day they are free again, if that is knowable."""
        from datetime import timedelta

        end = self.busy_until
        if end is None:
            return None
        return end + timedelta(days=1)

    @property
    def availability_headline(self) -> str:
        """One line answering "can I book this person, and when?".

        Derived rather than stored, so it cannot contradict the facts it
        summarises — a worker who is mid-job but still has "available now" set
        would otherwise read as free.
        """
        if self.availability_status == AvailabilityStatus.UNAVAILABLE:
            return _("Not taking work right now")

        if self.has_open_ended_commitment:
            return _("On a longer-term placement")

        end = self.busy_until
        if end is not None:
            today = timezone.localdate()
            if end < today:
                pass  # commitment has run its course; fall through to normal
            elif end == today:
                return _("Busy today, free from tomorrow")
            else:
                # One string with both dates in it, not "Busy until" + a date +
                # "free from" + a date. Greek does not order those words the way
                # English does, and a translator handed " — free from" on its own
                # has nothing to work with.
                return _("Busy until %(from_date)s — free from %(to_date)s") % {
                    "from_date": formats.date_format(end, "D j M"),
                    "to_date": formats.date_format(self.available_from, "D j M"),
                }

        if self.availability_status == AvailabilityStatus.AVAILABLE_NOW:
            return _("Available now")
        if self.availability_status == AvailabilityStatus.SPECIFIC_DAYS:
            upcoming = self.upcoming_dates
            if upcoming:
                # formats.date_format rather than strftime: "%-d" is a glibc
                # extension and raises on Windows.
                shown = ", ".join(
                    formats.date_format(entry.date, "D j M") for entry in upcoming[:3]
                )
                more = len(upcoming) - 3
                if more > 0:
                    return _("Free %(days)s +%(count)s more") % {
                        "days": shown,
                        "count": more,
                    }
                return _("Free %(days)s") % {"days": shown}
            return _("Free on selected days")
        return _("Open to ongoing work")

    def is_free_on(self, day) -> bool | None:
        """Is this worker free on ``day``?

        ``None`` means "cannot say" — they work ad hoc rather than by declared
        dates, so absence of a booking is not evidence of availability. Used to
        tell a client which of their applicants can actually make the date,
        which is the question the applicant list exists to answer.
        """
        if self.availability_status == AvailabilityStatus.UNAVAILABLE:
            return False
        if self.active_jobs.filter(gig_date=day).exists():
            return False
        if self.has_open_ended_commitment:
            return False
        end = self.busy_until
        if end is not None and day <= end:
            return False
        if self.availability_status == AvailabilityStatus.SPECIFIC_DAYS:
            return self.availability_dates.filter(date=day).exists()
        if self.availability_status == AvailabilityStatus.AVAILABLE_NOW:
            return True
        return None

    @property
    def upcoming_dates(self):
        """Only the days that have not already gone by."""
        return list(self.availability_dates.filter(date__gte=timezone.localdate()))

    @property
    def is_open_to_offers(self) -> bool:
        """Worth a client's time to contact, right now."""
        return (
            self.availability_status != AvailabilityStatus.UNAVAILABLE
            and not self.is_on_a_job
        )

    @property
    def is_bookable_later(self) -> bool:
        """Busy now, but with a known date they come free.

        Worth its own flag because "booked this week" is a very different
        answer from "not interested" — a client planning next week's crew wants
        to talk to these people, and hiding them would lose the match.
        """
        return (
            self.availability_status != AvailabilityStatus.UNAVAILABLE
            and self.available_from is not None
        )

    @property
    def can_receive_offers(self) -> bool:
        """May a client send this person a direct offer for a dated gig?

        Broader than :attr:`is_open_to_offers` on purpose. An offer names a
        date, so somebody booked until Friday is still worth offering next
        Monday to — refusing that would lose exactly the bookings a dated
        marketplace exists to make. The one answer taken at face value is
        "not taking work right now", which is a person saying no in advance.

        Whether they are actually free on the day is a separate question, and
        :meth:`is_free_on` answers it where the worker has told us enough to
        say. The offer form warns; it does not block, because a worker who has
        not listed dates is not thereby unavailable.
        """
        return self.availability_status != AvailabilityStatus.UNAVAILABLE

    @property
    def availability_tone(self) -> str:
        """"ok" / "soon" / "off" — which colour the availability dot takes.

        Derived here rather than reconstructed from three properties in a
        template, so the dot can never disagree with the headline beside it.
        """
        if self.availability_status == AvailabilityStatus.UNAVAILABLE:
            return "off"
        if self.is_on_a_job:
            return "soon" if self.available_from is not None else "off"
        return "ok"


class AvailabilityDate(models.Model):
    """One specific day a worker has marked themselves free.

    Only meaningful when ``availability_status`` is SPECIFIC_DAYS. Stored as
    rows rather than a JSON blob so that phase 2 can answer "who is free on
    the 14th?" with a join — which is precisely the query a date-specific gig
    marketplace is built around.
    """

    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="availability_dates"
    )
    date = models.DateField(db_index=True)

    class Meta:
        unique_together = ("worker", "date")
        ordering = ("date",)

    def __str__(self) -> str:
        return f"{self.worker.user} free on {self.date}"


class TradeLicense(TimestampedModel):
    """A self-reported licence number for a regulated trade.

    NOT VERIFIED IN V1. Anyone can type anything here. It is displayed as
    self-reported and must never be presented to clients as though the
    platform has checked it — that would be a claim we cannot stand behind,
    and in the regulated trades it is the kind of claim that ends up in court.

    TODO(v2): manual admin verification. The three columns below are the
    intended shape — an admin opens the licence, checks it against the state
    registry, and stamps it. Nothing writes them yet.
    """

    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="licenses"
    )
    trade = models.ForeignKey(Trade, on_delete=models.CASCADE, related_name="licenses")
    number = models.CharField(max_length=80)

    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    verification_note = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ("worker", "trade")

    def __str__(self) -> str:
        return f"{self.trade} licence for {self.worker.user}"

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None


def portfolio_upload_path(instance: "PortfolioPhoto", filename: str) -> str:
    return f"portfolio/{instance.worker.user_id}/{filename}"


class PortfolioPhoto(TimestampedModel):
    """A photo of past work."""

    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="portfolio_photos"
    )
    image = models.ImageField(upload_to=portfolio_upload_path)
    caption = models.CharField(max_length=140, blank=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("display_order", "created_at")

    def __str__(self) -> str:
        return self.caption or f"Photo {self.pk}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ClientProfile(ReputationMixin, TimestampedModel):
    """Someone hiring labour."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="client_profile")

    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name="clients")
    location = models.CharField(max_length=200, blank=True)

    #: Optional — plenty of clients are individual homeowners, not companies.
    company_name = models.CharField(max_length=150, blank=True)

    # -- reputation counters (written by phases 4-6) -----------------------
    jobs_posted = models.PositiveIntegerField(default=0)
    jobs_completed = models.PositiveIntegerField(default=0)
    jobs_cancelled = models.PositiveIntegerField(default=0)

    #: Approval speed is tracked as a running total plus a count rather than a
    #: stored average, so a new data point is one UPDATE and never needs the
    #: history re-read.
    approval_seconds_total = models.BigIntegerField(default=0)
    approvals_counted = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.company_name or self.user} (client)"

    @property
    def completed_job_count(self) -> int:
        return self.jobs_completed

    @property
    def cancellation_rate(self) -> Decimal | None:
        if self.is_new or not self.jobs_posted:
            return None
        return (Decimal(self.jobs_cancelled) / Decimal(self.jobs_posted)).quantize(
            Decimal("0.01")
        )

    # -- what they're hiring for right now ---------------------------------

    @property
    def open_jobs(self):
        """Everything this client currently has out for applications.

        Direct offers are excluded. This drives the public "Hiring for 2 gigs"
        headline, and a gig written for one named worker is not something a
        reader of this profile can apply to — counting it would promise work
        that does not exist and reveal that a private approach is in flight.
        """
        from core.state_machine import JobState

        return self.jobs.filter(
            state=JobState.POSTED, is_private=False
        ).select_related("trade")

    @property
    def hiring_headline(self) -> str:
        """One line answering "is this client actually hiring?"."""
        count = self.open_jobs.count()
        if not count:
            return "Not hiring right now"
        gigs = self.open_jobs.filter(job_type="gig").count()
        if gigs and gigs == count:
            return f"Hiring for {count} gig{'s' if count > 1 else ''}"
        if gigs:
            return f"Hiring — {gigs} gig{'s' if gigs > 1 else ''} and {count - gigs} position{'s' if count - gigs > 1 else ''}"
        return f"Hiring for {count} position{'s' if count > 1 else ''}"

    @property
    def is_hiring(self) -> bool:
        return self.open_jobs.exists()

    @property
    def average_approval_hours(self) -> Decimal | None:
        """Mean time from job completion to payment approval.

        The stat workers actually care about when deciding whether to take a
        client's gig: not whether they pay, but how long they sit on it.
        """
        if self.is_new or not self.approvals_counted:
            return None
        seconds = Decimal(self.approval_seconds_total) / Decimal(self.approvals_counted)
        return (seconds / Decimal(3600)).quantize(Decimal("0.1"))
