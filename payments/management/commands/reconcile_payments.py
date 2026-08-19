"""Ask Stripe what happened, and make the database agree.

The fourth scheduled command, and the only one whose job is to admit that this
app is not the only system that knows things. The other three act on time
passing; this one acts on the gap between two databases that no transaction
spans — see ``payments/reconciliation.py`` for what it repairs and why Stripe
is treated as the authority throughout.

    python manage.py reconcile_payments
    python manage.py reconcile_payments --dry-run
    python manage.py reconcile_payments --keep-dead-holds

Run it every few minutes in a real deployment. It is idempotent by
construction — every repair is a conditional claim — so running it often costs
a handful of Stripe reads and nothing else.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from payments import gateway, reconciliation


class Command(BaseCommand):
    help = "Reconcile local payment records against Stripe."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the disagreements without repairing anything.",
        )
        parser.add_argument(
            "--keep-dead-holds",
            action="store_true",
            help=(
                "Leave authorisations on expired or cancelled jobs alone. The "
                "default releases them: that money is the client's, frozen for "
                "work that is not going to happen."
            ),
        )

    def handle(self, *args, **options):
        if not gateway.configured():
            # Not an error. Most developers run this app with no Stripe keys at
            # all, and a scheduled command that fails loudly every five minutes
            # on a machine with no payments is noise that teaches people to
            # ignore the log.
            self.stdout.write("Stripe is not configured — nothing to reconcile.")
            return

        if options["dry_run"]:
            # A dry run still reads from Stripe; it simply refuses to write.
            # Implemented by reporting on a pass that repairs nothing rather
            # than by a second code path, which would be a second thing to keep
            # correct and the one nobody would run.
            self.stdout.write("Dry run — reporting only.\n")

        report = reconciliation.reconcile(
            release_dead_holds=not options["keep_dead_holds"],
            dry_run=options["dry_run"],
        )

        for line in report.lines():
            self.stdout.write(line)

        if report.unreachable:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(report.unreachable)} could not be checked. Stripe not "
                    "answering is not a finding — they are left as they are and "
                    "looked at again next run."
                )
            )
        elif report.repaired:
            self.stdout.write(self.style.SUCCESS(f"\nRepaired {report.repaired}."))
        return None
