"""Posting a job as a sequence of questions.

The wizard is a rendering choice, not a new state machine: every fieldset is in
the page, one submit posts the lot, and the server validates what it always
validated. So the tests here are about the two ways that arrangement can go
wrong — a field that belongs to no step and therefore renders nowhere, and a
step that promises a question it does not ask.

What must keep working is that a job can still be posted from this page with a
single POST. That is covered by the existing posting tests, which go through
the same view and the same form; nothing here replaces them.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import GigForm, OfferForm, StandingForm
from .tests import JobFactoryMixin


class StepCoverageTests(TestCase):
    """Every question is asked exactly once.

    This is the test the arrangement exists for. The grouping is written by
    hand, the fields are not, and the failure it guards against is silent: a
    field added to the form and forgotten here would vanish from the page, and
    the first sign of it would be a job posted without a price.
    """

    forms = (GigForm, OfferForm, StandingForm)

    def test_every_visible_field_belongs_to_a_step(self):
        for form_class in self.forms:
            with self.subTest(form=form_class.__name__):
                form = form_class()
                grouped = set(form_class.step_field_names())
                visible = {f.name for f in form if not f.is_hidden}
                self.assertEqual(visible - grouped, set())

    def test_no_step_names_a_field_that_does_not_exist(self):
        """A renamed field would otherwise leave a step quietly short."""
        for form_class in self.forms:
            with self.subTest(form=form_class.__name__):
                form = form_class()
                for name in form_class.step_field_names():
                    self.assertIn(name, form.fields, name)

    def test_no_field_is_asked_twice(self):
        """Two inputs for one field would post the second one's value."""
        for form_class in self.forms:
            with self.subTest(form=form_class.__name__):
                names = form_class.step_field_names()
                self.assertEqual(len(names), len(set(names)))

    def test_a_folded_field_is_still_a_field_of_the_step(self):
        """Folding is a rendering choice. A field behind the disclosure still
        posts, still validates, and still counts as covered."""
        step = GigForm().steps()[-1]
        self.assertEqual([f.name for f in step["folded"]],
                         ["site_latitude", "site_longitude"])


    def test_the_steps_are_numbered_from_one(self):
        for form_class in self.forms:
            with self.subTest(form=form_class.__name__):
                steps = form_class().steps()
                self.assertEqual([s["index"] for s in steps],
                                 list(range(1, len(steps) + 1)))

    def test_every_step_carries_the_total_and_knows_the_last(self):
        """The progress reads "step n of N" from these, and the buttons swap
        Next for Post it on the strength of is_last."""
        for form_class in self.forms:
            with self.subTest(form=form_class.__name__):
                steps = form_class().steps()
                self.assertTrue(all(s["total"] == len(steps) for s in steps))
                self.assertEqual([s["is_last"] for s in steps][-1], True)
                self.assertNotIn(True, [s["is_last"] for s in steps][:-1])

    def test_a_step_with_nothing_left_to_ask_is_not_counted(self):
        """The region is filled in and hidden while there is one market. A step
        holding only it would be a screen with no question on it, and counting
        it would make the progress promise a step that never appears."""
        for form_class in self.forms:
            with self.subTest(form=form_class.__name__):
                for step in form_class().steps():
                    self.assertTrue(step["fields"])

    @override_settings(STRIPE_SECRET_KEY="sk_test_configured")
    def test_a_gig_asks_six_questions(self):
        """Six on a platform that can hold money. The sixth is how it is paid,
        and it is not asked at all where the answer could only be one thing —
        see EscrowQuestionTests."""
        self.assertEqual(len(GigForm().steps()), 6)

    @override_settings(STRIPE_SECRET_KEY="sk_test_configured")
    def test_a_position_asks_fewer(self):
        """It has no date and no escrow decision — nothing is held for work
        with no day attached."""
        self.assertLess(len(StandingForm().steps()), len(GigForm().steps()))

    @override_settings(STRIPE_SECRET_KEY="sk_test_configured")
    def test_the_optional_coordinates_are_not_the_question(self):
        """Step six asks how the job is paid. Two decimal inputs for the site's
        position are worth keeping and are not what is being asked."""
        step = GigForm().steps()[-1]
        self.assertEqual([f.name for f in step["fields"]], ["use_escrow"])

    def test_the_first_question_is_the_easy_one(self):
        """Starting on a question with no wrong answer is what gets the second
        one answered."""
        for form_class in self.forms:
            with self.subTest(form=form_class.__name__):
                self.assertEqual(form_class.STEPS[0][1], ["trade"])


