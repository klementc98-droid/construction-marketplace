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
        self.assertEqual(self.profile.rate_display, "$30/hr")

    def test_range(self):
        self.profile.rate_min = Decimal("28")
        self.profile.rate_max = Decimal("35")
        self.assertEqual(self.profile.rate_display, "$28-$35/hr")

    def test_daily_rate_unit(self):
        self.profile.rate_type = "daily"
        self.profile.rate_min = Decimal("240")
        self.assertEqual(self.profile.rate_display, "$240/day")

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
        form = WorkerProfileForm(
            self._data(
                availability_status=AvailabilityStatus.SPECIFIC_DAYS,
                available_dates="2026-08-04, 2026-08-05",
            ),
            instance=self.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            [d.date.isoformat() for d in self.profile.availability_dates.all()],
            ["2026-08-04", "2026-08-05"],
        )

    def test_switching_away_from_specific_days_clears_stale_dates(self):
        """Otherwise a worker looks bookable on days they never re-confirmed."""
        form = WorkerProfileForm(
            self._data(
                availability_status=AvailabilityStatus.SPECIFIC_DAYS,
                available_dates="2026-08-04",
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


class FeedTests(TestCase):
    """The front page: open work, newest first, endlessly scrollable."""

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

    def test_the_feed_falls_back_to_filler_rather_than_going_blank(self):
        from core.state_machine import JobState

        job = self.make_jobs(1)[0]
        job.state = JobState.ACCEPTED
        job.save(update_fields=["state"])
        WorkerProfile.objects.create(
            user=make_user("chippy@example.com"), region=self.region
        )

        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(len(response.context["page"].object_list), 0)
        # Something to scroll: closed jobs below the feed, and the workers
        # section, which now stands on its own rather than being filler.
        self.assertIn(job, list(response.context["filler_jobs"]))
        self.assertEqual(len(response.context["preview_workers"]), 1)
        self.assertContains(response, "Recently filled")
        self.assertContains(response, "Find workers")

    def test_filler_only_appears_once_the_open_work_runs_out(self):
        from accounts.views import FEED_PAGE_SIZE

        self.make_jobs(FEED_PAGE_SIZE + 2)
        first = self.client.get(reverse("accounts:home"))
        self.assertNotIn("filler_jobs", first.context)

        last = self.client.get(reverse("accounts:home"), {"page": 2})
        self.assertIn("filler_jobs", last.context)

    def test_a_truly_empty_platform_says_so_rather_than_rendering_nothing(self):
        response = self.client.get(reverse("accounts:home"))
        self.assertContains(response, "Nothing on the board yet")

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


class SeekingStatusTests(TestCase):
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


class HomeSectionsTests(FeedTests):
    """The home page is two sections: find workers, then find work."""

    def test_both_sections_are_on_the_page(self):
        WorkerProfile.objects.create(
            user=make_user("sparks@example.com"), region=self.region
        )
        response = self.client.get(reverse("accounts:home"))
        self.assertContains(response, "<h2>Find workers</h2>")
        self.assertContains(response, "<h2>Find work</h2>")
        self.assertContains(response, reverse("jobs:worker_list"))
        self.assertContains(response, reverse("jobs:list"))

    def test_workers_show_without_waiting_for_the_job_feed_to_run_out(self):
        """The bug this section exists to fix.

        As filler they appeared only on the last page of the feed, so on a
        board with plenty of open work nobody ever saw them.
        """
        from accounts.views import FEED_PAGE_SIZE

        self.make_jobs(FEED_PAGE_SIZE + 2)
        WorkerProfile.objects.create(
            user=make_user("sparks@example.com"), region=self.region
        )
        first = self.client.get(reverse("accounts:home"))
        self.assertTrue(first.context["page"].has_next())
        self.assertEqual(len(first.context["preview_workers"]), 1)
        self.assertContains(first, "<h2>Find workers</h2>")

    def test_the_workers_section_is_capped(self):
        from accounts.views import PREVIEW_WORKERS

        for n in range(PREVIEW_WORKERS + 3):
            WorkerProfile.objects.create(
                user=make_user(f"w{n}@example.com"), region=self.region
            )
        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(len(response.context["preview_workers"]), PREVIEW_WORKERS)

    def test_the_scroll_partial_carries_no_workers(self):
        """Appended once per page, so a worker card here would repeat forever."""
        from accounts.views import FEED_PAGE_SIZE

        self.make_jobs(FEED_PAGE_SIZE + 2)
        WorkerProfile.objects.create(
            user=make_user("sparks@example.com"), region=self.region
        )
        response = self.client.get(reverse("accounts:home"), {"partial": "1", "page": 2})
        self.assertNotIn("preview_workers", response.context)
        self.assertNotContains(response, "<h2>Find workers</h2>")

    def test_the_section_is_absent_when_nobody_has_signed_up(self):
        response = self.client.get(reverse("accounts:home"))
        self.assertEqual(len(response.context["preview_workers"]), 0)
        self.assertNotContains(response, "<h2>Find workers</h2>")
