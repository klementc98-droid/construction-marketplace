"""Tell people the night before that they are working in the morning.

Not triggered by anything anybody did — the calendar arriving at a date is what
causes it, which is why it is a command rather than a hook.

A day, not a booking. Everywhere else in this app a week of work is one thing
and is deliberately collapsed into one row and one email; here it is not, and
that is the point. Tomorrow is a specific morning somebody has to turn up on,
and a booking-wide reminder sent once would tell them about Monday and leave
them to remember Thursday themselves.

Run it once a day, in the evening. Running it more often is harmless: the
dedupe key names the day, so the second run of the same day writes nothing.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.state_machine import JobState
from jobs.models import Job, JobType, booking_of
from notifications.models import Kind
from notifications.services import notify

#: The states a job can be in and still be happening tomorrow. Both mean a
#: worker is committed: one with the money held and one without, which is the
#: distinction escrow makes and which does not matter to whether somebody is
#: expected on site.
DUE_STATES = (JobState.ACCEPTED, JobState.ESCROW_HELD)


class Command(BaseCommand):
    help = "Email workers about the job they have on tomorrow."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=1,
            help="How far ahead to look. 1 is tomorrow, which is the point of "
                 "the command; the option exists so a test does not have to "
                 "travel through time.",
        )

    def handle(self, *args, **options):
        target = timezone.localdate() + timedelta(days=options["days"])

        due = (
            Job.objects.filter(
                job_type=JobType.GIG,
                gig_date=target,
                state__in=DUE_STATES,
                assigned_worker__isnull=False,
            )
            .select_related("assigned_worker__user", "client__user", "trade")
            .order_by("pk")
        )

        queued = 0
        for job in due:
            # Which day of the booking this is, so a five-day week reads as
            # "day 3 of 5" rather than as five identical reminders that give no
            # clue where in the job somebody is.
            days = booking_of(job) if job.offer_group else [job]
            try:
                position = [d.pk for d in days].index(job.pk) + 1
            except ValueError:                      # pragma: no cover - defensive
                position = 1

            if notify(
                job.assigned_worker.user,
                Kind.TOMORROW,
                job=job,
                # The date, not the booking. This is the one reminder that is
                # about a particular morning, so collapsing a booking here
                # would tell somebody about Monday and let them miss Thursday.
                dedupe=f"tomorrow:{job.pk}:{target.isoformat()}",
                job_title=job.title,
                client=str(job.client.user),
                pay=str(job.fixed_pay or ""),
                hours=str(job.gig_hours or ""),
                where=job.location or "",
                day_number=position,
                of_days=len(days) if len(days) > 1 else 0,
            ):
                queued += 1

        self.stdout.write(f"queued {queued}")
        return None
