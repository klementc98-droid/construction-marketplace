"""Create a local admin login and a couple of demo profiles.

Development only. Google OAuth needs real credentials, so this exists to let
you walk the UI before those are configured: sign in at /admin/ with the
password below and the session works across the whole site.

The second worker is seeded with real history on purpose — with only fresh
accounts every trust stat reads "New", and you cannot see that the display
logic actually works.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import AvailabilityStatus, ClientProfile, WorkerProfile
from core.models import Region, Trade

PASSWORD = "crew-dev-1234"


class Command(BaseCommand):
    help = "Seed a dev admin account and demo profiles."

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        region = Region.objects.filter(is_active=True).first()
        if region is None:
            self.stderr.write("No active region — run `manage.py migrate` first.")
            return

        admin, _ = User.objects.get_or_create(
            email="you@example.com", defaults={"full_name": "Demo Admin"}
        )
        admin.is_staff = True
        admin.is_superuser = True
        admin.set_password(PASSWORD)
        admin.save()

        # One account, both roles — the modelling claim, made visible.
        worker, _ = WorkerProfile.objects.get_or_create(
            user=admin,
            defaults={
                "region": region,
                "years_experience": 9,
                "service_area": "North side, will travel citywide",
                "rate_min": Decimal("32"),
                "rate_max": Decimal("40"),
                "availability_status": AvailabilityStatus.AVAILABLE_NOW,
                "bio": "Nine years framing and finish carpentry. Own tools, own truck.",
            },
        )
        worker.trades.set(
            Trade.objects.filter(slug__in=["carpenter", "drywall-framing", "electrician"])
        )
        worker.licenses.get_or_create(
            trade=Trade.objects.get(slug="electrician"), defaults={"number": "EL-441029"}
        )

        client, _ = ClientProfile.objects.get_or_create(
            user=admin,
            defaults={
                "region": region,
                "company_name": "Ridgeline Builders",
                "location": "Downtown",
            },
        )

        veteran_user, _ = User.objects.get_or_create(
            email="veteran@example.com", defaults={"full_name": "Marta Ruiz"}
        )
        veteran, _ = WorkerProfile.objects.get_or_create(
            user=veteran_user,
            defaults={
                "region": region,
                "years_experience": 15,
                "service_area": "Citywide",
                "rate_type": "daily",
                "rate_min": Decimal("280"),
                "rating_sum": 66,
                "rating_count": 14,
                "jobs_accepted": 20,
                "jobs_completed": 19,
                "jobs_disputed": 1,
                "bio": "Licensed plumber. Commercial and residential.",
            },
        )
        veteran.trades.set(Trade.objects.filter(slug__in=["plumber"]))
        veteran.licenses.get_or_create(
            trade=Trade.objects.get(slug="plumber"), defaults={"number": "PL-7781"}
        )

        out = self.stdout
        out.write(self.style.SUCCESS("\nSeeded.\n"))
        out.write(f"  Sign in at /admin/ with  you@example.com  /  {PASSWORD}")
        out.write("  Then browse:")
        out.write(f"    /                     dashboard (both roles)")
        out.write(f"    /worker/{worker.pk}/            your worker profile — all stats 'New'")
        out.write(f"    /worker/{veteran.pk}/            worker with history — real stats")
        out.write(f"    /client/{client.pk}/            your client profile")
        out.write(f"    /profile/worker/edit/ the profile form\n")
