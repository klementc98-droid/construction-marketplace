"""Seed the v1 trade list and the launch region.

Reference data belongs in a migration, not a fixture someone has to remember
to load: a fresh clone that runs ``migrate`` should have a working trade list,
because a marketplace with an empty category list is not bootable.

Only the three trades that are licensed in essentially every US jurisdiction
are marked ``requires_license``. That flag drives whether the profile form
asks for a licence number, so marking a trade regulated when it is not just
produces a field nobody can fill in.
"""

from django.db import migrations

TRADES = [
    # (name, slug, requires_license, display_order)
    ("General labor", "general-labor", False, 10),
    ("Electrician", "electrician", True, 20),
    ("Plumber", "plumber", True, 30),
    ("Carpenter", "carpenter", False, 40),
    ("Mason/Concrete", "mason-concrete", False, 50),
    ("Painter", "painter", False, 60),
    ("Roofer", "roofer", False, 70),
    ("HVAC", "hvac", True, 80),
    ("Drywall/Framing", "drywall-framing", False, 90),
    ("Landscaping/Excavation", "landscaping-excavation", False, 100),
    ("Welder", "welder", False, 110),
    ("Heavy equipment operator", "heavy-equipment-operator", False, 120),
]


def seed(apps, schema_editor):
    from config import business_rules as rules

    Trade = apps.get_model("core", "Trade")
    Region = apps.get_model("core", "Region")

    for name, slug, requires_license, order in TRADES:
        Trade.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "requires_license": requires_license,
                "display_order": order,
            },
        )

    Region.objects.update_or_create(
        slug=rules.DEFAULT_REGION_SLUG,
        defaults={
            "name": rules.DEFAULT_REGION_NAME,
            "timezone": rules.DEFAULT_REGION_TIMEZONE,
            "is_active": True,
        },
    )


def unseed(apps, schema_editor):
    """Remove only the seeded trades.

    Regions are deliberately left alone: by the time anyone reverses this,
    profiles and jobs may point at the launch region, and deleting it would
    either cascade into real user data or fail on the PROTECT constraint.
    """
    Trade = apps.get_model("core", "Trade")
    Trade.objects.filter(slug__in=[slug for _, slug, _, _ in TRADES]).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