class EscrowQuestionTests(TestCase):
    """The payment question, on a deployment that cannot take a payment.

    Without Stripe keys the app runs and every gig settles directly — the
    default, and the ordinary case. What it also did was keep offering "hold it
    in escrow", so a client could choose it, a worker could accept it, and the
    funding page would then say the platform is not configured. Nothing
    crashed. It was a door with no room behind it, and the person who walked
    through it was holding the money.
    """

    def test_no_stripe_no_question(self):
        with override_settings(STRIPE_SECRET_KEY=""):
            questions = [str(s["question"]) for s in GigForm().steps()]
        self.assertNotIn("How is it paid?", questions)

    def test_and_the_coordinates_are_not_stranded_with_it(self):
        """They were folded into the payment step. Losing that screen must not
        lose them — they move up rather than disappearing."""
        with override_settings(STRIPE_SECRET_KEY=""):
            last = GigForm().steps()[-1]
        self.assertEqual([f.name for f in last["folded"]],
                         ["site_latitude", "site_longitude"])

    def test_every_visible_field_still_belongs_to_a_step(self):
        """The coverage rule holds in both configurations, which is the whole
        reason the coordinates had somewhere to go."""
        with override_settings(STRIPE_SECRET_KEY=""):
            form = GigForm()
            covered = {n for _q, f, *rest in form.STEPS
                       for n in list(f) + (list(rest[0]) if rest else [])}
            visible = {b.name for b in form if not b.is_hidden}
        self.assertEqual(visible - covered, set())

    def test_posting_escrow_anyway_is_ignored(self):
        """The rule, not the rendering. A hidden input is a value a caller can
        post regardless, and this one decides whether the platform is expected
        to hold somebody's money — so the field is disabled, which makes Django
        use the initial and ignore whatever arrived.
        """
        with override_settings(STRIPE_SECRET_KEY=""):
            form = GigForm(data={"use_escrow": "True"})
            form.is_valid()
            self.assertIs(form.cleaned_data.get("use_escrow"), False)

    def test_a_counter_cannot_ask_for_it_either(self):
        """"Yes, but not on trust" is a good reason to counter — when there is
        an escrow to ask for. Where there is not, it proposes terms nobody
        could honour."""
        from .forms import CounterForm

        with override_settings(STRIPE_SECRET_KEY=""):
            self.assertTrue(CounterForm().fields["use_escrow"].disabled)

    def test_with_stripe_the_question_comes_back(self):
        with override_settings(STRIPE_SECRET_KEY="sk_test_configured"):
            questions = [str(s["question"]) for s in GigForm().steps()]
        self.assertIn("How is it paid?", questions)


