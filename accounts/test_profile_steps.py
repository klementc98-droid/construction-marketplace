"""Writing a helper's profile, one question at a time.

The same arrangement as posting a job, and the tests hold the same two things:
that every visible field belongs to exactly one step — a field belonging to
none renders nowhere and the first sign of it is a profile saved without a rate
— and that the page is stepped only the first time.

The second half matters more here than on the job form. This page is where
somebody who has never done the work decides whether they belong on the board,
and it is also where the same person comes back six months later to change one
number.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from core.models import Region, Trade
from jobs.tests import make_user

from .forms import WorkerProfileForm
from .models import WorkerProfile


class ProfileStepCoverageTests(TestCase):
    def test_every_visible_field_belongs_to_a_step(self):
        form = WorkerProfileForm()
        grouped = set(WorkerProfileForm.step_field_names())
        visible = {f.name for f in form if not f.is_hidden}
        self.assertEqual(visible - grouped, set())

    def test_no_step_names_a_field_that_does_not_exist(self):
        form = WorkerProfileForm()
        for name in WorkerProfileForm.step_field_names():
            self.assertIn(name, form.fields, name)

    def test_no_field_is_asked_twice(self):
        names = WorkerProfileForm.step_field_names()
        self.assertEqual(len(names), len(set(names)))

    def test_it_asks_six_questions(self):
        self.assertEqual(len(WorkerProfileForm().steps()), 6)

    def test_the_first_question_is_the_easy_one(self):
        """The trade is the one thing anybody can answer without thinking, and
        starting there is what gets the second question answered."""
        self.assertEqual(WorkerProfileForm.STEPS[0][1][0], "trades")

    def test_experience_shares_a_screen_with_the_trade(self):
        """On its own it reads as a test somebody is about to fail. Zero is a
        perfectly good answer here and the layout should not imply otherwise."""
        first = WorkerProfileForm().steps()[0]
        self.assertIn("years_experience", first["field_names"])

    def test_the_resume_is_folded_and_not_dropped(self):
        """Optional, answered by roughly nobody on this board, and still worth
        keeping for the few who have one."""
        last = WorkerProfileForm().steps()[-1]
        self.assertEqual([f.name for f in last["folded"]], ["cv"])
        self.assertNotIn("cv", [f.name for f in last["fields"]])


class ProfilePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.filter(is_active=True).first()
        cls.carpentry = Trade.objects.get(slug="carpenter")
        cls.user = make_user("newhand@example.com")
        cls.profile = WorkerProfile.objects.create(user=cls.user, region=cls.region)

    def setUp(self):
        self.client.force_login(self.user)

    def page(self):
        return self.client.get(reverse("accounts:worker_edit"))

    def test_a_new_profile_is_asked_one_question_at_a_time(self):
        self.assertContains(self.page(), "data-steps")

    def test_every_step_is_in_the_page_already(self):
        """Nothing is held on the server between steps, so a profile half
        written cannot be stranded anywhere."""
        response = self.page()
        for step in WorkerProfileForm().steps():
            with self.subTest(step=step["index"]):
                self.assertContains(response, 'data-step="%s"' % step["index"])

    def test_it_says_up_front_that_nothing_is_required_of_them(self):
        """The sentence the whole product rests on, on the screen where
        somebody is deciding whether they qualify."""
        self.assertContains(self.page(), "needs experience, a licence or a CV")

    def test_the_licence_boxes_sit_with_the_trades(self):
        """They are not form fields — one text box per regulated trade, saved
        separately — and they belong beside the trades or nowhere."""
        response = self.page()
        self.assertContains(response, "Licence numbers (optional)")

    def test_a_profile_that_has_been_written_is_not_stepped(self):
        """Somebody who came back to change their rate should not be walked
        through six screens to reach it."""
        self.profile.trades.add(self.carpentry)
        self.assertNotContains(self.page(), "data-steps")

    def test_photos_of_past_work_wait_until_there_is_a_profile(self):
        """For somebody starting out it is a question with no good answer, and
        putting it on the first screen is the wrong first impression."""
        self.assertNotContains(self.page(), "Photos of past work")

        self.profile.trades.add(self.carpentry)
        self.assertContains(self.page(), "Photos of past work")

    def test_saving_still_takes_one_request(self):
        """The whole point of rendering the steps rather than storing them."""
        response = self.client.post(
            reverse("accounts:worker_edit"),
            {
                "region": self.region.pk,
                "trades": [self.carpentry.pk],
                "years_experience": "0",
                "rate_type": "hourly",
                "rate_min": "12",
                "service_area": "north side",
                "seeking": "day work",
                "availability_status": "available_now",
                "open_to_full_time": "True",
                "bio": "New to it, keen to learn.",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.years_experience, 0)
        self.assertEqual(list(self.profile.trades.all()), [self.carpentry])

    def test_a_beginner_can_save_a_profile_with_nothing_behind_them(self):
        """No experience, no licence, no CV, no photos. If this ever stops
        working the board stops being for the people it is for."""
        self.client.post(
            reverse("accounts:worker_edit"),
            {
                "region": self.region.pk,
                "trades": [self.carpentry.pk],
                "years_experience": "0",
                "rate_type": "hourly",
                "rate_min": "10",
                "service_area": "anywhere",
                "seeking": "anything going",
                "availability_status": "available_now",
                "open_to_full_time": "False",
                "bio": "",
            },
        )
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.cv)
        self.assertEqual(self.profile.years_experience, 0)
