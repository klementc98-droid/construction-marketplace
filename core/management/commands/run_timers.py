"""Run the scheduled commands on a loop, for a machine with no cron.

Four things in this app happen because time passed rather than because
somebody pressed a button, and each has its own command. In a deployment they
belong in cron. On a development box — particularly a Windows one — there is
no cron, and the result is the failure this exists to prevent: notifications
are written correctly by every request, the table fills up, and not one email
is ever sent, because the thing that sends them is a command nobody ran.

    python manage.py run_timers          # loop until Ctrl-C
    python manage.py run_timers --once   # one pass of each, then exit

Leave it running in its own terminal beside ``runserver``. It is a development
convenience and nothing more: one process, no supervision, no persistence of
when it last ran. Real deployments should still use cron, which survives this
process being closed and does not lose its schedule when the laptop sleeps.

Every command it calls is idempotent, so a missed tick costs nothing but the
delay and a doubled one does nothing at all.
"""

from __future__ import annotations

import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

#: (command, seconds between runs). The intervals are the resolution each job
#: actually has, not a guess:
#:
#: * ``send_notifications`` is the only one anybody waits on. A minute is the
#:   difference between "the email arrived" and "the email is broken" to
#:   somebody testing an offer on their phone.
#: * ``expire_stale_gigs`` resolves to a calendar day; hourly is already far
#:   finer than it needs.
#: * ``settle_due_jobs`` moves money on a window measured in hours.
#: * ``remind_tomorrow`` is a nightly job, but the day is part of its dedupe
#:   key rather than a property of the schedule — so running it hourly sends
#:   exactly one reminder per person per day regardless, and means a laptop
#:   that was closed all evening still sends it when it opens.
#: * ``reconcile_payments`` asks Stripe what actually happened. Every few
#:   minutes, because the window it repairs — a capture that succeeded while
#:   the commit did not — is a window in which somebody's money has moved and
#:   this database does not know. It costs a handful of reads and nothing else
#:   when there is nothing wrong.
SCHEDULE: tuple[tuple[str, int], ...] = (
    ("send_notifications", 60),
    ("reconcile_payments", 300),
    ("expire_stale_gigs", 3600),
    ("settle_due_jobs", 3600),
    ("remind_tomorrow", 3600),
)


class Command(BaseCommand):
    help = "Run the scheduled commands on a timer (development stand-in for cron)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run every command a single time and exit, rather than looping.",
        )

    def handle(self, *args, **options):
        # Everything runs immediately on start, then on its own interval. The
        # first pass is the point of starting it: whatever queued up while
        # this was not running goes out now.
        due_at = {name: 0.0 for name, _ in SCHEDULE}

        if options["once"]:
            for name, _ in SCHEDULE:
                self._run(name)
            return

        self.stdout.write(
            "Timers running. "
            + ", ".join(f"{name} every {every}s" for name, every in SCHEDULE)
            + "\nCtrl-C to stop."
        )

        try:
            while True:
                now = time.monotonic()
                for name, every in SCHEDULE:
                    if now >= due_at[name]:
                        self._run(name)
                        # Scheduled from the end of the run, not the start, so
                        # a command that takes longer than its interval falls
                        # behind rather than being re-entered back to back.
                        due_at[name] = time.monotonic() + every
                # Coarse enough to sit idle, fine enough that the shortest
                # interval keeps roughly to its minute.
                time.sleep(1)
        except KeyboardInterrupt:
            self.stdout.write("\nStopped.")

    def _run(self, name: str) -> None:
        """One command, and never an exception out of the loop.

        A timer process that dies on the first bad SMTP password takes the
        other three jobs down with it, silently, in a terminal nobody is
        watching. A failure here is worth printing and worth trying again next
        tick — it is not worth stopping payments settling.
        """
        stamp = timezone.localtime().strftime("%H:%M:%S")
        self.stdout.write(f"[{stamp}] {name}: ", ending="")
        try:
            call_command(name)
        except Exception as error:            # noqa: BLE001 - see above
            self.stdout.write(self.style.ERROR(f"failed: {error}"))
        # Flushed every tick. Redirected to a file rather than a terminal,
        # Python buffers by the block, and a log that appears in one lump an
        # hour later is no use for the thing this is watched for.
        self.stdout.flush()
