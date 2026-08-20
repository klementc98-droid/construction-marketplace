"""Job posts and applications.

Two kinds of post live in ONE table, distinguished by ``job_type``:

* **Standing** — an open position. "Carpenter, ongoing, $30-38/hr." Workers
  apply, the client picks one, and money never moves through the platform.
* **Gig** — one dated shift. "$90 for 8 hours on the 14th." Once a worker is
  confirmed this becomes the thing escrow is built around.

One table rather than two, because the pieces that dominate the product —
browse, search, filter, apply, message, rate — are identical for both. Two
tables would mean two of every query, view, and template, to buy a handful of
nullable columns. The columns each type does not use are null, and
:meth:`Job.clean` plus the database constraints below make "gig with no date"
or "standing position with a fixed price" unrepresentable rather than merely
discouraged.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _, ngettext

from accounts.models import ClientProfile, RateType, WorkerProfile
from config import business_rules as rules
from core.models import Region, TimestampedModel, Trade
from core.money import money
from core.state_machine import JobState, state_tone


class JobType(models.TextChoices):
    STANDING = "standing", _("Standing position")
    GIG = "gig", _("One-day gig")


class ExperienceWanted(models.TextChoices):
    """How much the person posting actually needs the applicant to know.

    The field the product exists for. This app is not a board where qualified
    tradespeople bid for work — it is a tradesperson looking for a pair of
    hands, and the person answering may be nineteen and have never held a
    trowel. "No experience needed" is the sentence that decides whether they
    apply at all, so it is a fact about the job rather than something buried in
    a paragraph of description that they will read as "not for me".

    Three answers rather than a checkbox, because "some" is the honest middle
    and collapsing it into either extreme loses the jobs most people can
    actually take.
    """

    NONE = "none", _("No experience needed — will show you")
    SOME = "some", _("Some experience helps, not essential")
    SKILLED = "skilled", _("Needs someone who knows the trade")


class PositionType(models.TextChoices):
    """How long a standing position is expected to last.

    Free-form duration text would be unfilterable, and "how long is this for?"
    is the second question every worker asks after the rate.
    """

    TEMPORARY = "temporary", _("Temporary / short term")
    ONGOING = "ongoing", _("Ongoing")
    FULL_TIME = "full_time", _("Full time")
    CONTRACT = "contract", _("Contract / project-based")


class JobQuerySet(models.QuerySet):
    """The filters phase 2 exists to provide, as reusable query pieces."""

    def open(self):
        """Posts still awaiting a worker.

        Not the same as "posts on the board" — a direct offer is also POSTED
        but belongs to one named worker. Anything rendering a public list wants
        :meth:`public`; this stays the state test so ownership views ("my open
        jobs") keep working.
        """
        return self.filter(state=JobState.POSTED)

    def public(self):
        """Open posts anyone may see and apply to.

        Direct offers are excluded. A gig written for one person is not a
        listing, and showing it on the board would both mislead everyone else
        and leak who is being offered what.
        """
        return self.open().filter(is_private=False)

    def for_trade(self, trade_slug: str | None):
        return self.filter(trade__slug=trade_slug) if trade_slug else self

    def for_experience(self, level: str | None):
        """Jobs asking for exactly this much of the person taking them.

        The one filter on this board that changes who applies rather than what
        they see. Somebody deciding whether this app is for them is asking
        exactly this question, and it should take one tap to answer.

        Exact match rather than "this level and below". The chip says *No
        experience needed*, so it has to mean the jobs that say that — a filter
        that quietly widens is a filter nobody can predict, and the reader
        would be left wondering why a job wanting three years is in the
        beginners list.
        """
        return self.filter(experience_wanted=level) if level in ExperienceWanted.values else self

    def for_type(self, job_type: str | None):
        return self.filter(job_type=job_type) if job_type in JobType.values else self

    def in_region(self, region_slug: str | None):
        return self.filter(region__slug=region_slug) if region_slug else self

    def matching(self, term: str | None):
        """Free-text search over the fields a worker actually scans."""
        if not term:
            return self
        return self.filter(
            models.Q(title__icontains=term)
            | models.Q(description__icontains=term)
            | models.Q(location__icontains=term)
        )

    def with_applicant_counts(self):
        """Annotate the count of still-live applications.

        Annotated rather than counted per row in the template, which would be
        one query per job on a client's list of posts.
        """
        return self.annotate(
            applicant_count=models.Count(
                "applications",
                filter=models.Q(applications__status=ApplicationStatus.APPLIED),
            )
        )


def booking_of(job, *, states=None):
    """Every job in this booking, oldest day first — or just this one.

    The counterpart to :func:`collapse_groups`. That one makes a booking read
    as one entry; this one makes it *behave* as one. Both exist because the
    split into a row per day is storage — each day carries its own escrow and
    its own sign-off — and nothing a person does to "the job" should have to
    know that. They agreed one booking, they finish one booking, they rate one
    booking.

    ``states`` narrows to the days a particular step may touch. Left off, every
    day of the booking comes back, whatever state it is in.
    """
    if not job.offer_group:
        return [job]
    jobs = Job.objects.filter(offer_group=job.offer_group)
    if states is not None:
        jobs = jobs.filter(state__in=states)
    return list(jobs.order_by("gig_date", "pk"))


def collapse_groups(jobs):
    """One entry per multi-day booking, rather than one per day.

    A three-day booking is three gigs in the database, and has to be: each day
    carries its own escrow, its own sign-off and its own expiry, and two days
    cannot share a row when either can be finished or called off while the
    other runs. That is a storage fact, not something a reader should have to
    look at — three near-identical cards differing only by date read as the
    same job posted three times by mistake.

    So the list shows the booking. The first day carries the entry and gains
    two attributes for the template: ``group_days``, the count, and
    ``group_dates``, every date in it. Everything else about the row — the
    title, the trade, the per-day pay — is the same on all of them by
    construction, so the first is a fair representative.

    Ungrouped jobs pass through untouched with ``group_days`` of 1.
    """
    seen: dict = {}
    out = []
    for job in jobs:
        if not job.offer_group:
            out.append(_group_start(job))
            continue
        first = seen.get(job.offer_group)
        if first is None:
            seen[job.offer_group] = _group_start(job)
            out.append(job)
        else:
            _group_absorb(first, job)
    return out


def _group_start(job):
    """Make this day the representative of its booking."""
    job.group_days = 1
    job.group_dates = [job.gig_date] if job.gig_date else []
    # Summed rather than multiplied out from the first day: the days of a
    # booking can end up on different numbers, because a counter is agreed
    # per day. Multiplying the representative would quietly under- or
    # over-state a total the reader is about to rely on.
    job.group_pay = job.fixed_pay or Decimal("0")
    job.group_hours = job.gig_hours or Decimal("0")
    return job


def _group_absorb(first, job):
    """Fold another day into the representative already standing for it."""
    first.group_days += 1
    first.group_pay += job.fixed_pay or Decimal("0")
    first.group_hours += job.gig_hours or Decimal("0")
    if job.gig_date:
        first.group_dates.append(job.gig_date)
        first.group_dates.sort()


def collapse_rows(rows, job_of):
    """The same collapse, for lists whose rows *wrap* a job rather than are one.

    An application and an offer are written per day, because the day is what
    carries the escrow and the sign-off. But nobody applied five times and
    nobody was offered five things: applying once applies to the booking, and a
    client who offered somebody a week offered them one week. So "You applied
    to" and "Offered to you" have to collapse for the same reason the board
    does — five rows differing only by date read as five jobs.

    The surviving row keeps everything of its own — status, who sent it, when —
    and the job it points at gains the ``group_*`` attributes the templates
    already read, so a collapsed row can say "5 days" instead of naming one.

    ``job_of`` pulls the job out of a row; the caller knows the shape.
    """
    seen: dict = {}
    out = []
    for row in rows:
        job = job_of(row)
        if not job.offer_group:
            _group_start(job)
            out.append(row)
            continue
        first = seen.get(job.offer_group)
        if first is None:
            seen[job.offer_group] = _group_start(job)
            out.append(row)
        else:
            _group_absorb(first, job)
    return out


def count_bookings(group_ids) -> int:
    """How many bookings these per-day rows amount to.

    A count shown beside a collapsed list has to be counted the same way the
    list is, or the badge promises four things and the page holds one. Rows
    without a group are their own booking and each count once.
    """
    seen = set()
    total = 0
    for group in group_ids:
        if group is None:
            total += 1
        elif group not in seen:
            seen.add(group)
            total += 1
    return total


class Job(TimestampedModel):
    """One post, of either kind."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name="jobs"
    )
    job_type = models.CharField(max_length=16, choices=JobType.choices, db_index=True)

    trade = models.ForeignKey(Trade, on_delete=models.PROTECT, related_name="jobs")
    title = models.CharField(max_length=140)

    #: What this job asks of the person taking it — see ExperienceWanted.
    #:
    #: Defaults to NONE, and that default is the product's opinion rather than
    #: a shrug: most of these jobs are a second pair of hands, the people this
    #: exists to reach have no experience to declare, and a board that assumes
    #: skill by default quietly turns them away before they have applied.
    experience_wanted = models.CharField(
        max_length=16,
        choices=ExperienceWanted.choices,
        default=ExperienceWanted.NONE,
        db_index=True,
    )
    description = models.TextField(max_length=4000)

    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name="jobs")
    #: Free text within the region, same reasoning as WorkerProfile.service_area.
    location = models.CharField(max_length=200, blank=True)

    #: Where the site actually is, if the client bothered to say. Used only to
    #: sanity-check a worker's check-in — a SOFT signal recorded for review,
    #: never a gate. Both null is the normal case and must stay harmless: the
    #: check-in works exactly the same, it just carries no distance.
    site_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    site_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )

    #: The single source of truth for "where is this job at?". Every change
    #: goes through core.state_machine — see the note there on why job and
    #: payment state are one machine and not two.
    state = models.CharField(
        max_length=20,
        choices=JobState.choices,
        default=JobState.POSTED,
        db_index=True,
    )

    # -- standing-position fields (null on gigs) ---------------------------
    rate_type = models.CharField(
        max_length=16,
        choices=RateType.choices,
        blank=True,
        help_text=_("Per hour or per day."),
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
        help_text=_("Leave blank for a single flat rate."),
    )
    position_type = models.CharField(
        max_length=16, choices=PositionType.choices, blank=True
    )

    # -- gig fields (null on standing positions) ---------------------------
    gig_date = models.DateField(null=True, blank=True, db_index=True)
    gig_hours = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.5"))],
        help_text=_("Expected length of the day."),
    )
    #: What the client pays, total, for the whole gig. A fixed price rather
    #: than rate x hours: it is the number both sides agree on up front, and
    #: it is the exact amount captured into escrow in phase 4.
    fixed_pay = models.DecimalField(
        max_digits=9,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )

    #: Which multi-day offer wrote this row, if one did.
    #:
    #: A three-day offer is three gigs — each day has its own escrow and its own
    #: sign-off, and cannot share a row with another. But some answers are about
    #: the arrangement rather than the day: a worker asked for cash who wants
    #: escrow instead means all three days, not Tuesday only. This is what lets
    #: such an answer find its siblings.
    #:
    #: NULL for anything posted on its own, which is most jobs.
    offer_group = models.UUIDField(null=True, blank=True, db_index=True)

    #: Whether the platform holds the money for this gig.
    #:
    #: Off by default. Most work here is arranged between two people who settle
    #: it themselves — cash on the day, an invoice, a regular they have worked
    #: with for years — and a deal does not need us in the middle of the money
    #: to be a deal. Escrow is offered to the pair who want it and is otherwise
    #: not in the way: nothing in the ordinary lifecycle of a job consults it.
    #:
    #: Only meaningful on a gig. A standing position never had escrow: it is
    #: paid at a rate over an open period with no single day to sign off.
    use_escrow = models.BooleanField(
        default=False,
        help_text=_("Hold the client's payment until the day is signed off."),
    )

    #: Set when the client picks someone. On a gig this is the worker escrow
    #: will pay; on a standing position it simply records who got the job.
    assigned_worker = models.ForeignKey(
        WorkerProfile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_jobs",
    )
    filled_at = models.DateTimeField(null=True, blank=True)

    #: Created as a direct offer to one worker, so it never appears on the
    #: public board. A flag rather than a separate model: it is the same gig,
    #: with the same escrow and the same lifecycle, that simply started as an
    #: approach instead of an advert. If the worker turns it down the client
    #: can clear the flag and the post becomes an ordinary listing.
    is_private = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_("Direct offers are not listed on the public board."),
    )

    objects = JobQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # Make the invalid shapes unrepresentable in the database, not
            # merely rejected by a form. Anything that writes a Job — the
            # admin, a migration, a future importer — is held to this.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        job_type="gig",
                        gig_date__isnull=False,
                        gig_hours__isnull=False,
                        fixed_pay__isnull=False,
                    )
                    | models.Q(
                        job_type="standing",
                        gig_date__isnull=True,
                        gig_hours__isnull=True,
                        fixed_pay__isnull=True,
                    )
                ),
                name="job_gig_fields_match_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(job_type="gig", rate_min__isnull=True, rate_max__isnull=True)
                    | models.Q(job_type="standing", rate_min__isnull=False)
                ),
                name="job_rate_fields_match_type",
            ),
            models.CheckConstraint(
                condition=models.Q(rate_max__isnull=True)
                | models.Q(rate_max__gte=models.F("rate_min")),
                name="job_rate_max_not_below_min",
            ),
            # One worker, one of each day. A gig is a dated shift, so two live
            # gigs on the same date for the same person is two clients each
            # believing they have them — and the first either of them learns of
            # it is a morning nobody turns up to.
            #
            # Partial, over the committed states only, and both halves of that
            # matter. A day that has been paid out or closed is history, and
            # history is allowed to contain the overlaps this prevents from
            # here on. A day that is merely *offered* is not a commitment
            # either — being sent two offers for Tuesday is ordinary, and the
            # answer to one of them is what makes the other impossible.
            #
            # In the database and not only in the view, because two clients can
            # confirm the same worker in the same second and neither view sees
            # the other. See ``clashing_dates`` in services for the half of
            # this that produces a sentence rather than an error.
            models.UniqueConstraint(
                fields=["assigned_worker", "gig_date"],
                condition=models.Q(
                    assigned_worker__isnull=False,
                    gig_date__isnull=False,
                    state__in=[
                        "accepted",
                        "escrow_held",
                        "in_progress",
                        "ended_early",
                        "completed",
                    ],
                ),
                name="one_booking_per_worker_per_day",
            ),
        ]
        indexes = [
            # The browse page's default query: open posts of a trade, newest
            # first; and the same scoped to a region.
            models.Index(fields=["state", "job_type", "trade"]),
            models.Index(fields=["region", "state"]),
        ]

    def __str__(self) -> str:
        return self.title

    # -- validation --------------------------------------------------------

    def clean(self) -> None:
        """Readable errors for the person filling in the form.

        The constraints above guarantee the shape; this explains it.
        """
        errors: dict[str, str] = {}

        if self.job_type == JobType.GIG:
            if self.gig_date is None:
                errors["gig_date"] = "A gig needs the date it is for."
            elif self._state.adding and self.gig_date < timezone.localdate():
                errors["gig_date"] = "That date has already passed."
            if self.gig_hours is None:
                errors["gig_hours"] = "How many hours is the day?"
            if self.fixed_pay is None:
                errors["fixed_pay"] = "What does the gig pay in total?"
        elif self.job_type == JobType.STANDING:
            if self.rate_min is None:
                errors["rate_min"] = "Workers filter on pay — give at least a minimum."
            if not self.rate_type:
                errors["rate_type"] = "Is the rate per hour or per day?"
            if not self.position_type:
                errors["position_type"] = "How long is the position for?"

        if self.rate_max is not None and self.rate_min is not None:
            if self.rate_max < self.rate_min:
                errors["rate_max"] = "The top of the range cannot be below the bottom."

        if errors:
            raise ValidationError(errors)

    # -- display -----------------------------------------------------------

    @property
    def is_gig(self) -> bool:
        return self.job_type == JobType.GIG

    @property
    def is_open(self) -> bool:
        return self.state == JobState.POSTED

    # -- escrow, or not ----------------------------------------------------

    @property
    def is_escrowed(self) -> bool:
        """Does the platform hold the money for this job?

        Asked instead of reading ``use_escrow`` directly, because the answer is
        two conditions and one of them is easy to forget: a standing position
        has no escrow whatever the flag says, since there is no single day to
        sign off and nothing is captured. Left to callers, that check gets made
        in some templates and not others.
        """
        return self.is_gig and self.use_escrow

    @property
    def awaiting_client_confirmation(self) -> bool:
        """The worker has said the work is done and it is the client's turn.

        The non-escrow equivalent of the approval window, minus the window:
        there is no hold to release and so no timer, which is why nothing here
        happens on its own. Mutual agreement or nothing.
        """
        return self.state == JobState.COMPLETED and not self.is_escrowed

    def review_direction_for(self, user):
        """Which way a review by ``user`` would point, or None if they cannot.

        The subject is never stored on a Review — a job has one client and one
        assigned worker, so the direction plus the job says who is being rated.
        This is the other half of that: turning a viewer into a direction.
        """
        from .models import ReviewDirection  # local: defined below this class

        if self.client and self.client.user_id == user.pk:
            return ReviewDirection.CLIENT_ON_WORKER
        if self.assigned_worker and self.assigned_worker.user_id == user.pk:
            return ReviewDirection.WORKER_ON_CLIENT
        return None

    def can_be_reviewed_by(self, user) -> bool:
        """Is a review from this person due, and not already written?

        Being finished is the whole test. It used to also require the gig date
        to have passed, which sounded right and was wrong: a job only reaches
        PAID_OUT or CLOSED because both sides said the work happened — the
        client released the money, or both pressed "job done". Asking the
        calendar to agree after that left people staring at a finished job with
        no way to rate it, which is exactly what it did.

        The protection that matters is still here, in is_finished: rating
        before the money has moved would put a thumb on the scale of the
        payment itself.
        """
        if not getattr(user, "is_authenticated", False) or not self.is_finished:
            return False
        direction = self.review_direction_for(user)
        if direction is None:
            return False
        return self.booking_review(direction) is None

    def review_from(self, user):
        """Their review of this booking, if they have written one."""
        if not getattr(user, "is_authenticated", False):
            return None
        direction = self.review_direction_for(user)
        if direction is None:
            return None
        return self.booking_review(direction)

    def booking_review(self, direction):
        """The review of this booking in that direction, whichever day holds it.

        Asked of the booking rather than of the row, because that is where the
        answer is: ``review_create`` deliberately writes one rating for the
        whole booking and stores it against the first day, so a nine-day job
        rated once has one Review row and eight days with none.

        Looking only at ``self.reviews`` therefore told eight of those days
        that a rating was still owed. The reader saw it as a badge that would
        not clear — "1 completed job to review", still there after reviewing
        it, pointing at a page whose only button led to "You've already rated
        this one." The write side treated the booking as one thing; this is
        the read side agreeing.
        """
        reviews = Review.objects.filter(direction=direction)
        if self.offer_group:
            return reviews.filter(job__offer_group=self.offer_group).first()
        return reviews.filter(job=self).first()

    @property
    def is_finished(self) -> bool:
        """Work happened and the job is over, by either route.

        What a review hangs off — see :class:`Review`. Cancelled, expired and
        refunded are terminal too, but nothing was done on them, so there is
        nothing for either side to rate.
        """
        return self.state in (JobState.PAID_OUT, JobState.CLOSED)

    #: Which of the eight trade icons stands for this job, by trade slug. The
    #: mapping is here rather than in a template because a template that has to
    #: know the slugs is a template nobody can add a trade to.
    TRADE_ICONS = {
        "general-labor": "i-hardhat",
        "electrician": "i-bolt",
        "plumber": "i-wrench",
        "hvac": "i-wrench",
        "carpenter": "i-saw",
        "drywall-framing": "i-saw",
        "mason-concrete": "i-brick",
        "roofer": "i-brick",
        "painter": "i-roller",
        "welder": "i-hammer",
        "landscaping-excavation": "i-trade",
        "heavy-equipment-operator": "i-trade",
    }

    @property
    def trade_icon(self) -> str:
        """Falls back rather than disappearing: a new trade gets the generic
        mark, not a hole where the icon should be."""
        return self.TRADE_ICONS.get(self.trade.slug, "i-trade")

    @property
    def experience_tone(self) -> str:
        """go / steady / stop, for the badge that decides whether somebody
        reads the rest of the card.

        Three levels rather than a yes/no, because "some experience helps" is
        the honest middle and collapsing it either way loses the jobs most
        people can actually take.
        """
        return {
            ExperienceWanted.NONE: "go",
            ExperienceWanted.SOME: "steady",
            ExperienceWanted.SKILLED: "stop",
        }.get(self.experience_wanted, "steady")

    @property
    def teaches_on_the_job(self) -> bool:
        """Would somebody with nothing behind them be welcome here?

        Read by the board and by the job page, because it is the fact that
        decides whether the person this app is for reads any further.
        """
        return self.experience_wanted == ExperienceWanted.NONE

    def get_absolute_url(self) -> str:
        """Where this job lives. Django's convention, and the one thing every
        template that links back to a job can rely on without importing a URL
        name it might get wrong."""
        return reverse("jobs:detail", args=[self.pk])

    @property
    def state_tone(self) -> str:
        """Badge modifier for this job's state — see core.state_machine.

        Display only. It exists so that every template writes
        ``badge state-{{ job.state_tone }}`` and a state means the same colour
        on the board, in Mine, in a thread and on the job page. Eleven states
        hand-mapped in six templates is how they drift apart.
        """
        return state_tone(self.state)

    @property
    def pay_display(self) -> str:
        """One string for either kind of post, for list rows and cards."""
        if self.is_gig:
            if self.fixed_pay is None:
                return _("Pay not set")
            hours = (self.gig_hours or Decimal("0")).normalize()
            return _("%(pay)s for %(hours)s hours") % {
                "pay": money(self.fixed_pay),
                "hours": hours,
            }
        if self.rate_min is None:
            return _("Rate on request")
        unit = _("hr") if self.rate_type == RateType.HOURLY else _("day")
        if self.rate_max and self.rate_max != self.rate_min:
            return f"{money(self.rate_min)}-{money(self.rate_max)}/{unit}"
        return f"{money(self.rate_min)}/{unit}"

    @property
    def implied_hourly(self) -> Decimal | None:
        """What a gig works out to per hour.

        Shown beside the fixed price because "$90 for 8 hours" is the number
        the client thinks in, and "$11.25/hr" is the number the worker needs in
        order to compare it against anything else on the board.
        """
        if not self.is_gig or not self.fixed_pay or not self.gig_hours:
            return None
        return (self.fixed_pay / self.gig_hours).quantize(Decimal("0.01"))

    @property
    def starts_in(self) -> str:
        """How soon a gig is, in words. Empty for anything not imminent.

        A gig tomorrow and a gig in three weeks are very different propositions
        and the raw date makes you do the arithmetic. Only rendered when it is
        close enough to matter, so the badge means something when it appears.
        """
        if not self.is_gig or self.gig_date is None:
            return ""
        days = (self.gig_date - timezone.localdate()).days
        if days < 0:
            return _("Date passed")
        if days == 0:
            return _("Today")
        if days == 1:
            return _("Tomorrow")
        if days <= 6:
            return ngettext("In %(days)s day", "In %(days)s days", days) % {
                "days": days
            }
        return ""

    @property
    def is_urgent(self) -> bool:
        """Happening within 48 hours and still nobody booked."""
        if not self.is_gig or self.gig_date is None or not self.is_open:
            return False
        return 0 <= (self.gig_date - timezone.localdate()).days <= 2

    @property
    def is_past_gig(self) -> bool:
        """An open gig whose date has gone by. Nothing sweeps these yet."""
        return (
            self.is_gig
            and self.is_open
            and self.gig_date is not None
            and self.gig_date < timezone.localdate()
        )

    def application_from(self, worker: WorkerProfile | None) -> "Application | None":
        if worker is None:
            return None
        return self.applications.filter(worker=worker).first()

    @property
    def pending_offer(self) -> "Offer | None":
        """The offer still waiting on an answer, if there is one."""
        return self.offers.filter(status=OfferStatus.PENDING).first()

    # -- negotiation -------------------------------------------------------
    # Terms can be haggled on any open gig, not only on a direct offer, so the
    # negotiation hangs off the job rather than off the offer. A public gig may
    # have several running at once — one per interested worker.

    def live_counter_from(self, worker) -> "Counter | None":
        """The terms this worker currently has on the table for this job."""
        if worker is None:
            return None
        return self.counters.filter(
            worker=worker, status=CounterStatus.PENDING
        ).first()

    @property
    def live_counters(self):
        """Everyone currently asking for something other than the posted terms.

        What the client sees on the applicants page: the same list of people,
        with the ones who named their own price showing it.
        """
        return self.counters.filter(status=CounterStatus.PENDING).select_related(
            "worker__user"
        )

    @property
    def is_negotiable(self) -> bool:
        """Can terms be haggled on this job at all?

        Gigs only. A standing position has no single price and no single day —
        there is nothing here to put a number against, and the rate range on
        the post is already an invitation to discuss it.
        """
        return self.is_gig and self.is_open

    def can_negotiate(self, worker) -> bool:
        """May this worker propose terms right now?

        Not the client's own job, and not one they have already had an answer
        to. Deliberately allowed even without an existing application: naming
        your price *is* putting yourself forward, and making someone apply at a
        price they have already said no to first is a pointless step.
        """
        if worker is None or not self.is_negotiable:
            return False
        if self.client.user_id == worker.user_id:
            return False
        return self.is_visible_to(worker.user)

    def is_visible_to(self, user) -> bool:
        """May this person see the post at all?

        A public post that is still open is public — the board is browsable
        signed out and that is the point of a marketplace. Two things narrow
        it, and they are different questions.

        **Private posts** are written for one named worker and were never on
        the board.

        **Taken posts** were, and stopped being. Once somebody has the work it
        is no longer an advertisement, it is an arrangement between two people
        — who is doing it, for how much, on which days, and where. It stayed
        world-readable at that URL, so anybody holding the link could read a
        booking they had nothing to do with, complete with the worker's name.
        Nobody browses to it: it is off the board, off the feed and out of
        search the moment it is taken. It leaks by being pasted, which is
        exactly the case a state-blind check cannot catch.

        The test is "has somebody got this", not "is it still open". A post
        that expired or was called off with nobody on it is a dead
        advertisement, not an arrangement — it was public while it stood and
        there is nothing in it to protect, so an old link to one still opens.

        People who *were* part of it keep their access. A worker who applied,
        or who was offered it and said no, has the job in their own lists and
        would otherwise get a 404 from their own history — see the assigned-to
        block in the template for what they still do not get to see.
        """
        arranged = (
            self.is_private
            or self.assigned_worker_id is not None
            or self.is_finished
        )
        if arranged:
            if not getattr(user, "is_authenticated", False):
                return False
            if self.client.user_id == user.pk:
                return True
            worker = getattr(user, "worker_profile", None)
            if worker is None:
                return False
            if self.assigned_worker_id == worker.pk:
                return True
            # Any offer or application, not just a live one: somebody who
            # declined should still be able to open the thing they declined,
            # and somebody passed over should still reach what they applied to.
            return (
                self.offers.filter(worker=worker).exists()
                or self.applications.filter(worker=worker).exists()
            )
        return True

    def parties_only(self, user) -> bool:
        """Is this one of the two people the job is actually between?

        Narrower than :meth:`is_visible_to`, which lets everyone who was ever
        involved read the page. Who ended up with the work is between the pair
        who agreed it — a rival applicant reading "assigned to X" off a job
        they lost is a different disclosure from being able to find the post
        they applied to.
        """
        if not getattr(user, "is_authenticated", False):
            return False
        if self.client.user_id == user.pk:
            return True
        worker = getattr(user, "worker_profile", None)
        return worker is not None and self.assigned_worker_id == worker.pk


