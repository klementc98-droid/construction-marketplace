"""Retire dated gigs whose day has passed with nobody committed to them.

Run this on a schedule — once an hour is ample, since the resolution is a
calendar day. Nothing else retires a stale post, so if this does not run the
board slowly fills with gigs nobody can take, and workers open them to find a
date that went by last week.

    python manage.py expire_stale_gigs            # do it
    python manage.py expire_stale_gigs --dry-run  # show what it would do

Safe to run twice: expiring is a state transition, and the second pass finds
nothing because the rows are no longer in an expirable state.

Standing positions are untouched — they have no date to be past.
"""

from django.core.management.base import BaseCommand

from jobs.services import due_for_expiry, expire_stale_gigs


class Command(BaseCommand):
    help = "Move dated gigs past their day, with no worker committed, to Expired."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would expire without changing anything.",
        )

    def handle(self, *args, **options):
        # Materialised now, on purpose — the same reasoning as
        # settle_due_jobs: expiring changes `state`, which is the very field
        # the queryset filters on, so a lazy queryset re-evaluated afterwards
        # comes back empty and the arithmetic below goes negative.
        due = list(due_for_expiry())

        if options["dry_run"]:
            if not due:
                self.stdout.write("Nothing to expire.")
                return
            for job in due:
                self.stdout.write(
                    f"  would expire '{job.title}' "
                    f"({job.gig_date:%Y-%m-%d}, {job.state}) "
                    f"for {job.client.user}"
                )
            self.stdout.write(
                f"\n{len(due)} to expire. Re-run without --dry-run to apply."
            )
            return

        expired = expire_stale_gigs()
        skipped = len(due) - len(expired)

        for job in expired:
            self.stdout.write(
                self.style.SUCCESS(f"  expired '{job.title}' ({job.gig_date:%Y-%m-%d})")
            )
        self.stdout.write(f"\nExpired {len(expired)}.")
        if skipped:
            # Almost always a row that moved on between the queryset and the
            # lock — someone funded or cancelled it mid-run. Worth printing,
            # not worth failing over.
            self.stdout.write(
                self.style.WARNING(
                    f"{skipped} were due but had already moved on. Nothing to do."
                )
            )
