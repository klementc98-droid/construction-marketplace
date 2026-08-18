"""Nudge people who are holding somebody else up.

Every other notification is about a thing that just happened. This one is about
things that happened a while ago and were ignored — an offer nobody answered, a
finished job nobody confirmed, a rating nobody left. The cost of those is paid
by the person on the other end, who is waiting and cannot do anything about it.

Two rules keep it from becoming nagging:

* Only what is genuinely waiting. The list comes from ``jobs.waiting``, which
  is the same computation behind the header badge — so a reminder can never
  claim something the page does not also show.
* At most one per person per interval, enforced by putting the day in the
  dedupe key. Somebody with four things outstanding gets one email listing
  four, not four emails.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from jobs.waiting import waiting_for
from notifications.models import Kind
from notifications.services import notify

#: Days between nudges to the same person. A week is long enough that a
#: reminder still reads as a favour rather than as pestering, and short enough
#: that an unanswered offer does not sit for a month.
EVERY_DAYS = 7


class Command(BaseCommand):
    help = "Email people who have something waiting on their answer."

    def add_arguments(self, parser):
        parser.add_argument(
            "--every",
            type=int,
            default=EVERY_DAYS,
            help="Days between reminders to the same person.",
        )

    def handle(self, *args, **options):
        every = max(1, options["every"])
        # The bucket, not the date. Putting today's date in the key would allow
        # one a day; bucketing by the interval is what makes "at most one a
        # week" a property of the key rather than of how often cron runs — so
        # running this hourly and running it daily do the same thing.
        bucket = int(timezone.now().timestamp() // (every * 86400))

        queued = 0
        # Only people who could have something waiting. waiting_for costs three
        # counted queries per person, so walking every account would make this
        # scale by signups rather than by activity — and somebody holding
        # neither profile has no jobs, no offers and nothing to rate.
        people = (
            get_user_model()
            .objects.filter(email_notifications=True)
            .exclude(email="")
            .filter(Q(worker_profile__isnull=False) | Q(client_profile__isnull=False))
            .distinct()
        )

        for person in people:
            waiting = waiting_for(person)
            if not waiting.total:
                continue
            if notify(
                person,
                Kind.REMINDER,
                dedupe=f"reminder:{bucket}",
                offers=waiting.offers,
                confirmations=waiting.confirmations,
                ratings=waiting.ratings,
                path="/jobs/mine/",
            ):
                queued += 1

        self.stdout.write(f"queued {queued}")
        return None
