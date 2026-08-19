"""Where a market is, as a field rather than as a default argument.

The country reaches Stripe: a worker's Connect account is opened in one, and
country decides which capabilities exist, what onboarding asks for and whether
payouts are possible at all. It was a default of "US" on the gateway function,
which is a coherent answer for exactly one launch market and silently wrong for
every other — including a marketplace running in euros and Greek.

The field's default fills existing rows, so a market already trading keeps
whatever DEFAULT_REGION_COUNTRY says at the time this runs. That is deliberate:
a data migration guessing the country from a timezone or a currency would be
inventing an answer, and the one place that can honestly know is configuration.
"""

from django.db import migrations, models

from config import business_rules as rules


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_seed_reference_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="region",
            name="country",
            field=models.CharField(
                default=rules.DEFAULT_REGION_COUNTRY, max_length=2
            ),
        ),
    ]
