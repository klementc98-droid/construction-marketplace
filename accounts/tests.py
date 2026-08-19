"""Tests for accounts: dual roles, trust display, profile forms."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from config import business_rules as rules
from core.models import Region, Trade

from .forms import WorkerProfileForm
from .models import AvailabilityStatus, ClientProfile, User, WorkerProfile


def make_user(email="w@example.com", **extra) -> User:
    return User.objects.create_user(email=email, full_name="Test User", **extra)


class RoleModelTests(TestCase):
    """The central modelling claim: one account, potentially both roles."""

    def setUp(self):
        self.region = Region.objects.get(slug=rules.DEFAULT_REGION_SLUG)
        self.user = make_user()

    def test_new_user_has_no_roles_and_is_asked_to_pick(self):
        self.assertEqual(self.user.roles, [])
        self.assertTrue(self.user.needs_role_selection)

    def test_a_single_account_can_be_both_worker_and_client(self):
        WorkerProfile.objects.create(user=self.user, region=self.region)
        ClientProfile.objects.create(user=self.user, region=self.region)

        user = User.objects.get(pk=self.user.pk)
        self.assertTrue(user.is_worker)
        self.assertTrue(user.is_client)
        self.assertEqual(sorted(user.roles), ["client", "worker"])
        self.assertFalse(user.needs_role_selection)

    def test_roles_come_from_profiles_not_a_column(self):
        """last_active_role is a UI hint and must grant nothing."""
        self.user.last_active_role = "worker"
        self.user.save()
        self.assertFalse(self.user.is_worker)


class TrustDisplayTests(TestCase):
    """New accounts must never show a 0% stat."""

    def setUp(self):
        self.region = Region.objects.get(slug=rules.DEFAULT_REGION_SLUG)
        self.profile = WorkerProfile.objects.create(user=make_user(), region=self.region)

    def test_brand_new_worker_reports_unknown_not_zero(self):
        self.assertTrue(self.profile.is_new)
        self.assertIsNone(self.profile.average_rating)
        self.assertIsNone(self.profile.completion_rate)
        self.assertIsNone(self.profile.dispute_rate)

    def test_a_worker_with_one_bad_job_is_still_new(self):
        """One job is not a track record; 0% would follow them forever."""
        self.profile.jobs_accepted = 1
        self.profile.jobs_completed = 0
        self.profile.save()
        self.assertIsNone(self.profile.completion_rate)

    def test_rates_appear_once_there_is_enough_history(self):
        self.profile.jobs_accepted = 10
        self.profile.jobs_completed = rules.MIN_JOBS_FOR_PUBLIC_STATS + 5
        self.profile.jobs_disputed = 1
        self.profile.save()

        self.assertFalse(self.profile.is_new)
        self.assertEqual(self.profile.completion_rate, Decimal("0.80"))
        self.assertEqual(self.profile.dispute_rate, Decimal("0.10"))

    def test_average_rating_needs_only_ratings_not_job_count(self):
        self.profile.rating_sum = 9
        self.profile.rating_count = 2
        self.profile.save()
        self.assertEqual(self.profile.average_rating, Decimal("4.5"))

    def test_client_approval_speed_is_averaged_from_running_totals(self):
        client = ClientProfile.objects.create(
            user=make_user("c@example.com"),
            region=self.region,
            jobs_completed=rules.MIN_JOBS_FOR_PUBLIC_STATS,
            approval_seconds_total=3600 * 5,
            approvals_counted=2,
        )
        self.assertEqual(client.average_approval_hours, Decimal("2.5"))

    def test_flagging_records_reason_and_time(self):
        self.profile.flag_for_review("ratings all from one account")
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.flagged_for_review)
        self.assertIsNotNone(self.profile.flagged_at)


class RateDisplayTests(TestCase):
    def setUp(self):
        self.region = Region.objects.get(slug=rules.DEFAULT_REGION_SLUG)
        self.profile = WorkerProfile.objects.create(user=make_user(), region=self.region)

    def test_flat_rate(self):
        self.profile.rate_min = Decimal("30")
        self.assertEqual(self.profile.rate_display, "€30/hr")

    def test_range(self):
        self.profile.rate_min = Decimal("28")
        self.profile.rate_max = Decimal("35")
        self.assertEqual(self.profile.rate_display, "€28-€35/hr")

    def test_daily_rate_unit(self):
        self.profile.rate_type = "daily"
        self.profile.rate_min = Decimal("240")
        self.assertEqual(self.profile.rate_display, "€240/day")

    def test_unset_rate_does_not_render_as_zero(self):
        self.assertEqual(self.profile.rate_display, "Rate on request")


class WorkerProfileFormTests(TestCase):
    def setUp(self):
        self.region = Region.objects.get(slug=rules.DEFAULT_REGION_SLUG)
        self.profile = WorkerProfile.objects.create(user=make_user(), region=self.region)
        self.trade = Trade.objects.get(slug="carpenter")

    def _data(self, **overrides):
        data = {
            "region": self.region.pk,
            "trades": [self.trade.pk],
            "years_experience": 5,
            "rate_type": "hourly",
            "rate_min": "30",
            "availability_status": AvailabilityStatus.AVAILABLE_NOW,
            "service_area": "North side",
            "bio": "",
            "availability_note": "",
            "available_dates": "",
            "open_to_full_time": "False",
        }
        data.update(overrides)
        return data

    def test_valid_minimal_profile(self):
        form = WorkerProfileForm(self._data(), instance=self.profile)
        self.assertTrue(form.is_valid(), form.errors)

    def test_the_full_time_question_must_be_answered(self):
        """A blank answer is worse than either real one — it hides the worker
        from full-time searches without them ever deciding that."""
        data = self._data()
        del data["open_to_full_time"]
        form = WorkerProfileForm(data, instance=self.profile)
        self.assertFalse(form.is_valid())
        self.assertIn("open_to_full_time", form.errors)

    def test_both_answers_are_stored_as_real_booleans(self):
        for raw, expected in [("True", True), ("False", False)]:
            form = WorkerProfileForm(
                self._data(open_to_full_time=raw), instance=self.profile
            )
            self.assertTrue(form.is_valid(), form.errors)
            profile = form.save()
            profile.refresh_from_db()
            self.assertIs(profile.open_to_full_time, expected)

    def test_upper_rate_below_lower_rate_is_rejected(self):
        form = WorkerProfileForm(
            self._data(rate_min="40", rate_max="20"), instance=self.profile
        )
        self.assertFalse(form.is_valid())
        self.assertIn("rate_max", form.errors)

    def test_specific_days_without_dates_is_rejected(self):
        """Otherwise the worker is invisible to date-matched search."""
        form = WorkerProfileForm(
            self._data(availability_status=AvailabilityStatus.SPECIFIC_DAYS),
            instance=self.profile,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("available_dates", form.errors)

    def test_specific_days_saves_parsed_dates(self):
        from datetime import timedelta

        from django.utils import timezone

        # Relative to today, never written out. The form rejects past dates, so
        # a date fixed in the source is a test with an expiry date on it.
        days = [timezone.localdate() + timedelta(days=n) for n in (3, 4)]
        form = WorkerProfileForm(
            self._data(
                availability_status=AvailabilityStatus.SPECIFIC_DAYS,
                available_dates=", ".join(d.isoformat() for d in days),
            ),
            instance=self.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            [d.date.isoformat() for d in self.profile.availability_dates.all()],
            [d.isoformat() for d in days],
        )

    def test_switching_away_from_specific_days_clears_stale_dates(self):
        """Otherwise a worker looks bookable on days they never re-confirmed."""
        from datetime import timedelta

        from django.utils import timezone

        form = WorkerProfileForm(
            self._data(
                availability_status=AvailabilityStatus.SPECIFIC_DAYS,
                available_dates=(timezone.localdate() + timedelta(days=3)).isoformat(),
            ),
            instance=self.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        form = WorkerProfileForm(
            self._data(availability_status=AvailabilityStatus.AVAILABLE_NOW),
            instance=self.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(self.profile.availability_dates.count(), 0)

    def test_malformed_date_is_reported_not_swallowed(self):
        form = WorkerProfileForm(
            self._data(
                availability_status=AvailabilityStatus.SPECIFIC_DAYS,
                available_dates="next tuesday",
            ),
            instance=self.profile,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("available_dates", form.errors)


class ViewTests(TestCase):
    def setUp(self):
        self.region = Region.objects.get(slug=rules.DEFAULT_REGION_SLUG)
        self.user = make_user()

    def test_landing_page_renders_for_anonymous_visitors(self):
        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue with Google")

    def test_signed_in_user_without_a_role_is_sent_to_the_picker(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("accounts:home"))
        self.assertRedirects(response, reverse("accounts:select_role"))

    def test_selecting_both_roles_creates_both_profiles(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("accounts:select_role"), {"roles": ["worker", "client"]}
        )
        self.assertRedirects(response, reverse("accounts:worker_edit"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_worker)
        self.assertTrue(self.user.is_client)

    def test_role_selection_is_idempotent(self):
        """Re-submitting must not blow up on the OneToOne constraint."""
        self.client.force_login(self.user)
        for _ in range(2):
            self.client.post(reverse("accounts:select_role"), {"roles": ["worker"]})
        self.assertEqual(WorkerProfile.objects.filter(user=self.user).count(), 1)

    def test_worker_profile_page_is_publicly_viewable(self):
        profile = WorkerProfile.objects.create(user=self.user, region=self.region)
        response = self.client.get(reverse("accounts:worker_detail", args=[profile.pk]))
        self.assertEqual(response.status_code, 200)

    def test_new_worker_profile_shows_new_not_zero_percent(self):
        profile = WorkerProfile.objects.create(user=self.user, region=self.region)
        response = self.client.get(reverse("accounts:worker_detail", args=[profile.pk]))
        self.assertContains(response, "New")
        # Match a rendered stat value specifically. A bare "0%" search also
        # hits `border-radius:50%` in the stylesheet.
        self.assertNotContains(response, ">0%<")
        self.assertContains(response, "not enough completed jobs")

    def test_licence_is_labelled_self_reported(self):
        """A client must never read our display as verification."""
        profile = WorkerProfile.objects.create(user=self.user, region=self.region)
        profile.trades.add(Trade.objects.get(slug="electrician"))
        profile.licenses.create(trade=Trade.objects.get(slug="electrician"), number="E-123")

        response = self.client.get(reverse("accounts:worker_detail", args=[profile.pk]))
        self.assertContains(response, "Self-reported")
        self.assertContains(response, "does not verify")

    def test_editing_a_profile_requires_sign_in(self):
        response = self.client.get(reverse("accounts:worker_edit"))
        self.assertEqual(response.status_code, 302)

    def test_worker_cannot_delete_another_workers_photo(self):
        other = WorkerProfile.objects.create(
            user=make_user("other@example.com"), region=self.region
        )
        photo = other.portfolio_photos.create(image="portfolio/x.jpg")

        WorkerProfile.objects.create(user=self.user, region=self.region)
        self.client.force_login(self.user)
        response = self.client.post(reverse("accounts:portfolio_delete", args=[photo.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(other.portfolio_photos.count(), 1)


class AccountDetailsTests(TestCase):
    """Name, face and age live on the person, not on either role."""

    def setUp(self):
        self.region = Region.objects.filter(is_active=True).first()
        self.user = make_user("me@example.com")
        self.client.force_login(self.user)

    def _data(self, **overrides):
        data = {
            "full_name": "Nikos Papadopoulos",
            "headline": "Framing and finish carpentry",
            "phone": "+1 555 010 0199",
            "date_of_birth": "1990-05-14",
        }
        data.update(overrides)
        return data

    def test_a_person_can_set_their_name_headline_phone_and_age(self):
        response = self.client.post(reverse("accounts:details"), self._data())
        self.assertRedirects(response, reverse("accounts:details"))

        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "Nikos Papadopoulos")
        self.assertEqual(self.user.headline, "Framing and finish carpentry")
        self.assertEqual(self.user.phone, "+1 555 010 0199")
        self.assertIsNotNone(self.user.age)

    def test_a_blank_name_is_refused(self):
        self.client.post(reverse("accounts:details"), self._data(full_name="   "))
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.full_name, "   ")

    def test_someone_too_young_to_be_on_a_site_is_refused(self):
        from datetime import date

        too_young = date.today().replace(year=date.today().year - 15)
        self.client.post(
            reverse("accounts:details"),
            self._data(date_of_birth=too_young.isoformat()),
        )
        self.user.refresh_from_db()
        self.assertIsNone(self.user.date_of_birth)

    def test_age_is_whole_years_and_handles_a_birthday_still_to_come(self):
        from datetime import date, timedelta

        today = date.today()
        # A birthday tomorrow means they are still the younger age today.
        tomorrow = today + timedelta(days=1)
        self.user.date_of_birth = date(today.year - 30, tomorrow.month, tomorrow.day)
        self.user.save(update_fields=["date_of_birth"])
        self.assertEqual(self.user.age, 29)

    def test_no_date_of_birth_means_no_age_rather_than_zero(self):
        self.assertIsNone(self.user.age)

    def test_initials_fall_back_sensibly_when_there_is_no_photo(self):
        self.user.full_name = "Nikos Papadopoulos"
        self.assertEqual(self.user.initials, "NP")
        self.user.full_name = "Cher"
        self.assertEqual(self.user.initials, "CH")
        self.user.full_name = ""
        self.assertEqual(self.user.initials, "ME")

    def test_display_photo_is_empty_rather_than_broken_when_there_is_none(self):
        self.assertEqual(self.user.display_photo(), "")

    def test_google_picture_is_used_until_something_is_uploaded(self):
        self.user.google_picture_url = "https://lh3.example/a.jpg"
        self.user.save(update_fields=["google_picture_url"])
        self.assertEqual(self.user.display_photo(), "https://lh3.example/a.jpg")

    def test_the_details_page_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("accounts:details"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.headers["Location"])

    def test_one_account_holding_both_roles_has_one_name_and_face(self):
        """The point of putting this on the user: no double data entry."""
        WorkerProfile.objects.create(user=self.user, region=self.region)
        ClientProfile.objects.create(user=self.user, region=self.region)
        self.client.post(reverse("accounts:details"), self._data())
        self.user.refresh_from_db()

        worker_page = self.client.get(
            reverse("accounts:worker_detail", args=[self.user.worker_profile.pk])
        )
        client_page = self.client.get(
            reverse("accounts:client_detail", args=[self.user.client_profile.pk])
        )
        self.assertContains(worker_page, "Nikos Papadopoulos")
        self.assertContains(client_page, "Nikos Papadopoulos")


class PublicProfileTests(TestCase):
    """Profiles are for other people to read, not just their owner."""

    def setUp(self):
        self.region = Region.objects.filter(is_active=True).first()
        self.person = make_user("nikos@example.com")
        self.person.full_name = "Nikos Papadopoulos"
        self.person.headline = "Framing and finish carpentry"
        self.person.save()
        self.worker = WorkerProfile.objects.create(
            user=self.person, region=self.region, years_experience=9
        )

    def test_a_signed_out_visitor_can_read_a_worker_profile(self):
        response = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nikos Papadopoulos")
        self.assertContains(response, "Framing and finish carpentry")

    def test_a_signed_out_visitor_can_read_a_client_profile(self):
        client_profile = ClientProfile.objects.create(
            user=make_user("acme@example.com"), region=self.region
        )
        response = self.client.get(
            reverse("accounts:client_detail", args=[client_profile.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_profile_url_prefers_the_worker_page(self):
        ClientProfile.objects.create(user=self.person, region=self.region)
        self.assertEqual(
            self.person.profile_url,
            reverse("accounts:worker_detail", args=[self.worker.pk]),
        )

    def test_profile_url_falls_back_to_the_client_page(self):
        someone = make_user("hirer@example.com")
        profile = ClientProfile.objects.create(user=someone, region=self.region)
        self.assertEqual(
            someone.profile_url,
            reverse("accounts:client_detail", args=[profile.pk]),
        )

    def test_a_roleless_account_has_no_profile_url_rather_than_a_broken_one(self):
        self.assertEqual(make_user("nobody@example.com").profile_url, "")

    def test_the_two_profiles_of_one_person_link_to_each_other(self):
        client_profile = ClientProfile.objects.create(
            user=self.person, region=self.region
        )
        worker_page = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        client_page = self.client.get(
            reverse("accounts:client_detail", args=[client_profile.pk])
        )
        self.assertContains(
            worker_page, reverse("accounts:client_detail", args=[client_profile.pk])
        )
        self.assertContains(
            client_page, reverse("accounts:worker_detail", args=[self.worker.pk])
        )

    def _accepted_job(self, *, days_ahead=2, state=None):
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from core.models import Trade
        from core.state_machine import JobState
        from jobs.models import Job, JobType

        # Unique per call: a test that books two days calls this twice, and a
        # fixed address collides on the second.
        hirer = make_user(f"books-them-{days_ahead}@example.com")
        return Job.objects.create(
            client=ClientProfile.objects.create(user=hirer, region=self.region),
            job_type=JobType.GIG,
            trade=Trade.objects.first(),
            region=self.region,
            title="Framing",
            description="A day of it.",
            gig_date=timezone.localdate() + timedelta(days=days_ahead),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("120"),
            state=state or JobState.ACCEPTED,
            assigned_worker=self.worker,
        )

    def test_a_booked_day_shows_on_the_profile(self):
        """A client about to offer Tuesday needs to know Tuesday is gone."""
        from django.utils import formats

        job = self._accepted_job()
        self.assertEqual(self.worker.booked_dates, [job.gig_date])

        page = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertContains(page, formats.date_format(job.gig_date, "D j M"))

    def test_a_day_already_gone_is_not_still_shown_as_booked(self):
        """A gig can sit active past its date while a sign-off is waited on."""
        self._accepted_job(days_ahead=-3)
        self.assertEqual(self.worker.booked_dates, [])

    def test_a_finished_day_frees_the_diary(self):
        from core.state_machine import JobState

        self._accepted_job(state=JobState.CLOSED)
        self.assertEqual(self.worker.booked_dates, [])

    def test_a_profile_with_nothing_booked_shows_no_such_section(self):
        page = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertNotContains(page, "Already booked")

    def test_a_free_day_inside_a_run_of_booked_ones_stays_free(self):
        """Booked the 25th and the 30th does not make the 26th taken.

        The 26th is exactly the day somebody is about to try to hire them for,
        and a horizon test — everything up to the last booked day — answered
        "no" to it.
        """
        from datetime import timedelta

        from django.utils import timezone

        booked = self._accepted_job(days_ahead=2)
        far = self._accepted_job(days_ahead=6)
        gap = timezone.localdate() + timedelta(days=4)

        self.assertEqual(self.worker.booked_dates, [booked.gig_date, far.gig_date])
        self.assertFalse(self.worker.is_free_on(booked.gig_date))
        self.assertFalse(self.worker.is_free_on(far.gig_date))
        self.assertTrue(self.worker.is_free_on(gap))

    def test_the_headline_counts_the_days_rather_than_claiming_a_block(self):
        self._accepted_job(days_ahead=2)
        self._accepted_job(days_ahead=6)
        self.assertIn("Booked 2 days", str(self.worker.availability_headline))

    def test_a_solid_run_still_reads_as_busy_until(self):
        """The old wording is right when the diary really is solid."""
        self._accepted_job(days_ahead=1)
        self._accepted_job(days_ahead=2)
        self.assertIn("Busy until", str(self.worker.availability_headline))

    def _finished_job_with_reviews(self):
        """One closed job, rated by both sides, and everyone involved."""
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from core.models import Trade
        from core.state_machine import JobState
        from jobs.models import Job, JobType, Review, ReviewDirection

        hirer = make_user("hirer-with-views@example.com")
        hirer.full_name = "Maria Georgiou"
        hirer.save()
        client_profile = ClientProfile.objects.create(user=hirer, region=self.region)
        job = Job.objects.create(
            client=client_profile,
            job_type=JobType.GIG,
            trade=Trade.objects.first(),
            region=self.region,
            title="Loft conversion",
            description="Two days of framing.",
            gig_date=timezone.localdate() - timedelta(days=2),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("120"),
            state=JobState.CLOSED,
            assigned_worker=self.worker,
        )
        Review.objects.create(
            job=job, author=hirer, direction=ReviewDirection.CLIENT_ON_WORKER,
            rating=5, comment="Turned up early and cleared up after.",
        )
        Review.objects.create(
            job=job, author=self.person, direction=ReviewDirection.WORKER_ON_CLIENT,
            rating=4, comment="Paid the day it was done.",
        )
        return client_profile

    def test_a_worker_profile_shows_the_words_clients_wrote(self):
        """An average is a number nobody learns anything from on its own."""
        self._finished_job_with_reviews()
        page = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertContains(page, "Turned up early and cleared up after.")
        # Not the review pointing the other way — that belongs on the client.
        self.assertNotContains(page, "Paid the day it was done.")

    def test_a_client_profile_shows_the_words_workers_wrote(self):
        client_profile = self._finished_job_with_reviews()
        page = self.client.get(
            reverse("accounts:client_detail", args=[client_profile.pk])
        )
        self.assertContains(page, "Paid the day it was done.")
        self.assertNotContains(page, "Turned up early and cleared up after.")

    def test_a_profile_with_no_reviews_says_so_rather_than_showing_nothing(self):
        page = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertContains(page, "No reviews yet")

    def test_a_client_profile_shows_what_they_have_open(self):
        from jobs.models import Job, JobType

        client_profile = ClientProfile.objects.create(
            user=make_user("hiring@example.com"), region=self.region
        )
        Job.objects.create(
            client=client_profile,
            job_type=JobType.STANDING,
            trade=Trade.objects.get(slug="carpenter"),
            region=self.region,
            title="Carpenter wanted, ongoing",
            description="x",
            rate_type="hourly",
            rate_min=Decimal("30"),
            position_type="ongoing",
        )
        response = self.client.get(
            reverse("accounts:client_detail", args=[client_profile.pk])
        )
        self.assertContains(response, "Carpenter wanted, ongoing")


class FeedFixture(TestCase):
    """Shared fixture: a client with a region and a way to make jobs."""

    def setUp(self):
        self.region = Region.objects.filter(is_active=True).first()
        self.trade = Trade.objects.get(slug="carpenter")
        self.client_profile = ClientProfile.objects.create(
            user=make_user("hirer@example.com"), region=self.region
        )

    def make_jobs(self, count):
        from jobs.models import Job, JobType

        return [
            Job.objects.create(
                client=self.client_profile,
                job_type=JobType.STANDING,
                trade=self.trade,
                region=self.region,
                title=f"Carpenter needed {n}",
                description="Ongoing work on a rebuild.",
                rate_type="hourly",
                rate_min=Decimal("30"),
                position_type="ongoing",
            )
            for n in range(count)
        ]



class FeedTests(FeedFixture):
    """The front page: open work, newest first, endlessly scrollable."""

    def test_a_signed_out_visitor_sees_the_work_not_just_a_sign_in_wall(self):
        self.make_jobs(2)
        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Carpenter needed 0")
        self.assertContains(response, "Continue with Google")

    def test_the_feed_is_capped_and_offers_a_next_page(self):
        from accounts.views import FEED_PAGE_SIZE

        self.make_jobs(FEED_PAGE_SIZE + 3)
        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(len(response.context["page"].object_list), FEED_PAGE_SIZE)
        self.assertTrue(response.context["page"].has_next())
        # data-next only ever appears on a real sentinel; the bare class
        # name also occurs in the stylesheet and the loader script.
        self.assertContains(response, "data-next=")

    def test_the_partial_returns_rows_only_so_they_can_be_appended(self):
        from accounts.views import FEED_PAGE_SIZE

        self.make_jobs(FEED_PAGE_SIZE + 2)
        response = self.client.get(reverse("accounts:home"), {"page": 2, "partial": "1"})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("feed-card", body)
        # No layout, or appending it would nest a whole page inside the feed.
        self.assertNotIn("<html", body)
        self.assertNotIn("tabbar", body)

    def test_the_last_page_has_no_sentinel_so_the_scroll_ends(self):
        self.make_jobs(2)
        response = self.client.get(reverse("accounts:home"))
        self.assertFalse(response.context["page"].has_next())
        self.assertNotContains(response, "data-next=")

    def test_a_filled_job_is_never_offered_as_open_work(self):
        """It may show as filler, but never as something you can apply to."""
        from core.state_machine import JobState

        job = self.make_jobs(1)[0]
        job.state = JobState.ACCEPTED
        job.save(update_fields=["state"])

        response = self.client.get(reverse("accounts:home"))
        self.assertNotIn(job, response.context["page"].object_list)
        self.assertNotContains(
            response, reverse("jobs:apply", args=[job.pk])
        )

    def test_finished_work_is_not_listed(self):
        """It used to pad the end of the feed as "Recently filled".

        A board carrying jobs nobody can apply to makes the reader check each
        card to find out it is over. The record itself is untouched — it still
        counts on both parties' profiles — it just stops being browsable.
        """
        from core.state_machine import JobState

        job = self.make_jobs(1)[0]
        job.state = JobState.ACCEPTED
        job.save(update_fields=["state"])

        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(len(response.context["page"].object_list), 0)
        self.assertNotContains(response, "Recently filled")
        self.assertNotContains(response, job.title)
        self.assertNotIn("filler_jobs", response.context)

    def test_the_finished_job_still_exists(self):
        """Hidden from the board, not deleted. The trust display needs it."""
        from core.state_machine import JobState
        from jobs.models import Job

        job = self.make_jobs(1)[0]
        job.state = JobState.ACCEPTED
        job.save(update_fields=["state"])
        self.client.get(reverse("accounts:home"))
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())

    def test_a_truly_empty_platform_says_so_rather_than_rendering_nothing(self):
        """One message now, not two.

        The second branch covered "nothing open, but there is filler below".
        With finished work no longer listed, both branches said the same thing.
        """
        response = self.client.get(reverse("accounts:home"))
        self.assertContains(response, "No open work at the moment")

    def test_a_nonsense_page_number_does_not_500(self):
        self.make_jobs(2)
        for bad in ["0", "999", "abc", ""]:
            response = self.client.get(reverse("accounts:home"), {"page": bad})
            self.assertEqual(response.status_code, 200, f"page={bad!r}")

    def test_the_account_page_is_still_reachable_and_private(self):
        user = make_user("both@example.com")
        WorkerProfile.objects.create(user=user, region=self.region)
        self.client.force_login(user)
        self.assertEqual(
            self.client.get(reverse("accounts:dashboard")).status_code, 200
        )
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("accounts:dashboard")).status_code, 302
        )



class SeekingFixture(TestCase):
    """"What are they after right now" must never contradict the facts."""

    def setUp(self):
        self.region = Region.objects.filter(is_active=True).first()
        self.trade = Trade.objects.get(slug="carpenter")
        self.worker = WorkerProfile.objects.create(
            user=make_user("chippy@example.com"), region=self.region
        )
        self.client_profile = ClientProfile.objects.create(
            user=make_user("hirer@example.com"), region=self.region
        )

    def make_job(self, state=None, job_type="gig"):
        from datetime import timedelta

        from django.utils import timezone

        from core.state_machine import JobState
        from jobs.models import Job, JobType

        kwargs = dict(
            client=self.client_profile,
            trade=self.trade,
            region=self.region,
            title="Framing help",
            description="x",
            state=state or JobState.POSTED,
        )
        if job_type == "gig":
            kwargs.update(
                job_type=JobType.GIG,
                gig_date=timezone.localdate() + timedelta(days=3),
                gig_hours=Decimal("8"),
                fixed_pay=Decimal("90"),
            )
        else:
            kwargs.update(
                job_type=JobType.STANDING,
                rate_type="hourly",
                rate_min=Decimal("30"),
                position_type="ongoing",
            )
        return Job.objects.create(**kwargs)


class SeekingStatusTests(SeekingFixture):
    """What a worker says they are after, and what the record says."""


    def test_available_now_reads_as_available(self):
        self.assertEqual(self.worker.availability_headline, "Available now")
        self.assertTrue(self.worker.is_open_to_offers)

    def test_a_worker_mid_job_reads_as_busy_until_a_date(self):
        """The contradiction this property exists to prevent — and no job name."""
        from core.state_machine import JobState

        job = self.make_job(state=JobState.IN_PROGRESS)
        job.assigned_worker = self.worker
        job.save(update_fields=["assigned_worker"])

        headline = self.worker.availability_headline
        self.assertIn("Busy until", headline)
        self.assertIn("free from", headline)
        self.assertNotIn(job.title, headline)
        self.assertFalse(self.worker.is_open_to_offers)
        self.assertTrue(self.worker.is_bookable_later)
        self.assertEqual(self.worker.busy_until, job.gig_date)

    def test_a_finished_job_frees_them_up_again(self):
        from core.state_machine import JobState

        job = self.make_job(state=JobState.PAID_OUT)
        job.assigned_worker = self.worker
        job.save(update_fields=["assigned_worker"])

        self.assertFalse(self.worker.is_on_a_job)
        self.assertEqual(self.worker.availability_headline, "Available now")

    def test_saying_unavailable_wins_over_everything(self):
        self.worker.availability_status = AvailabilityStatus.UNAVAILABLE
        self.worker.save(update_fields=["availability_status"])
        self.assertEqual(
            self.worker.availability_headline, "Not taking work right now"
        )
        self.assertFalse(self.worker.is_open_to_offers)

    def test_specific_days_lists_only_days_still_to_come(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import AvailabilityDate

        today = timezone.localdate()
        AvailabilityDate.objects.create(worker=self.worker, date=today - timedelta(days=5))
        AvailabilityDate.objects.create(worker=self.worker, date=today + timedelta(days=2))
        self.worker.availability_status = AvailabilityStatus.SPECIFIC_DAYS
        self.worker.save(update_fields=["availability_status"])

        self.assertEqual(len(self.worker.upcoming_dates), 1)
        self.assertIn("Free", self.worker.availability_headline)

    def test_the_seeking_line_shows_on_the_public_profile(self):
        self.worker.seeking = "Day gigs this week, framing or second fix"
        self.worker.save(update_fields=["seeking"])
        response = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertContains(response, "Looking for right now")
        self.assertContains(response, "Day gigs this week")

    def test_a_client_with_nothing_open_says_so(self):
        self.assertFalse(self.client_profile.is_hiring)
        self.assertEqual(self.client_profile.hiring_headline, "Not hiring right now")

    def test_a_hiring_client_counts_what_is_actually_open(self):
        self.make_job()
        self.make_job()
        self.make_job(job_type="standing")
        self.assertTrue(self.client_profile.is_hiring)
        headline = self.client_profile.hiring_headline
        self.assertIn("2 gig", headline)
        self.assertIn("1 position", headline)

    def test_a_filled_job_stops_counting_as_hiring(self):
        from core.state_machine import JobState

        job = self.make_job()
        self.assertTrue(self.client_profile.is_hiring)
        job.state = JobState.ACCEPTED
        job.save(update_fields=["state"])
        self.assertFalse(self.client_profile.is_hiring)

    def test_the_hiring_line_shows_on_the_public_client_profile(self):
        self.make_job()
        response = self.client.get(
            reverse("accounts:client_detail", args=[self.client_profile.pk])
        )
        self.assertContains(response, "Hiring right now")
        self.assertContains(response, "Hiring for 1 gig")

    def test_the_profile_never_shows_two_contradictory_availabilities(self):
        """It rendered both "On a job" and "Available now" at one point."""
        from core.state_machine import JobState

        job = self.make_job(state=JobState.IN_PROGRESS)
        job.assigned_worker = self.worker
        job.save(update_fields=["assigned_worker"])

        response = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertContains(response, "Busy until")
        self.assertNotContains(response, "Available now")

    def test_a_profile_never_reveals_which_job_someone_is_on(self):
        """Whose site they are on is between them and whoever hired them.

        A rival client browsing profiles gets the date they come free and
        nothing else — not the job title, not the hiring client.
        """
        from core.state_machine import JobState

        # A name of its own, so "did the client's name leak?" is a real
        # question — make_user() gives everyone the same one by default, and the
        # worker's own name belongs on their own page.
        hirer = self.client_profile.user
        hirer.full_name = "Ridgeline Construction Ltd"
        hirer.save(update_fields=["full_name"])

        job = self.make_job(state=JobState.IN_PROGRESS)
        job.title = "Second storey rebuild, Elm Street"
        job.assigned_worker = self.worker
        job.save(update_fields=["assigned_worker", "title"])

        body = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        ).content.decode()

        self.assertNotIn(job.title, body)
        self.assertNotIn("Ridgeline Construction", body)
        self.assertNotIn("Elm Street", body)
        self.assertIn("Busy until", body)

    def test_an_open_ended_placement_has_no_date_to_quote(self):
        from core.state_machine import JobState

        job = self.make_job(state=JobState.ACCEPTED, job_type="standing")
        job.assigned_worker = self.worker
        job.save(update_fields=["assigned_worker"])

        self.assertTrue(self.worker.has_open_ended_commitment)
        self.assertIsNone(self.worker.busy_until)
        self.assertIsNone(self.worker.available_from)
        self.assertEqual(
            self.worker.availability_headline, "On a longer-term placement"
        )

    def test_is_free_on_answers_the_date_a_client_actually_asked_about(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.state_machine import JobState

        booked = self.make_job(state=JobState.ESCROW_HELD)
        booked.assigned_worker = self.worker
        booked.save(update_fields=["assigned_worker"])

        self.assertFalse(self.worker.is_free_on(booked.gig_date))
        self.assertTrue(
            self.worker.is_free_on(booked.gig_date + timedelta(days=1))
        )

    def test_is_free_on_says_it_cannot_tell_rather_than_guessing(self):
        """Absence of a booking is not evidence of availability."""
        from datetime import timedelta

        from django.utils import timezone

        self.worker.availability_status = AvailabilityStatus.ONGOING
        self.worker.save(update_fields=["availability_status"])
        self.assertIsNone(
            self.worker.is_free_on(timezone.localdate() + timedelta(days=4))
        )


class TemplateHygieneTests(TestCase):
    """Django's {# #} is single-line only.

    A multi-line one renders every following line as visible text, which is
    how "the availability card that used to sit here has gone" ended up on a
    public profile page. Cheap to check, invisible until someone reads the page.
    """

    def test_no_template_leaks_a_multi_line_comment(self):
        import pathlib

        from django.conf import settings

        offenders = []
        for template in pathlib.Path(settings.BASE_DIR, "templates").rglob("*.html"):
            for number, line in enumerate(
                template.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "{#" in line and "#}" not in line.split("{#", 1)[1]:
                    offenders.append(f"{template.name}:{number}")
        self.assertEqual(
            offenders,
            [],
            "Use {% comment %}…{% endcomment %} for multi-line comments: "
            + ", ".join(offenders),
        )

    def test_a_rendered_page_contains_no_template_syntax(self):
        region = Region.objects.filter(is_active=True).first()
        worker = WorkerProfile.objects.create(
            user=make_user("leak@example.com"), region=region
        )
        for url in [
            reverse("accounts:home"),
            reverse("accounts:worker_detail", args=[worker.pk]),
        ]:
            body = self.client.get(url).content.decode()
            self.assertNotIn("{#", body, f"{url} leaked a comment")
            self.assertNotIn("{%", body, f"{url} leaked a tag")


class HomeTabsTests(FeedFixture):
    """The home page is one of two lists, chosen by a switch at the top."""

    def test_the_switch_offers_both_sides(self):
        response = self.client.get(reverse("accounts:home"))
        self.assertContains(response, "home-tabs")
        self.assertContains(response, "?show=workers")

    def test_work_is_the_default(self):
        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(response.context["showing"], "work")
        self.assertContains(response, "<h2>Find work</h2>")
        self.assertNotContains(response, "<h2>Find workers</h2>")

    def test_asking_for_workers_shows_workers_instead(self):
        WorkerProfile.objects.create(
            user=make_user("sparks@example.com"), region=self.region
        )
        response = self.client.get(reverse("accounts:home"), {"show": "workers"})
        self.assertEqual(response.context["showing"], "workers")
        self.assertContains(response, "<h2>Find workers</h2>")
        self.assertNotContains(response, "<h2>Find work</h2>")
        self.assertEqual(len(response.context["workers"]), 1)

    def test_the_hidden_side_is_not_queried(self):
        """The tabs are links, so the other list costs nothing until asked for."""
        response = self.client.get(reverse("accounts:home"))
        self.assertNotIn("workers", response.context)

    def test_nonsense_lands_on_the_feed_rather_than_a_blank_page(self):
        response = self.client.get(reverse("accounts:home"), {"show": "banana"})
        self.assertEqual(response.context["showing"], "work")
        self.assertContains(response, "<h2>Find work</h2>")

    def test_the_workers_list_is_capped(self):
        from accounts.views import PREVIEW_WORKERS

        for n in range(PREVIEW_WORKERS + 3):
            WorkerProfile.objects.create(
                user=make_user(f"w{n}@example.com"), region=self.region
            )
        response = self.client.get(reverse("accounts:home"), {"show": "workers"})
        self.assertEqual(len(response.context["workers"]), PREVIEW_WORKERS)

    def test_an_empty_workers_tab_says_so(self):
        response = self.client.get(reverse("accounts:home"), {"show": "workers"})
        self.assertContains(response, "Nobody's listed here yet.")

    def test_the_workers_tab_carries_no_scroll_sentinel(self):
        """Nothing to page through here, so the loader must find no target."""
        from accounts.views import FEED_PAGE_SIZE

        self.make_jobs(FEED_PAGE_SIZE + 2)
        response = self.client.get(reverse("accounts:home"), {"show": "workers"})
        self.assertNotContains(response, "feed-sentinel")

    def test_the_scroll_partial_carries_no_workers(self):
        """Appended once per page, so a worker card here would repeat forever."""
        from accounts.views import FEED_PAGE_SIZE

        self.make_jobs(FEED_PAGE_SIZE + 2)
        WorkerProfile.objects.create(
            user=make_user("sparks@example.com"), region=self.region
        )
        response = self.client.get(
            reverse("accounts:home"), {"partial": "1", "page": 2}
        )
        self.assertNotIn("workers", response.context)
        self.assertNotContains(response, "<h2>Find workers</h2>")


class StaleCommitmentTests(SeekingFixture):
    """A booking whose last day has gone by is not a booking.

    A gig can sit in an active state well past its date — waiting on a
    sign-off, or on an approval window that has not run out. The worker is
    plainly not busy on a day that has been and gone, and every page that
    quotes a date has to agree about that.
    """

    def past_job(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.state_machine import JobState

        job = self.make_job(state=JobState.ACCEPTED)
        job.assigned_worker = self.worker
        job.gig_date = timezone.localdate() - timedelta(days=5)
        job.save(update_fields=["assigned_worker", "gig_date"])
        return job

    def test_a_finished_day_no_longer_makes_them_busy(self):
        self.past_job()
        self.assertIsNone(self.worker.busy_until)
        self.assertIsNone(self.worker.available_from)

    def test_they_read_as_available_again(self):
        """The bug as reported: "free from 1 Aug" still showing in mid-August."""
        self.past_job()
        self.assertEqual(self.worker.availability_headline, "Available now")
        self.assertEqual(self.worker.availability_tone, "ok")

    def test_the_profile_page_stops_quoting_the_old_date(self):
        from django.urls import reverse

        self.past_job()
        response = self.client.get(
            reverse("accounts:worker_detail", args=[self.worker.pk])
        )
        self.assertNotContains(response, "Booked through")

    def test_a_future_booking_still_reads_as_busy(self):
        """The guard must not swallow the case it exists to report."""
        from core.state_machine import JobState

        job = self.make_job(state=JobState.ACCEPTED)
        job.assigned_worker = self.worker
        job.save(update_fields=["assigned_worker"])

        self.assertEqual(self.worker.busy_until, job.gig_date)
        self.assertEqual(self.worker.availability_tone, "soon")

    def test_a_past_day_no_longer_blocks_being_booked_again(self):
        from datetime import timedelta

        from django.utils import timezone

        self.past_job()
        self.worker.availability_status = AvailabilityStatus.AVAILABLE_NOW
        self.worker.save(update_fields=["availability_status"])
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.assertTrue(self.worker.is_free_on(tomorrow))
