"""Applying, from the side of somebody who has never done the work.

The application has always been one optional field — no CV, no attachments,
nothing required. What it also had was a page that read like a writing task,
and that is the thing being tested here: the words this screen puts in front of
a person who is about to decide they are not qualified.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from .forms import ApplicationForm
from .models import Application
from .tests import JobFactoryMixin


class NothingIsRequiredTests(TestCase):
    """The backend has never asked for more than a note. It still doesn't."""

    def test_the_only_field_is_optional(self):
        self.assertFalse(ApplicationForm().fields["message"].required)

    def test_there_is_exactly_one_field(self):
        """A CV upload, a phone number or a cover letter added here would each
        be a new reason for the person this board is for not to apply."""
        self.assertEqual(list(ApplicationForm().fields), ["message"])

    def test_the_label_says_it_is_optional(self):
        """In the label, where somebody scanning the page reads it — not in a
        placeholder that disappears the moment they type."""
        self.assertIn("optional", str(ApplicationForm().fields["message"].label).lower())

    def test_it_does_not_ask_them_to_make_a_case(self):
        """"What makes you right for this one?" is a fair question to put to a
        tradesperson and an impossible one for somebody who has never held a
        trowel. They read it as proof they are not qualified.

        Checked across label, placeholder and help text together. The first
        pass at this changed the placeholder and left the sentence sitting in
        the model's help_text, rendered under the box — a test that looked at
        one of the three would have called that fixed.
        """
        field = ApplicationForm().fields["message"]
        wording = " ".join([
            str(field.label),
            str(field.help_text),
            str(field.widget.attrs.get("placeholder", "")),
        ]).lower()
        self.assertNotIn("right for this", wording)


class ApplyPageTests(JobFactoryMixin, TestCase):
    def setUp(self):
        self.job = self.gig()
        self.client.force_login(self.worker_user)

    def _page(self):
        return self.client.get(reverse("jobs:apply", args=[self.job.pk]))

    def test_the_page_says_there_is_nothing_to_write(self):
        self.assertContains(self._page(), "There is nothing to write")

    def test_it_says_what_the_client_will_see(self):
        """The commonest reason not to send is not knowing what is being sent."""
        self.assertContains(self._page(), "sees your profile")

    def test_the_page_nowhere_asks_what_makes_them_right_for_it(self):
        """The whole rendered page, not just the field it came from."""
        self.assertNotContains(self._page(), "right for this one")

    def test_the_openers_are_offered(self):
        self.assertContains(self._page(), "data-opener")

    def test_the_openers_start_hidden(self):
        """They are wired up by the script. Without it they would be buttons
        that do nothing, which is worse than not offering them."""
        self.assertContains(self._page(), "data-openers hidden")

    def test_an_empty_application_is_accepted(self):
        """The one that matters. Everything on this page is arrangement; this
        is the behaviour it is arranged around."""
        response = self.client.post(
            reverse("jobs:apply", args=[self.job.pk]), {"message": ""}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Application.objects.filter(
                job=self.job, worker=self.worker_profile
            ).exists()
        )

    def test_a_note_is_still_carried_through(self):
        self.client.post(
            reverse("jobs:apply", args=[self.job.pk]),
            {"message": "I'm free that day and I can be there."},
        )
        application = Application.objects.get(
            job=self.job, worker=self.worker_profile
        )
        self.assertEqual(application.message, "I'm free that day and I can be there.")
