"""A counter proposes days rather than a day.

Three steps rather than the two Django wrote for itself. The autodetector saw a
column go and another arrive and had no reason to connect them; dropping
`gig_date` before copying it would throw away every live negotiation's proposed
date, and a counter with no date reads to both sides as "same day as posted" —
which is the opposite of what was being proposed.
"""

from django.db import migrations, models


def carry_the_day_over(apps, schema_editor):
    """One date becomes a list of one."""
    Counter = apps.get_model("jobs", "Counter")
    for counter in Counter.objects.exclude(gig_date=None).only("id", "gig_date"):
        counter.gig_dates = [counter.gig_date.isoformat()]
        counter.save(update_fields=["gig_dates"])


def take_the_first_day_back(apps, schema_editor):
    """Reverse: the first proposed day is the one a single field can hold.

    Lossy, and there is no way for it not to be — a counter proposing four days
    cannot be expressed by a column that holds one. Written anyway so the
    migration is reversible in the ordinary case, which is the one anybody
    rolling back is likely to have.
    """
    from datetime import date

    Counter = apps.get_model("jobs", "Counter")
    for counter in Counter.objects.exclude(gig_dates=None).only("id", "gig_dates"):
        days = counter.gig_dates or []
        counter.gig_date = date.fromisoformat(days[0]) if days else None
        counter.save(update_fields=["gig_date"])


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0011_experience_wanted"),
    ]

    operations = [
        migrations.AddField(
            model_name="counter",
            name="gig_dates",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(carry_the_day_over, take_the_first_day_back),
        migrations.RemoveField(
            model_name="counter",
            name="gig_date",
        ),
    ]
