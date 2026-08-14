"""Phase 2 tests: the rules that would cost someone money or a day's work."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import ClientProfile, WorkerProfile
from core.models import Region, Trade
from core.state_machine import JobState

from .models import Application, ApplicationStatus, Job, JobType

User = get_user_model()


def make_user(email: str) -> "User":
    return User.objects.create_user(email=email, full_name=email.split("@")[0])


class JobFactoryMixin:
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.filter(is_active=True).first()
        cls.carpentry = Trade.objects.get(slug="carpenter")
        cls.electrical = Trade.objects.get(slug="electrician")

        cls.client_user = make_user("poster@example.com")
        cls.client_profile = ClientProfile.objects.create(
            user=cls.client_user, region=cls.region
        )
        cls.worker_user = make_user("worker@example.com")
        cls.worker_profile = WorkerProfile.objects.create(
            user=cls.worker_user, region=cls.region
        )

    def gig(self, **overrides):
        defaults = dict(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.carpentry,
            region=self.region,
            title="Framing help",
            description="Second storey rebuild.",
            gig_date=timezone.localdate() + timedelta(days=3),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("90"),
        )
        return Job.objects.create(**(defaults | overrides))

    def standing(self, **overrides):
        defaults = dict(
            client=self.client_profile,
            job_type=JobType.STANDING,
            trade=self.carpentry,
            region=self.region,
            title="Carpenter wanted",
            description="Ongoing work.",
            rate_type="hourly",
            rate_min=Decimal("30"),
            rate_max=Decimal("38"),
            position_type="ongoing",
        )
        return Job.objects.create(**(defaults | overrides))


class JobShapeTests(JobFactoryMixin, TestCase):
    """A post must be one of the two valid shapes — never a hybrid."""

    def test_gig_without_a_date_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Job.objects.create(
                client=self.client_profile,
                job_type=JobType.GIG,
                trade=self.carpentry,
                region=self.region,
                title="No date",
                description="x",
            )

    def test_standing_position_cannot_carry_a_fixed_gig_price(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Job.objects.create(
                client=self.client_profile,
                job_type=JobType.STANDING,
                trade=self.carpentry,
                region=self.region,
                title="Hybrid",
                description="x",
                rate_type="hourly",
                rate_min=Decimal("30"),
                position_type="ongoing",
                fixed_pay=Decimal("90"),
            )

    def test_rate_range_cannot_be_inverted(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.standing(rate_min=Decimal("40"), rate_max=Decimal("20"))

    def test_a_gig_cannot_be_posted_for_a_date_that_has_passed(self):
        job = Job(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.carpentry,
            region=self.region,
            title="Yesterday",
            description="x",
            gig_date=timezone.localdate() - timedelta(days=1),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("90"),
        )
        with self.assertRaises(ValidationError) as caught:
            job.full_clean()
        self.assertIn("gig_date", caught.exception.message_dict)


class PayDisplayTests(JobFactoryMixin, TestCase):
    def test_gig_shows_the_total_and_what_it_works_out_to_hourly(self):
        """The spec's own example: $90 for 8 hours."""
        job = self.gig()
        self.assertEqual(job.pay_display, "€90 for 8 hours")
        self.assertEqual(job.implied_hourly, Decimal("11.25"))

    def test_standing_shows_a_range_or_a_flat_rate(self):
        self.assertEqual(self.standing().pay_display, "€30-€38/hr")
        self.assertEqual(self.standing(rate_max=None).pay_display, "€30/hr")

    def test_a_standing_position_has_no_implied_hourly(self):
        self.assertIsNone(self.standing().implied_hourly)


class FilterTests(JobFactoryMixin, TestCase):
    def test_filters_narrow_by_trade_type_and_free_text(self):
        gig = self.gig()
        standing = self.standing(trade=self.electrical, title="Sparky needed")

        self.assertEqual(list(Job.objects.open().for_trade("carpenter")), [gig])
        self.assertEqual(list(Job.objects.open().for_type("standing")), [standing])
        self.assertEqual(list(Job.objects.open().matching("sparky")), [standing])
        self.assertEqual(list(Job.objects.open().matching("storey")), [gig])

    def test_filled_jobs_drop_off_the_board(self):
        job = self.gig()
        self.assertIn(job, Job.objects.open())
        job.state = JobState.ACCEPTED
        job.save(update_fields=["state"])
        self.assertNotIn(job, Job.objects.open())


