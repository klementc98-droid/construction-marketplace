"""The board, read by somebody who has never done the work.

This is the product's one question — *can I take this?* — so it is tested at
the two places it gets answered: the badge on every card, and the row of chips
that narrows the board to the jobs whose answer is yes.

The tests are written against what a reader sees, not against the queryset,
because the failure worth catching is not "the filter returned the wrong rows".
It is "the answer was correct and nobody could find it".
"""

from __future__ import annotations

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.models import Trade

from .forms import JobFilterForm
from .models import ExperienceWanted, Job
from .tests import JobFactoryMixin


class ExperienceBadgeTests(JobFactoryMixin, TestCase):
    """Every card says what it wants. There is no fourth, silent state."""

    def _board(self, wanted):
        self.gig(experience_wanted=wanted, title="Site work")
        return self.client.get(reverse("jobs:list"))

    # The assertions are on the badge class rather than on the sentence: the
    # words "No experience needed" also appear in the page description and in
    # the footer strapline, so matching on those alone would pass on a page
    # carrying no badge at all.

    def test_a_beginners_job_says_so_on_the_board(self):
        response = self._board(ExperienceWanted.NONE)
        self.assertContains(response, "xp-go")
        self.assertContains(response, "No experience needed")

    def test_a_job_wanting_some_experience_says_that_instead(self):
        response = self._board(ExperienceWanted.SOME)
        self.assertContains(response, "xp-steady")
        self.assertNotContains(response, "xp-go")

    def test_a_skilled_job_is_not_dressed_up_as_a_beginners_one(self):
        """The badge is only worth anything if it is able to say no."""
        response = self._board(ExperienceWanted.SKILLED)
        self.assertContains(response, "xp-stop")
        self.assertNotContains(response, "xp-go")

    def test_every_level_has_a_tone(self):
        """A level added later without one would render a bare, colourless pill."""
        for wanted in ExperienceWanted.values:
            with self.subTest(wanted=wanted):
                job = self.gig(experience_wanted=wanted)
                self.assertIn(job.experience_tone, {"go", "steady", "stop"})


class ExperienceChipTests(JobFactoryMixin, TestCase):
    """One tap, and the board is the jobs you can take."""

    def setUp(self):
        self.beginner = self.gig(
            title="Carrying and mixing", experience_wanted=ExperienceWanted.NONE
        )
        self.skilled = self.gig(
            title="Second fix wiring", experience_wanted=ExperienceWanted.SKILLED
        )

    def test_the_board_shows_everything_by_default(self):
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, "Carrying and mixing")
        self.assertContains(response, "Second fix wiring")

    def test_the_beginners_chip_hides_the_skilled_work(self):
        response = self.client.get(reverse("jobs:list"), {"experience": "none"})
        self.assertContains(response, "Carrying and mixing")
        self.assertNotContains(response, "Second fix wiring")

    def test_the_skilled_chip_is_the_mirror_of_it(self):
        response = self.client.get(reverse("jobs:list"), {"experience": "skilled"})
        self.assertContains(response, "Second fix wiring")
        self.assertNotContains(response, "Carrying and mixing")

    def test_the_filter_is_exact_and_not_a_ceiling(self):
        """"Some experience" means the jobs that say that.

        A filter that quietly widened to "this level and below" would put a job
        wanting three years into a list somebody chose because it said
        beginners, and there is no way to explain that on a chip.
        """
        self.gig(title="Helping the roofer", experience_wanted=ExperienceWanted.SOME)
        response = self.client.get(reverse("jobs:list"), {"experience": "some"})
        self.assertContains(response, "Helping the roofer")
        self.assertNotContains(response, "Carrying and mixing")

    def test_nonsense_in_the_url_shows_the_whole_board(self):
        """A hand-edited or stale URL must not empty the board silently."""
        response = self.client.get(reverse("jobs:list"), {"experience": "wizard"})
        self.assertContains(response, "Carrying and mixing")
        self.assertContains(response, "Second fix wiring")

    def test_the_applied_chip_is_marked_as_the_current_one(self):
        """Otherwise the board is filtered and looks like it is not."""
        response = self.client.get(reverse("jobs:list"), {"experience": "none"})
        self.assertContains(response, 'aria-current="true"')

    def test_tapping_a_chip_keeps_the_search_term(self):
        """The chips narrow what is on screen; they do not reset the board."""
        response = self.client.get(
            reverse("jobs:list"), {"q": "wiring", "experience": "skilled"}
        )
        self.assertContains(response, "q=wiring")

    def test_the_all_chip_clears_the_parameter_rather_than_emptying_it(self):
        """A URL ending "experience=" is a filter that reads as applied."""
        response = self.client.get(reverse("jobs:list"), {"experience": "none"})
        self.assertNotContains(response, 'href="?experience="')

    def test_all_four_chips_are_offered_in_order(self):
        chips = list(JobFilterForm(data={}).chips())
        self.assertEqual([c["tone"] for c in chips], ["all", "go", "steady", "stop"])

    def test_the_chip_labels_are_short_enough_for_a_phone(self):
        """The choice labels are sentences written for a form; chips have no room."""
        for chip in JobFilterForm(data={}).chips():
            with self.subTest(chip=chip["value"]):
                self.assertLessEqual(len(str(chip["label"])), 20)

    def test_the_chips_are_on_the_board_without_opening_the_panel(self):
        """The whole point of hoisting it out of the filter panel."""
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, 'class="chips"')


