"""One rating per booking, enforced by the database rather than by a view.

The rule was real but unwritten: ``review_create`` collapsed to the booking's
first day before writing, so through the button you could only rate a booking
once. Underneath, the service checked the day and the unique constraint named
the day, which left a second rating on the same booking representable — and it
happened, twice over, on the first multi-day booking the app ever ran.

Three steps, and the order matters. The column has to exist before it can be
filled, and it has to be *correct* before a unique constraint is put over it or
the migration fails on exactly the data it was written to clean up.

The duplicates are collapsed to the first rating written for each booking and
direction — what the rule would have produced had it been enforced when the
second one was attempted. The profile counters are then recomputed from the
surviving rows rather than decremented, because a counter that has been wrong
is not something to do arithmetic on; the reviews are the record, and the two
columns are a cache of them.
"""

from django.db import migrations, models


def fill_and_collapse(apps, schema_editor):
    Review = apps.get_model("jobs", "Review")
    WorkerProfile = apps.get_model("accounts", "WorkerProfile")
    ClientProfile = apps.get_model("accounts", "ClientProfile")

    for review in Review.objects.select_related("job").all():
        review.booking = review.job.offer_group
        review.save(update_fields=["booking"])

    # Oldest first, so the first rating of each booking is the one that stays.
    seen: set = set()
    doomed: list = []
    for review in Review.objects.filter(booking__isnull=False).order_by(
        "created_at", "pk"
    ):
        key = (review.booking, review.direction)
        if key in seen:
            doomed.append(review.pk)
        else:
            seen.add(key)
    Review.objects.filter(pk__in=doomed).delete()

    # Recomputed from what survives, for every profile — including the ones
    # that were never involved, whose counters should come out unchanged.
    for model in (WorkerProfile, ClientProfile):
        for profile in model.objects.all():
            if model is WorkerProfile:
                scores = Review.objects.filter(
                    direction="client_on_worker", job__assigned_worker=profile
                ).values_list("rating", flat=True)
            else:
                scores = Review.objects.filter(
                    direction="worker_on_client", job__client=profile
                ).values_list("rating", flat=True)
            scores = list(scores)
            profile.rating_sum = sum(scores)
            profile.rating_count = len(scores)
            profile.save(update_fields=["rating_sum", "rating_count"])


def unfill(apps, schema_editor):
    """Reversing drops the column. Nothing deleted above comes back."""


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0008_alter_job_use_escrow"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="review",
            name="booking",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(fill_and_collapse, unfill),
        migrations.AddConstraint(
            model_name="review",
            constraint=models.UniqueConstraint(
                condition=models.Q(("booking__isnull", False)),
                fields=("booking", "direction"),
                name="one_review_per_direction_per_booking",
            ),
        ),
    ]
