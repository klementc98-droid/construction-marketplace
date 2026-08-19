"""The browse filter bar: one line, and what it does with a filter already set.

The bar collapses its controls behind a button so the board starts at the top
of the screen instead of below six inputs. That trade has one failure mode
worth testing: a filter that is applied but folded out of sight, leaving
someone to wonder why the board is nearly empty.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from .forms import JobFilterForm, WorkerFilterForm


class PanelFieldsTests(TestCase):
    """The search box is rendered apart from the rest, by name, not by index."""

    def test_the_search_box_is_not_in_the_panel(self):
        for form_class in (JobFilterForm, WorkerFilterForm):
            with self.subTest(form=form_class.__name__):
                names = [f.name for f in form_class().panel_fields()]
                self.assertNotIn("q", names)

    def test_every_other_filter_is_in_the_panel(self):
        """Guards the drift that a hardcoded name list would eventually cause."""
        for form_class in (JobFilterForm, WorkerFilterForm):
            with self.subTest(form=form_class.__name__):
                form = form_class()
                expected = [n for n in form.fields if n != "q"]
                self.assertEqual([f.name for f in form.panel_fields()], expected)


class ActiveCountTests(TestCase):
    def test_nothing_set_counts_none(self):
        self.assertEqual(JobFilterForm(data={}).active_count(), 0)

    def test_the_search_box_alone_does_not_count(self):
        """It has its own box on the bar, so it is never hidden and never news."""
        self.assertEqual(JobFilterForm(data={"q": "framing"}).active_count(), 0)

    def test_a_set_filter_counts(self):
        self.assertEqual(JobFilterForm(data={"job_type": "gig"}).active_count(), 1)

    def test_an_unticked_checkbox_does_not_count(self):
        """Absent from GET entirely, which is how an unticked box arrives."""
        self.assertEqual(WorkerFilterForm(data={"q": "x"}).active_count(), 0)

    def test_a_ticked_checkbox_counts(self):
        form = WorkerFilterForm(data={"available_now": "on"})
        self.assertEqual(form.active_count(), 1)


class PanelRenderingTests(TestCase):
    """The panel is closed by default and open when it is doing something."""

    def test_the_board_opens_with_the_filters_folded_away(self):
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "filterbar-drop")
        self.assertNotContains(response, "filterbar-drop\" open")

    def test_an_applied_filter_opens_the_panel_and_is_counted(self):
        """The whole point of the badge: a filter you cannot see is one you forget."""
        response = self.client.get(reverse("jobs:list"), {"job_type": "gig"})
        self.assertContains(response, "filterbar-drop\" open")
        self.assertContains(response, "filterbar-count")

    def test_searching_alone_leaves_the_panel_shut(self):
        response = self.client.get(reverse("jobs:list"), {"q": "framing"})
        self.assertNotContains(response, "filterbar-drop\" open")

    def test_clear_is_offered_only_once_something_is_set(self):
        bare = self.client.get(reverse("jobs:list"))
        self.assertNotContains(bare, ">Clear<")
        filtered = self.client.get(reverse("jobs:list"), {"job_type": "gig"})
        self.assertContains(filtered, ">Clear<")

    def test_both_boards_use_the_same_bar(self):
        for name in ("jobs:list", "jobs:worker_list"):
            with self.subTest(board=name):
                response = self.client.get(reverse(name))
                self.assertContains(response, "filterbar-row")