class OfferStatus(models.TextChoices):
    PENDING = "pending", _("Waiting on the worker")
    ACCEPTED = "accepted", _("Accepted")
    DECLINED = "declined", _("Declined")
    #: The client pulled it before the worker answered.
    WITHDRAWN = "withdrawn", _("Withdrawn")


class Offer(TimestampedModel):
    """A client approaching one worker with a gig, rather than advertising it.

    The mirror image of :class:`Application`: there, a worker puts themselves
    forward for a post; here, a client puts a post in front of a worker. Both
    end at the same place — a job with an ``assigned_worker`` — so an offer is
    modelled as a row against a real :class:`Job` rather than as a separate
    kind of thing with its own price and date fields. Accepting is then a
    state transition on a job that already exists, not a conversion step that
    has to copy six fields across and could get one of them wrong.

    Kept after it is answered, for the same reason applications are: a decline
    is evidence, and phase 6 computes acceptance rates from exactly this.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="offers")
    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="offers"
    )

    #: The client's covering note — why this person, and anything about the
    #: work that does not belong in the public description.
    note = models.TextField(
        max_length=1500,
        blank=True,
        help_text=_("Anything they should know before saying yes."),
    )
    #: The worker's answer in their own words. Optional: "no" is a complete
    #: answer and nobody should have to justify it to get out of the form.
    response_note = models.CharField(max_length=300, blank=True)

    status = models.CharField(
        max_length=16,
        choices=OfferStatus.choices,
        default=OfferStatus.PENDING,
        db_index=True,
    )
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # Re-offering the same job to the same person edits the offer you
            # have; it does not stack another on the pile.
            models.UniqueConstraint(
                fields=["job", "worker"], name="one_offer_per_worker_per_job"
            ),
            # At most one live offer per job. Two people cannot both be holding
            # an answer to the same gig, or both could accept and only one of
            # them would actually have it. Enforced in the database rather than
            # by the view, because the view is not the only thing that writes.
            models.UniqueConstraint(
                fields=["job"],
                condition=models.Q(status="pending"),
                name="one_pending_offer_per_job",
            ),
        ]
        indexes = [
            # The worker's "offers for me" list.
            models.Index(fields=["worker", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.job.client} -> {self.worker.user} re {self.job}"

    @property
    def is_pending(self) -> bool:
        return self.status == OfferStatus.PENDING

    @property
    def is_live(self) -> bool:
        """Pending, and the gig it is for has not been taken or called off."""
        return self.is_pending and self.job.is_open

    @property
    def free_on_date(self) -> bool | None:
        """Is the worker free on the day this offer is for?

        ``None`` means "cannot say" — see :meth:`WorkerProfile.is_free_on`.
        Shown to the client as a warning, never used to block: a worker who
        has not listed dates is not thereby unavailable.
        """
        if self.job.gig_date is None:
            return None
        return self.worker.is_free_on(self.job.gig_date)

    # -- negotiation -------------------------------------------------------
    # Counters belong to (job, worker), not to the offer — see Counter. These
    # read through to that pair so the offer templates do not have to know.

    @property
    def live_counter(self):
        """The proposal currently on the table, if the terms have moved."""
        return self.job.live_counter_from(self.worker)

    @property
    def awaiting_from(self) -> str | None:
        """Whose turn it is to answer, or ``None`` if the offer is closed.

        The single source of truth for which buttons either side sees. Derived
        rather than stored: a turn field would be a second thing to keep in
        step with the counters themselves, and the two could disagree.
        """
        if not self.is_live:
            return None
        counter = self.live_counter
        # No counter yet means the worker still owes an answer to the original.
        return counter.answered_by if counter else Party.WORKER

    def awaits(self, party: str) -> bool:
        return self.awaiting_from == party

    @property
    def counter_history(self) -> list:
        return list(self.job.counters.filter(worker=self.worker))

    @property
    def rounds(self) -> int:
        """How many times the terms have been re-proposed."""
        return self.job.counters.filter(worker=self.worker).count()

    @property
    def has_history(self) -> bool:
        return self.rounds > 0


def _day(value) -> str:
    """"Thu 14 Aug".

    Built by hand rather than with ``strftime("%a %-d %b")``: the no-padding
    flag is a glibc extension, and the Windows C library wants ``%#d``. This
    formats the same on every platform the app might run on.
    """
    return f"{value:%a} {value.day} {value:%b}" if value else "—"


class Party(models.TextChoices):
    """Who put a set of terms on the table.

    Deliberately not :class:`core.state_machine.Actor`, which also has SYSTEM
    and ADMIN. Only the two people in the deal may name a price, and a type
    that cannot express "the system countered" is the cheapest way to say so.
    """

    WORKER = "worker", _("Worker")
    CLIENT = "client", _("Client")


class CounterStatus(models.TextChoices):
    PENDING = "pending", _("On the table")
    ACCEPTED = "accepted", _("Agreed")
    DECLINED = "declined", _("Turned down")
    #: Replaced by a newer counter from the other side. Kept, not deleted —
    #: the sequence of numbers is the negotiation, and either party should be
    #: able to see how they got to the figure they are being asked to accept.
    SUPERSEDED = "superseded", _("Replaced by a later offer")


class Counter(TimestampedModel):
    """A revised set of terms on a gig, from either side.

    Hung off ``(job, worker)`` rather than off an :class:`Offer`, because
    haggling is not exclusive to direct offers. Any worker looking at any open
    gig on the board can say "I'll do it, but for $280", and the client can
    accept, come back, or leave it. A public gig therefore has as many
    negotiations running as it has interested workers — one per pair — while a
    direct offer has exactly one. Both are the same object.

    A negotiation is a chain rather than a field that gets overwritten. The job
    keeps the terms it was posted with until somebody actually agrees to new
    ones; a counter is a *proposal* about that job, so nothing on the job moves
    until it is accepted. That ordering matters because the job's ``fixed_pay``
    is what the client's card is eventually charged — a price that could drift
    while nobody had agreed to it would be a price nobody agreed to.

    Only the fields being changed are set. A worker happy with the date and the
    hours but not the money sends ``fixed_pay`` alone, and the rest is read
    through from the job.

    The whole exchange happens while the job is still ``posted``. No escrow
    exists yet, so there is no captured money to reconcile against a changing
    price, and :mod:`core.state_machine` needs no new states — accepting a
    counter is the same ``posted -> accepted`` move as accepting anything else.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="counters")
    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="counters"
    )
    proposed_by = models.CharField(max_length=8, choices=Party.choices, db_index=True)

    #: All nullable: a counter names only what it wants changed.
    fixed_pay = models.DecimalField(
        max_digits=9,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("1"))],
        help_text=_("Total for the day."),
    )
    gig_date = models.DateField(null=True, blank=True)
    gig_hours = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.5"))],
    )
    #: The third thing worth haggling over, alongside the money and the day.
    #: A worker offered cash-in-hand can come back asking for escrow without
    #: touching the price — which is the answer "yes, but not on trust", and
    #: the one a board built around held money should make easy to give.
    #:
    #: NULL means "not part of this counter", exactly like the others.
    use_escrow = models.BooleanField(null=True, blank=True)

    note = models.CharField(
        max_length=300,
        blank=True,
        help_text=_("Why — one line is plenty."),
    )

    status = models.CharField(
        max_length=12,
        choices=CounterStatus.choices,
        default=CounterStatus.PENDING,
        db_index=True,
    )
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            # One live proposal per person per job. Within a pair, negotiation
            # is turn-taking: if both sides could have terms on the table at
            # once, each could accept the other's and the job would end up with
            # two agreed prices. Across pairs it is deliberately unconstrained —
            # five workers may each be asking a different price for the same
            # gig, which is the whole point of putting it on the board.
            models.UniqueConstraint(
                fields=["job", "worker"],
                condition=models.Q(status="pending"),
                name="one_live_counter_per_worker_per_job",
            ),
        ]
        indexes = [
            models.Index(fields=["job", "status"]),
            models.Index(fields=["worker", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_proposed_by_display()} counter on {self.job_id}"

    @property
    def is_pending(self) -> bool:
        return self.status == CounterStatus.PENDING

    @property
    def answered_by(self) -> str:
        """The side that owes a reply to this proposal — the other one."""
        return Party.CLIENT if self.proposed_by == Party.WORKER else Party.WORKER

    @property
    def changes(self) -> list[tuple[str, str, str]]:
        """``(label, before, after)`` for every term this counter moves.

        Built for display. Somebody being asked to agree to a number should be
        shown what it was as well as what it would become — "$280" alone is not
        a proposal anyone can weigh.

        A property, not a method taking the job: templates cannot pass
        arguments, and a method that needs one fails *silently* there — the
        loop simply renders nothing, which is exactly the kind of missing
        information nobody notices until somebody has agreed to the wrong
        number. The counter can reach its own job; it should.
        """
        job = self.job
        rows = []
        if self.fixed_pay is not None and self.fixed_pay != job.fixed_pay:
            # money(), not a literal sign. core.money exists because the symbol
            # was written out by hand in a dozen places, and this was one of
            # the ones the sweep missed: the whole app said € and the single
            # screen asking somebody to agree to a number said $.
            rows.append((_("Pay"), money(job.fixed_pay), money(self.fixed_pay)))
        if self.gig_date is not None and self.gig_date != job.gig_date:
            rows.append((_("Date"), _day(job.gig_date), _day(self.gig_date)))
        if self.gig_hours is not None and self.gig_hours != job.gig_hours:
            hours = (job.gig_hours or Decimal("0")).normalize()
            rows.append((_("Hours"), f"{hours}", f"{self.gig_hours.normalize()}"))
        return rows

    def apply_to(self, job: "Job") -> list[str]:
        """Write the agreed terms onto the job. Returns the fields changed.

        Called only from the accept path, inside the same transaction that
        assigns the worker — the price and the person are one decision.
        """
        changed = []
        for field in ("fixed_pay", "gig_date", "gig_hours", "use_escrow"):
            value = getattr(self, field)
            if value is not None and value != getattr(job, field):
                setattr(job, field, value)
                changed.append(field)
        return changed


class ApplicationStatus(models.TextChoices):
    APPLIED = "applied", _("Applied")
    SELECTED = "selected", _("Selected")
    #: Set on the others when someone is chosen, so a worker gets a definite
    #: answer instead of an application that just goes quiet.
    PASSED_OVER = "passed_over", _("Not selected")
    WITHDRAWN = "withdrawn", _("Withdrawn")


class Application(TimestampedModel):
    """A worker putting themselves forward for a post.

    Kept after the client picks someone else: this history is what phase 6
    computes acceptance and completion rates against, and a passed-over or
    withdrawn application is evidence too.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    worker = models.ForeignKey(
        WorkerProfile, on_delete=models.CASCADE, related_name="applications"
    )
    message = models.TextField(
        max_length=1500,
        blank=True,
        help_text=_("Optional. What makes you right for this one?"),
    )
    status = models.CharField(
        max_length=16,
        choices=ApplicationStatus.choices,
        default=ApplicationStatus.APPLIED,
        db_index=True,
    )
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # One application per worker per job. Re-applying edits the one
            # you have; it does not stack another on the pile.
            models.UniqueConstraint(
                fields=["job", "worker"], name="one_application_per_worker_per_job"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker.user} -> {self.job}"

    @property
    def is_active(self) -> bool:
        return self.status == ApplicationStatus.APPLIED


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


class ReviewDirection(models.TextChoices):
    """Which way a review points.

    The subject is not stored. A job has exactly one client and exactly one
    assigned worker, so the direction plus the job identifies who is being
    rated — and a denormalised subject column is one more thing that can
    disagree with the job it hangs off.
    """

    CLIENT_ON_WORKER = "client_on_worker", _("Client rating the worker")
    WORKER_ON_CLIENT = "worker_on_client", _("Worker rating the client")


class Review(TimestampedModel):
    """One side's verdict on one finished job.

    Written only once the money has moved. Rating someone before they are paid
    would put a thumb on the scale of the payment itself — "give me five stars
    and I'll approve" is a threat the timing makes impossible.

    Both directions exist because both sides take a risk. A worker choosing
    between two clients wants to know which one approves promptly and which
    one argues, and that information only exists if workers can write it down.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="reviews")
    direction = models.CharField(max_length=20, choices=ReviewDirection.choices)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews_written",
    )
    rating = models.PositiveSmallIntegerField(
        validators=[
            MinValueValidator(rules.RATING_MIN),
            MaxValueValidator(rules.RATING_MAX),
        ]
    )
    #: Optional. A score with no words is still a data point, and forcing
    #: prose is how you get "good" a thousand times.
    comment = models.TextField(max_length=1000, blank=True)

    #: The booking this rates, copied down from the job at write time — the
    #: same denormalisation, for the same reason, as ``Notification.booking``.
    #:
    #: It exists to be constrained. "One rating per booking" was true only
    #: because one view happened to collapse to the first day before writing;
    #: the service underneath checked the day, and so did the database. A
    #: second rating on a nine-day booking was therefore always representable,
    #: and it happened — two ratings in each direction on one week's work, both
    #: folded into an average that is meant to say how many jobs somebody has
    #: been rated on.
    #:
    #: Null for a job that is not part of a booking, where the per-job
    #: constraint below is already the whole rule.
    booking = models.UUIDField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # One review per side per job. Re-rating the same job would let
            # somebody stack five reviews onto one day's work.
            models.UniqueConstraint(
                fields=["job", "direction"], name="one_review_per_direction_per_job"
            ),
            # And one per side per *booking*, which is the rule that matters:
            # a week worked for one client is one opinion, not five. Partial,
            # because a job outside a booking has no group to be unique on and
            # the constraint above already covers it.
            models.UniqueConstraint(
                fields=["booking", "direction"],
                condition=models.Q(booking__isnull=False),
                name="one_review_per_direction_per_booking",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    rating__gte=rules.RATING_MIN, rating__lte=rules.RATING_MAX
                ),
                name="rating_within_scale",
            ),
        ]
        indexes = [models.Index(fields=["direction", "-created_at"])]

    def save(self, *args, **kwargs):
        """Copy the booking down before writing.

        Here rather than in the service, so that the constraint cannot be
        stepped around by the admin, a data migration, or the next piece of
        code that writes a Review without knowing this rule exists.
        """
        if self.booking is None and self.job_id:
            self.booking = self.job.offer_group
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.author} rated {self.rating}/5 on {self.job}"

    @property
    def subject_profile(self):
        """The profile being rated — a WorkerProfile or a ClientProfile."""
        if self.direction == ReviewDirection.CLIENT_ON_WORKER:
            return self.job.assigned_worker
        return self.job.client

    @property
    def subject_user(self):
        profile = self.subject_profile
        return profile.user if profile else None
