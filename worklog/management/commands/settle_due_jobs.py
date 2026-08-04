"""Release payments whose window has lapsed.

Run this on a schedule — every 10 minutes is plenty, since the shortest window
is two hours. Nothing else in the system moves money on a timer, so if this
does not run, workers do not get paid without the client clicking approve.

    python manage.py settle_due_jobs          # do it
    python manage.py settle_due_jobs --dry-run  # show what it would do
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from worklog.models import Completion
from worklog.services import settle_due


class Command(BaseCommand):
    help = "Auto-release payments past their approval or dispute window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what is due without touching any money.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        # Materialised now, on purpose. Settling flips ``settled_at``, so a
        # lazy queryset re-evaluated afterwards would come back empty and the
        # "skipped" arithmetic below would go negative.
        due = list(
            Completion.objects.filter(settled_at__isnull=True, settles_at__lte=now)
            .select_related("job", "job__assigned_worker__user")
        )

        if options["dry_run"]:
            if not due:
                self.stdout.write("Nothing due.")
                return
            for completion in due:
                kind = "early finish" if completion.ended_early else "full day"
                self.stdout.write(
                    f"  would release ${completion.payable_amount} "
                    f"({kind}) on '{completion.job.title}' "
                    f"to {completion.job.assigned_worker.user} "
                    f"— due since {completion.settles_at:%Y-%m-%d %H:%M}"
                )
            self.stdout.write(f"\n{len(due)} due. Re-run without --dry-run to release.")
            return

        settled = settle_due(now=now)
        skipped = len(due) - len(settled)

        for completion in settled:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  released ${completion.payable_amount} on "
                    f"'{completion.job.title}'"
                )
            )
        self.stdout.write(f"\nReleased {len(settled)}.")
        if skipped:
            # Almost always an escrow that is missing or already settled — a
            # reconciliation question, and one a human should look at.
            self.stdout.write(
                self.style.WARNING(
                    f"{skipped} due but not releasable. Check their escrow records."
                )
            )