class PostingPermissionTests(JobFactoryMixin, TestCase):
    def test_a_worker_without_a_client_profile_is_sent_to_add_one(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("jobs:post_choose"))
        self.assertRedirects(response, reverse("accounts:select_role"))

    def test_a_client_can_post_a_gig(self):
        self.client.force_login(self.client_user)
        response = self.client.post(
            reverse("jobs:post", args=["gig"]),
            {
                "trade": self.carpentry.pk,
                "title": "Concrete pour",
                "description": "Slab, one day.",
                "region": self.region.pk,
                "location": "North side",
                "gig_dates": (timezone.localdate() + timedelta(days=5)).isoformat(),
                "gig_hours": "8",
                "fixed_pay": "180",
            },
        )
        job = Job.objects.get(title="Concrete pour")
        self.assertRedirects(response, reverse("jobs:detail", args=[job.pk]))
        self.assertEqual(job.job_type, JobType.GIG)
        self.assertEqual(job.state, JobState.POSTED)
        self.assertEqual(job.client, self.client_profile)

    def test_posting_a_gig_in_the_past_is_refused(self):
        self.client.force_login(self.client_user)
        response = self.client.post(
            reverse("jobs:post", args=["gig"]),
            {
                "trade": self.carpentry.pk,
                "title": "Too late",
                "description": "x",
                "region": self.region.pk,
                "gig_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
                "gig_hours": "8",
                "fixed_pay": "90",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Job.objects.filter(title="Too late").exists())

    def test_one_client_cannot_edit_another_clients_post(self):
        job = self.gig()
        intruder = make_user("intruder@example.com")
        ClientProfile.objects.create(user=intruder, region=self.region)
        self.client.force_login(intruder)
        self.assertEqual(
            self.client.get(reverse("jobs:edit", args=[job.pk])).status_code, 404
        )


class ApplicationTests(JobFactoryMixin, TestCase):
    def test_a_worker_can_apply_once_and_editing_does_not_stack_rows(self):
        job = self.gig()
        self.client.force_login(self.worker_user)
        url = reverse("jobs:apply", args=[job.pk])

        self.client.post(url, {"message": "Done plenty of framing."})
        self.client.post(url, {"message": "Updated pitch."})

        applications = Application.objects.filter(job=job, worker=self.worker_profile)
        self.assertEqual(applications.count(), 1)
        self.assertEqual(applications.first().message, "Updated pitch.")

    def test_the_unique_constraint_holds_even_if_a_view_is_bypassed(self):
        job = self.gig()
        Application.objects.create(job=job, worker=self.worker_profile)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Application.objects.create(job=job, worker=self.worker_profile)

    def test_applying_to_a_filled_job_is_refused(self):
        job = self.gig(state=JobState.ACCEPTED)
        self.client.force_login(self.worker_user)
        response = self.client.post(
            reverse("jobs:apply", args=[job.pk]), {"message": "Late"}
        )
        self.assertRedirects(response, reverse("jobs:detail", args=[job.pk]))
        self.assertFalse(Application.objects.filter(job=job).exists())

    def test_a_user_without_a_worker_profile_is_sent_to_add_one(self):
        job = self.gig()
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:apply", args=[job.pk]))
        self.assertRedirects(response, reverse("accounts:select_role"))

    def test_withdrawing_frees_the_worker_to_apply_again(self):
        job = self.gig()
        self.client.force_login(self.worker_user)
        self.client.post(reverse("jobs:apply", args=[job.pk]), {"message": "In"})
        application = Application.objects.get(job=job)

        self.client.post(
            reverse("jobs:application_withdraw", args=[application.pk])
        )
        application.refresh_from_db()
        self.assertEqual(application.status, ApplicationStatus.WITHDRAWN)

        self.client.post(reverse("jobs:apply", args=[job.pk]), {"message": "Back in"})
        application.refresh_from_db()
        self.assertEqual(application.status, ApplicationStatus.APPLIED)
        self.assertEqual(Application.objects.filter(job=job).count(), 1)


class SelectionTests(JobFactoryMixin, TestCase):
    def setUp(self):
        self.job = self.gig()
        self.chosen = Application.objects.create(
            job=self.job, worker=self.worker_profile
        )
        rival_user = make_user("rival@example.com")
        self.rival_profile = WorkerProfile.objects.create(
            user=rival_user, region=self.region
        )
        self.rival = Application.objects.create(
            job=self.job, worker=self.rival_profile
        )

    def test_selecting_assigns_the_worker_and_answers_everyone_else(self):
        self.client.force_login(self.client_user)
        response = self.client.post(
            reverse("jobs:application_select", args=[self.chosen.pk])
        )
        self.assertRedirects(response, reverse("jobs:detail", args=[self.job.pk]))

        self.job.refresh_from_db()
        self.chosen.refresh_from_db()
        self.rival.refresh_from_db()

        self.assertEqual(self.job.state, JobState.ACCEPTED)
        self.assertEqual(self.job.assigned_worker, self.worker_profile)
        self.assertIsNotNone(self.job.filled_at)
        self.assertEqual(self.chosen.status, ApplicationStatus.SELECTED)
        # Silence is the failure mode this prevents.
        self.assertEqual(self.rival.status, ApplicationStatus.PASSED_OVER)
        self.assertIsNotNone(self.rival.responded_at)

    def test_a_second_selection_is_refused(self):
        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:application_select", args=[self.chosen.pk]))
        self.client.post(reverse("jobs:application_select", args=[self.rival.pk]))

        self.job.refresh_from_db()
        self.rival.refresh_from_db()
        self.assertEqual(self.job.assigned_worker, self.worker_profile)
        self.assertEqual(self.rival.status, ApplicationStatus.PASSED_OVER)

    def test_a_stranger_cannot_select_for_someone_elses_job(self):
        outsider = make_user("outsider@example.com")
        ClientProfile.objects.create(user=outsider, region=self.region)
        self.client.force_login(outsider)
        response = self.client.post(
            reverse("jobs:application_select", args=[self.chosen.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.assigned_worker)


class BoardVisibilityTests(JobFactoryMixin, TestCase):
    def test_the_board_is_readable_without_an_account(self):
        job = self.gig()
        response = self.client.get(reverse("jobs:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, job.title)

    def test_worker_search_filters_by_trade(self):
        self.worker_profile.trades.add(self.carpentry)
        other = WorkerProfile.objects.create(
            user=make_user("sparky@example.com"), region=self.region
        )
        other.trades.add(self.electrical)

        response = self.client.get(
            reverse("jobs:worker_list"), {"trade": self.carpentry.pk}
        )
        self.assertContains(response, str(self.worker_profile.user))
        self.assertNotContains(response, str(other.user))

    def test_the_full_time_filter_takes_only_an_explicit_yes(self):
        """Never asked is not the same as no — but it is not a lead either."""
        keen = self.worker_profile
        keen.open_to_full_time = True
        keen.save(update_fields=["open_to_full_time"])

        refused = WorkerProfile.objects.create(
            user=make_user("dayonly@example.com"),
            region=self.region,
            open_to_full_time=False,
        )
        never_asked = WorkerProfile.objects.create(
            user=make_user("unasked@example.com"), region=self.region
        )
        self.assertIsNone(never_asked.open_to_full_time)

        response = self.client.get(reverse("jobs:worker_list"), {"full_time": "on"})
        self.assertContains(response, str(keen.user))
        self.assertNotContains(response, str(refused.user))
        self.assertNotContains(response, str(never_asked.user))

        unfiltered = self.client.get(reverse("jobs:worker_list"))
        self.assertContains(unfiltered, str(never_asked.user))


class BookingDisplayTests(JobFactoryMixin, TestCase):
    """A four-day booking reads as one booking, not four jobs.

    The rows stay per day in the database — each carries its own escrow,
    sign-off and expiry — but three near-identical cards differing only by
    date read as the same job posted three times by mistake.
    """

    def booking(self, days=3, pay="90"):
        from uuid import uuid4

        group = uuid4()
        made = []
        for n in range(days):
            job = self.gig(fixed_pay=Decimal(pay))
            job.offer_group = group
            job.gig_date = timezone.localdate() + timedelta(days=3 + n)
            job.save(update_fields=["offer_group", "gig_date"])
            made.append(job)
        return made

    def test_the_board_shows_one_row_for_the_booking(self):
        from jobs.models import collapse_groups

        made = self.booking(4)
        rows = collapse_groups(Job.objects.filter(offer_group=made[0].offer_group))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].group_days, 4)

    def test_the_row_carries_every_date_in_order(self):
        from jobs.models import collapse_groups

        made = self.booking(3)
        row = collapse_groups(Job.objects.filter(offer_group=made[0].offer_group))[0]
        self.assertEqual(row.group_dates, sorted(j.gig_date for j in made))

    def test_a_single_day_job_is_untouched(self):
        from jobs.models import collapse_groups

        self.gig()
        rows = collapse_groups(Job.objects.filter(offer_group=None))
        self.assertTrue(all(r.group_days == 1 for r in rows))

    def test_the_count_on_the_board_matches_the_rows(self):
        """"12 open posts" for four bookings is a number matching nothing."""
        self.booking(4)
        response = self.client.get(reverse("jobs:list"))
        self.assertEqual(response.context["total"], len(response.context["jobs"]))

    def test_the_pay_is_stated_per_day(self):
        self.booking(3, pay="90")
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "per day")

    def test_both_parties_see_the_booking_on_the_day(self):
        made = self.booking(3)
        for user in (self.client_user,):
            self.client.force_login(user)
            response = self.client.get(reverse("jobs:detail", args=[made[0].pk]))
            self.assertEqual(response.context["group_days"], 3)
            self.assertContains(response, "per day")

    def test_the_days_are_still_separate_rows_underneath(self):
        """The display groups them; escrow and sign-off still do not."""
        made = self.booking(3)
        self.assertEqual(Job.objects.filter(offer_group=made[0].offer_group).count(), 3)
        self.assertEqual(len({j.pk for j in made}), 3)


class WaitingPanelTests(JobFactoryMixin, TestCase):
    """"Waiting on you", on both the home page and Mine.

    The three things that can sit unanswered here all cost somebody real time
    when they go unseen, and none of them announce themselves — they sit
    inside a list you have to think to open.
    """

    def test_nothing_waiting_renders_nothing(self):
        """An empty "0 waiting" teaches people to stop reading the notice."""
        self.client.force_login(self.client_user)
        for url in (reverse("accounts:home"), reverse("jobs:mine")):
            with self.subTest(url=url):
                self.assertNotContains(self.client.get(url), "Waiting on you")

    def test_a_finished_job_to_rate_is_counted(self):
        from core.state_machine import JobState
        from jobs.waiting import waiting_for

        job = self.gig()
        job.state = JobState.CLOSED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])

        self.assertEqual(waiting_for(self.client_user).ratings, 1)
        self.assertEqual(waiting_for(self.worker_user).ratings, 1)

    def test_it_stops_counting_once_rated(self):
        from core.state_machine import JobState
        from jobs.models import Review, ReviewDirection
        from jobs.waiting import waiting_for

        job = self.gig()
        job.state = JobState.CLOSED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])
        Review.objects.create(
            job=job, author=self.client_user,
            direction=ReviewDirection.CLIENT_ON_WORKER, rating=5,
        )

        self.assertEqual(waiting_for(self.client_user).ratings, 0)
        self.assertEqual(waiting_for(self.worker_user).ratings, 1)

    def test_it_shows_on_both_pages(self):
        from core.state_machine import JobState

        job = self.gig()
        job.state = JobState.CLOSED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])

        self.client.force_login(self.client_user)
        for url in (reverse("accounts:home"), reverse("jobs:mine")):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "Waiting on you")

    def test_a_signed_out_visitor_is_never_asked_for_anything(self):
        from jobs.waiting import waiting_for
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(waiting_for(AnonymousUser()))


