"""Post whatever is owed. Driven by cron, like the other two."""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications import mail
from notifications.models import Notification

#: After this many failed attempts a row is left alone. Something is wrong with
#: the address or the template rather than with the network, and a permanent
#: failure retried every five minutes forever is how a sending queue turns into
#: a log nobody reads.
MAX_ATTEMPTS = 5


class Command(BaseCommand):
    help = "Send queued email notifications."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Most emails to send in one run. Keeps a backlog from turning "
                 "into one very long run that overlaps the next.",
        )

    def handle(self, *args, **options):
        due = (
            Notification.objects.filter(
                sent_at__isnull=True, attempts__lt=MAX_ATTEMPTS
            )
            .select_related("recipient", "job")
            .order_by("created_at")[: options["limit"]]
        )

        sent = failed = 0
        for notification in due:
            # Per row, and never a raise out of the loop. One bad address must
            # not stop the other hundred and ninety-nine: the point of a queue
            # is that a failure is data, not an outage.
            try:
                message = mail.build(notification)
                message.send()
            except Exception as error:            # noqa: BLE001 - see above
                Notification.objects.filter(pk=notification.pk).update(
                    attempts=notification.attempts + 1,
                    last_error=str(error)[:2000],
                    updated_at=timezone.now(),
                )
                failed += 1
                continue

            # Marked by id rather than by saving the instance: sent_at is the
            # only thing that decides whether somebody gets a second copy, and
            # a plain UPDATE cannot write anything else back by accident.
            Notification.objects.filter(pk=notification.pk).update(
                sent_at=timezone.now(),
                attempts=notification.attempts + 1,
                last_error="",
                updated_at=timezone.now(),
            )
            sent += 1

        self.stdout.write(f"sent {sent}, failed {failed}")
        return None