class StepRenderingTests(JobFactoryMixin, TestCase):
    """What the page actually sends, with and without the script."""

    def setUp(self):
        self.client.force_login(self.client_user)

    def _post_page(self):
        return self.client.get(reverse("jobs:post", args=["gig"]))

    def test_the_form_asks_the_script_to_step_it(self):
        self.assertContains(self._post_page(), "data-steps")

    def test_every_step_is_in_the_page_already(self):
        """Nothing is fetched between steps and nothing is held on the server.
        A half-finished job that exists nowhere cannot be stranded."""
        response = self._post_page()
        for step in GigForm().steps():
            with self.subTest(step=step["index"]):
                self.assertContains(response, 'data-step="%s"' % step["index"])

    def test_the_questions_are_asked_in_words(self):
        self.assertContains(self._post_page(), "Which days?")

    def test_the_hidden_region_is_still_posted(self):
        """It is filled in and hidden, and it is not in any step's fields —
        so it has to be rendered outside them or the form posts without it."""
        self.assertContains(self._post_page(), 'name="region"')

    def test_the_progress_starts_hidden(self):
        """With no script the whole form is on one screen, and a progress bar
        there would describe a sequence that is not happening."""
        response = self._post_page()
        self.assertContains(response, "data-step-progress")
        self.assertContains(response, "steps-progress")

    def test_the_real_submit_is_always_there(self):
        """Back and Next are added by the script and hidden without it. Post it
        is the form's own button and must work with nothing running."""
        response = self._post_page()
        self.assertContains(response, "step-submit")
        self.assertContains(response, "Post it")

    def test_posting_still_takes_one_request(self):
        """The whole point of rendering the steps rather than storing them."""
        before = self.client_profile.jobs.count()
        response = self.client.post(
            reverse("jobs:post", args=["gig"]),
            {
                "trade": self.carpentry.pk,
                "title": "Carrying and mixing",
                "description": "A day on a house build.",
                "experience_wanted": "none",
                "region": self.region.pk,
                "location": "Nea Smyrni",
                "gig_dates": "2027-03-04",
                "gig_hours": "8",
                "fixed_pay": "80",
                "use_escrow": "True",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client_profile.jobs.count(), before + 1)


class EditIsNotAWizardTests(JobFactoryMixin, TestCase):
    """Somebody editing came to change one field."""

    def test_editing_shows_the_whole_form(self):
        """Walking them through six screens to reach the price would be a worse
        form than the one the wizard replaced."""
        job = self.gig()
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:edit", args=[job.pk]))
        self.assertNotContains(response, "data-steps")
        self.assertNotContains(response, 'class="step"')


class OfferAsksTheSameQuestionsTests(TestCase):
    """Writing to one person is the posting form with a note on the end.

    The value of that is entirely in it being recognisable: a client who has
    posted a gig has answered these questions, in this order, on screens that
    looked like this. Anything that lets the two drift is what these guard.
    """

    @override_settings(STRIPE_SECRET_KEY="sk_test_configured")
    def test_the_questions_are_the_posting_forms_questions(self):
        posting = [q for q, *_ in GigForm.STEPS]
        offering = [q for q, *_ in OfferForm.STEPS]
        self.assertEqual(offering[: len(posting)], posting[: len(posting)])

    @override_settings(STRIPE_SECRET_KEY="sk_test_configured")
    def test_and_they_are_asked_in_the_same_order(self):
        shared = [
            name
            for name in GigForm.step_field_names()
            if name in OfferForm().fields
        ]
        offering = [
            name for name in OfferForm.step_field_names() if name in shared
        ]
        self.assertEqual(offering, shared)

    @override_settings(STRIPE_SECRET_KEY="sk_test_configured")
    def test_the_covering_note_is_the_last_question(self):
        """Last because it is the one answer that depends on the others — what
        you write changes once you know you are asking for three days."""
        step = OfferForm().steps()[-1]
        self.assertEqual([f.name for f in step["fields"]], ["note"])
        self.assertTrue(step["is_last"])

    def test_there_are_no_site_coordinates_to_fold_away(self):
        """GigForm moves them onto its last step when escrow is not asked for.
        This form has no such fields, and the last step here is the note."""
        for step in OfferForm().steps():
            with self.subTest(step=step["index"]):
                self.assertEqual(step["folded"], [])


class OfferRenderingTests(JobFactoryMixin, TestCase):
    """The offer page, as it arrives in a browser."""

    def setUp(self):
        self.client.force_login(self.client_user)

    def _page(self):
        return self.client.get(
            reverse("jobs:offer", args=[self.worker_profile.pk])
        )

    def test_the_form_asks_the_script_to_step_it(self):
        self.assertContains(self._page(), "data-steps")

    def test_every_step_is_in_the_page_already(self):
        response = self._page()
        for step in OfferForm().steps():
            with self.subTest(step=step["index"]):
                self.assertContains(response, 'data-step="%s"' % step["index"])

    def test_the_questions_are_asked_in_words(self):
        response = self._page()
        self.assertContains(response, "What kind of work is it?")
        self.assertContains(response, "Which days?")

    def test_the_real_submit_is_always_there(self):
        """Back and Next are the script's. This one has to work without it."""
        response = self._page()
        self.assertContains(response, "step-submit")
        self.assertContains(response, "Send the offer")

    def test_the_hidden_region_is_still_posted(self):
        self.assertContains(self._page(), 'name="region"')

    def test_who_it_is_for_stays_above_the_questions(self):
        """The one thing on the page that is not an answer to anything."""
        self.assertContains(self._page(), self.worker_user.short_name
                            or str(self.worker_user))

    def test_sending_it_still_takes_one_request(self):
        """The whole point of rendering the steps rather than storing them."""
        response = self.client.post(
            reverse("jobs:offer", args=[self.worker_profile.pk]),
            {
                "new": "1",
                "trade": self.electrical.pk,
                "title": "Second fix",
                "description": "A day of first-floor sockets.",
                "experience_wanted": "none",
                "region": self.region.pk,
                "location": "Nea Smyrni",
                "gig_dates": "2027-03-04",
                "gig_hours": "8",
                "fixed_pay": "80",
                "use_escrow": "False",
                "note": "Yours if you want it.",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.worker_profile.offers.count(), 1
        )