class BookingIsOneThingTests(JobFactoryMixin, TestCase):
    """Five days is one job to look at, one to apply for, one to be given.

    The rows stay per day underneath — each carries its own escrow, sign-off
    and expiry — but nobody applies for Tuesday and thinks they have not
    applied for Wednesday.
    """

    def booking(self, days=5):
        from uuid import uuid4

        group = uuid4()
        made = []
        for n in range(days):
            job = self.gig(fixed_pay=Decimal("90"))
            job.offer_group = group
            job.gig_date = timezone.localdate() + timedelta(days=1 + n)
            job.save(update_fields=["offer_group", "gig_date"])
            made.append(job)
        return made

    def test_the_feed_shows_one_card_not_five(self):
        self.booking(5)
        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(len(response.context["page"].object_list), 1)
        self.assertEqual(response.context["total"], 1)

    def test_the_card_says_how_many_days(self):
        self.booking(5)
        response = self.client.get(reverse("accounts:home"))
        self.assertContains(response, "5 days")
        self.assertContains(response, "per day")

    def test_applying_once_applies_for_every_day(self):
        made = self.booking(5)
        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:apply", args=[made[2].pk]), {"message": "I can do all week."}
        )
        self.assertEqual(
            Application.objects.filter(worker=self.worker_profile).count(), 5
        )

    def test_confirming_them_books_every_day(self):
        from core.state_machine import JobState

        made = self.booking(5)
        self.client.force_login(self.worker_user)
        self.client.post(reverse("jobs:apply", args=[made[0].pk]), {"message": ""})

        application = Application.objects.filter(job=made[0]).get()
        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:application_select", args=[application.pk]))

        for job in made:
            job.refresh_from_db()
            self.assertEqual(job.state, JobState.ACCEPTED, f"{job.gig_date} not booked")
            self.assertEqual(job.assigned_worker, self.worker_profile)

    def test_a_single_day_job_is_unaffected(self):
        from core.state_machine import JobState

        job = self.gig()
        self.client.force_login(self.worker_user)
        self.client.post(reverse("jobs:apply", args=[job.pk]), {"message": ""})
        self.assertEqual(Application.objects.filter(job=job).count(), 1)

        application = Application.objects.get(job=job)
        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:application_select", args=[application.pk]))
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.ACCEPTED)

    def test_re_applying_does_not_stack_rows(self):
        made = self.booking(3)
        self.client.force_login(self.worker_user)
        for _ in range(2):
            self.client.post(reverse("jobs:apply", args=[made[0].pk]), {"message": "hi"})
        self.assertEqual(
            Application.objects.filter(worker=self.worker_profile).count(), 3
        )
