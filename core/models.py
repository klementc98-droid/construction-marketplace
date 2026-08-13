"""Shared reference data: regions and trades.

Both are database rows rather than Python constants, which is the whole point.
v1 launches in one city, but "one city" is a data fact, not a schema fact —
adding the second one is an INSERT, not a migration and a refactor.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext, gettext_lazy as _


class TimestampedModel(models.Model):
    """Created/updated stamps for anything worth auditing later."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Region(TimestampedModel):
    """A launch market.

    Everything geographic hangs off this rather than off a hardcoded city
    name. Jobs and profiles both point here, so opening a second market means
    creating a row and letting people select it — no code path assumes there
    is exactly one.
    """

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=120, unique=True)

    #: IANA timezone. Gig dates and dispute windows are only meaningful in the
    #: market's local time — "the 48 hours expired" must not depend on where
    #: the server happens to be running.
    timezone = models.CharField(max_length=64, default="America/New_York")

    #: Lets a market be prepared before it opens, or paused, without deleting
    #: it and orphaning every job that referenced it.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class Trade(TimestampedModel):
    """A construction trade/category.

    Seeded with the v1 list (see the data migration). A row rather than a
    choices enum so the list can grow without a migration, and so filters can
    join instead of matching strings.
    """

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True)

    #: Whether this trade is regulated and should prompt for a licence number.
    #: Driving the licence field off a data flag rather than a hardcoded
    #: ``if trade in ("electrician", "plumber", "hvac")`` means the day a
    #: jurisdiction starts regulating, say, roofers, it is a checkbox in the
    #: admin — not a code change in every template that touches profiles.
    requires_license = models.BooleanField(
        default=False,
        help_text="Prompt for a licence number on worker profiles in this trade.",
    )

    display_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ("display_order", "name")

    def __str__(self) -> str:
        """The trade's name, translated when a catalogue has it.

        Trade names are rows, not code, so gettext cannot find them by reading
        the source — SEEDED_TRADE_NAMES below exists purely so the extractor
        sees them. The lookup is by the English name because that is what the
        seed migration writes and what the slug is derived from.

        A trade added later through the admin simply renders as typed, which is
        the right failure: an untranslated name is readable, and the alternative
        is a second name column that nobody remembers to fill in.
        """
        return gettext(self.name)


#: The v1 trade list, restated so ``extractpo`` can find these strings. Nothing
#: reads this at runtime; it exists to put the names in the catalogue. Keep it
#: in step with the seed migration.
SEEDED_TRADE_NAMES = (
    _("General labor"),
    _("Electrician"),
    _("Plumber"),
    _("Carpenter"),
    _("Mason/Concrete"),
    _("Painter"),
    _("Roofer"),
    _("HVAC"),
    _("Drywall/Framing"),
    _("Landscaping/Excavation"),
    _("Welder"),
    _("Heavy equipment operator"),
)