class JobPageTests(JobFactoryMixin, TestCase):
    """The page a card opens into.

    The board now answers "can I take this?" in a badge. The page it links to
    used to answer it nowhere at all — somebody could read a whole listing,
    write an application and learn from a sentence in the description that the
    job wanted a time-served electrician.
    """

    def test_the_page_says_what_the_job_wants(self):
        job = self.gig(experience_wanted=ExperienceWanted.SKILLED)
        response = self.client.get(job.get_absolute_url())
        self.assertContains(response, "xp-stop")

    def test_it_says_it_for_a_beginners_job_too(self):
        job = self.gig(experience_wanted=ExperienceWanted.NONE)
        response = self.client.get(job.get_absolute_url())
        self.assertContains(response, "xp-go")

    def test_the_trade_mark_is_the_same_one_the_card_used(self):
        """Opening a card should resolve into the page it was showing."""
        job = self.gig(trade=self.electrical)
        response = self.client.get(job.get_absolute_url())
        self.assertContains(response, "#i-bolt")


class ApplyBarTests(JobFactoryMixin, TestCase):
    """The bar exists because this page is long and the button was at the end."""

    def setUp(self):
        self.job = self.gig()

    def test_a_signed_out_reader_is_offered_the_way_in(self):
        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "applybar")

    def test_a_worker_who_can_apply_gets_the_bar(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "applybar")

    def test_the_bar_carries_the_rate_and_not_only_the_verb(self):
        """A button that says Apply and nothing else is half a decision."""
        self.client.force_login(self.worker_user)
        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "applybar-pay")

    def test_the_client_who_posted_it_is_not_offered_their_own_job(self):
        self.client.force_login(self.client_user)
        response = self.client.get(self.job.get_absolute_url())
        self.assertNotContains(response, "applybar")

    def test_the_bar_and_the_row_are_offered_together_or_not_at_all(self):
        """They are hidden on opposite breakpoints, so one appearing without
        the other is a screen size with no way to apply."""
        self.client.force_login(self.worker_user)
        body = self.client.get(self.job.get_absolute_url()).content.decode()
        self.assertEqual("apply-inline" in body, "applybar-in" in body)

    def test_the_bar_leaves_room_for_the_page_underneath_it(self):
        """Fixed to the bottom of the screen, it would otherwise sit on top of
        whatever the last card is."""
        self.client.force_login(self.worker_user)
        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "applybar-spacer")


class EmptyLevelTests(JobFactoryMixin, TestCase):
    """An empty board says something useful about why it is empty."""

    def setUp(self):
        self.gig(title="Second fix wiring", experience_wanted=ExperienceWanted.SKILLED)

    def test_an_empty_level_offers_the_beginners_board(self):
        """Somebody who tapped a chip narrowed by the one thing that decides
        whether they can take the work. "Try clearing the trade" is useless
        advice to them."""
        response = self.client.get(reverse("jobs:list"), {"experience": "some"})
        self.assertContains(response, "Nothing open at that level")
        self.assertContains(response, "experience=none")

    def test_it_does_not_offer_the_level_you_are_already_on(self):
        """A button that reloads the same empty board."""
        response = self.client.get(reverse("jobs:list"), {"experience": "none"})
        self.assertNotContains(response, "Show jobs needing no experience")

    def test_the_way_back_to_everything_is_offered(self):
        response = self.client.get(reverse("jobs:list"), {"experience": "some"})
        self.assertContains(response, "Show every level")

    def test_an_ordinary_empty_filter_still_says_clear_filters(self):
        """The other empty board, with its own advice. Two different problems."""
        response = self.client.get(reverse("jobs:list"), {"q": "nothing matches this"})
        self.assertContains(response, "Clear filters")


class TradeIconTests(JobFactoryMixin, TestCase):
    """Every trade gets a mark, including one nobody has mapped yet."""

    def test_a_known_trade_gets_its_own_icon(self):
        self.assertEqual(self.gig(trade=self.electrical).trade_icon, "i-bolt")

    def test_an_unmapped_trade_falls_back_instead_of_vanishing(self):
        """A trade row added later must not render a card with a hole in it."""
        glazier = Trade.objects.create(name="Glazier", slug="glazier")
        self.assertEqual(self.gig(trade=glazier).trade_icon, "i-trade")

    def test_every_mapped_icon_exists_in_the_sprite(self):
        """The mapping names ids in base.html; a typo there is a blank square."""
        sprite = (settings.BASE_DIR / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        for slug, icon in Job.TRADE_ICONS.items():
            with self.subTest(trade=slug):
                self.assertIn(f'id="{icon}"', sprite)

    def test_every_seeded_trade_is_mapped(self):
        """The fallback exists for a trade added at runtime, not as cover for
        forgetting one that ships with the app."""
        for slug in Trade.objects.values_list("slug", flat=True):
            with self.subTest(trade=slug):
                self.assertIn(slug, Job.TRADE_ICONS)
