"""Getting in.

Google is the only way in — see SOCIALACCOUNT_ONLY in settings. That makes
allauth's own sign-in page a screen with one button on it, and it renders in
allauth's bare layout rather than in this app's, so pressing Sign in took
somebody to a page that does not look like the site they were on.

These tests hold two things: that the visible ways in go straight to Google,
and that the page they used to land on is still there, still ours, for the
routes that cannot skip it.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from core.models import Region, Trade
from jobs.models import Job, JobType
from .models import ClientProfile
from .tests import make_user


class SignInLinksTests(TestCase):
    """Every Sign in on a signed-out page."""

    def test_the_header_goes_straight_to_google(self):
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "/accounts/google/login/?process=login")

    def test_it_carries_the_page_it_was_pressed_on(self):
        """Signing in from the board should come back to the board, not dump
        the reader on the home page wondering what they were doing."""
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "next=/jobs/")

    def test_no_signed_out_page_still_points_at_the_bare_allauth_page(self):
        """The one that started this: a link to a screen with one button on it,
        wearing a layout from another site."""
        for name in ("jobs:list", "accounts:home", "core:about"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertNotContains(response, 'href="/accounts/login/"')


class SignInFromAJobTests(TestCase):
    """The one place where coming back to the right page really matters."""

    @classmethod
    def setUpTestData(cls):
        region = Region.objects.filter(is_active=True).first()
        poster = ClientProfile.objects.create(
            user=make_user("poster@example.com"), region=region
        )
        cls.job = Job.objects.create(
            client=poster,
            region=region,
            trade=Trade.objects.get(slug="carpenter"),
            job_type=JobType.STANDING,
            title="Carpenter wanted",
            description="Ongoing work.",
            rate_type="hourly",
            rate_min=30,
            position_type="ongoing",
        )

    def test_signing_in_to_apply_returns_to_the_job(self):
        """Somebody who signed in to apply and landed on the home page has to
        find the job again — and the commonest outcome is that they don't."""
        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "next=/jobs/%s/" % self.job.pk)


class LoginPageTests(TestCase):
    """It still exists. login_required sends people here, and so does a
    cancelled or failed Google flow."""

    def test_it_is_our_page_now(self):
        response = self.client.get(reverse("account_login"))
        self.assertContains(response, "Continue with Google")
        self.assertTemplateUsed(response, "account/login.html")

    def test_it_wears_the_app_chrome(self):
        """The tell that it is ours: allauth's layout has no stylesheet and a
        "Menu:" list where the header should be."""
        response = self.client.get(reverse("account_login"))
        self.assertContains(response, "Construction's Finest")
        self.assertNotContains(response, "<strong>Menu:</strong>")

    def test_it_says_what_google_hands_over(self):
        """The question anybody hesitating on that button is asking."""
        response = self.client.get(reverse("account_login"))
        self.assertContains(response, "nothing else", status_code=200)

    def test_it_carries_next_through_to_google(self):
        response = self.client.get(reverse("account_login"), {"next": "/jobs/"})
        self.assertContains(response, "next=%2Fjobs%2F")
