"""Tests for the shared foundations: state machine, money rules, seed data."""

import importlib
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from dotenv import dotenv_values

from config import business_rules as rules
from core.models import Region, Trade
from core.state_machine import (
    Actor,
    IllegalTransition,
    JobState,
    TERMINAL_STATES,
    assert_transition,
    available_transitions,
    can_transition,
    is_terminal,
)


class GoogleSettingsTests(SimpleTestCase):
    def test_google_client_id_is_loaded_from_dotenv_when_env_is_blank(self):
        dotenv_path = Path(__file__).resolve().parent.parent / ".env"
        expected_client_id = dotenv_values(dotenv_path).get("GOOGLE_CLIENT_ID", "")

        if not expected_client_id:
            self.skipTest("GOOGLE_CLIENT_ID is not configured in .env")

        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": ""}, clear=False):
            import config.settings as settings_module

            importlib.reload(settings_module)

            self.assertEqual(
                settings_module.SOCIALACCOUNT_PROVIDERS["google"]["APP"]["client_id"],
                expected_client_id,
            )


class StateMachineTests(TestCase):
    def test_happy_path_is_walkable_end_to_end(self):
        path = [
            (JobState.POSTED, JobState.ACCEPTED, Actor.CLIENT),
            (JobState.ACCEPTED, JobState.ESCROW_HELD, Actor.CLIENT),
            (JobState.ESCROW_HELD, JobState.IN_PROGRESS, Actor.WORKER),
            (JobState.IN_PROGRESS, JobState.COMPLETED, Actor.WORKER),
            (JobState.COMPLETED, JobState.PAID_OUT, Actor.CLIENT),
        ]
        for src, dst, actor in path:
            assert_transition(src, dst, actor)  # must not raise

    def test_work_cannot_start_before_escrow_is_funded(self):
        """The rule the whole design exists to enforce."""
        with self.assertRaises(IllegalTransition):
            assert_transition(JobState.ACCEPTED, JobState.IN_PROGRESS, Actor.WORKER)

    def test_job_cannot_skip_straight_to_payout(self):
        for src in (JobState.POSTED, JobState.ACCEPTED, JobState.ESCROW_HELD):
            with self.assertRaises(IllegalTransition):
                assert_transition(src, JobState.PAID_OUT, Actor.SYSTEM)

    def test_client_cannot_mark_the_job_complete(self):
        """Only the worker reports that the work is done."""
        self.assertFalse(
            can_transition(JobState.IN_PROGRESS, JobState.COMPLETED, Actor.CLIENT)
        )
        self.assertTrue(
            can_transition(JobState.IN_PROGRESS, JobState.COMPLETED, Actor.WORKER)
        )

    def test_worker_cannot_release_their_own_payment(self):
        self.assertFalse(
            can_transition(JobState.COMPLETED, JobState.PAID_OUT, Actor.WORKER)
        )

    def test_silence_releases_funds_to_the_worker(self):
        """An absent client must not be able to strand a worker's pay."""
        self.assertTrue(
            can_transition(JobState.COMPLETED, JobState.PAID_OUT, Actor.SYSTEM)
        )

    def test_disputes_never_auto_resolve(self):
        """There is deliberately no timer out of DISPUTED — only an admin."""
        moves = available_transitions(JobState.DISPUTED, Actor.SYSTEM)
        self.assertEqual(moves, ())
        self.assertTrue(available_transitions(JobState.DISPUTED, Actor.ADMIN))

    def test_either_party_can_flag_an_early_finish(self):
        for actor in (Actor.WORKER, Actor.CLIENT):
            self.assertTrue(
                can_transition(JobState.IN_PROGRESS, JobState.ENDED_EARLY, actor)
            )

    def test_cancelling_after_funding_refunds_rather_than_cancelling(self):
        """CANCELLED after escrow would orphan the client's money."""
        self.assertFalse(
            can_transition(JobState.ESCROW_HELD, JobState.CANCELLED, Actor.CLIENT)
        )
        self.assertTrue(
            can_transition(JobState.ESCROW_HELD, JobState.REFUNDED, Actor.CLIENT)
        )

    def test_terminal_states_have_no_exits(self):
        for state in (JobState.PAID_OUT, JobState.REFUNDED, JobState.CANCELLED, JobState.EXPIRED):
            self.assertTrue(is_terminal(state))
            with self.assertRaises(IllegalTransition):
                assert_transition(state, JobState.DISPUTED, Actor.ADMIN)

    def test_self_transition_is_rejected(self):
        with self.assertRaises(IllegalTransition):
            assert_transition(JobState.COMPLETED, JobState.COMPLETED, Actor.CLIENT)

    def test_every_state_is_reachable_from_posted(self):
        """No orphan states: anything defined can actually be arrived at.

        Guards against adding a state to the enum and forgetting to wire an
        edge into it, which would leave dead code that looks live.
        """
        seen = {JobState.POSTED}
        frontier = [JobState.POSTED]
        while frontier:
            for move in available_transitions(frontier.pop()):
                if move.to_state not in seen:
                    seen.add(move.to_state)
                    frontier.append(move.to_state)

        self.assertEqual(seen, set(JobState.values))

    def test_error_message_names_actor_state_and_target(self):
        with self.assertRaises(IllegalTransition) as ctx:
            assert_transition(JobState.POSTED, JobState.PAID_OUT, Actor.WORKER)
        message = str(ctx.exception)
        for fragment in ("worker", "posted", "paid_out"):
            self.assertIn(fragment, message)


class MoneyRuleTests(TestCase):
    def test_fee_and_payout_always_sum_to_the_total(self):
        """No cent may be created or destroyed by the split."""
        for cents in range(1, 2000):
            amount = Decimal(cents) / Decimal(100)
            fee = rules.platform_fee_for(amount)
            payout = rules.worker_payout_for(amount)
            self.assertEqual(fee + payout, amount, f"split lost a cent at {amount}")

    def test_default_fee_is_twelve_percent(self):
        self.assertEqual(rules.platform_fee_for(Decimal("100.00")), Decimal("12.00"))
        self.assertEqual(rules.worker_payout_for(Decimal("100.00")), Decimal("88.00"))

    def test_fee_is_quantised_to_cents(self):
        fee = rules.platform_fee_for(Decimal("90.00"))
        self.assertEqual(fee.as_tuple().exponent, -2)

    def test_windows_are_ordered_sensibly(self):
        """The early-end window must be shorter than the approval window."""
        self.assertLess(rules.EARLY_END_DISPUTE_WINDOW, rules.CLIENT_APPROVAL_WINDOW)


class SeedDataTests(TestCase):
    def test_all_twelve_trades_are_seeded(self):
        self.assertEqual(Trade.objects.count(), 12)

    def test_only_the_regulated_trades_require_a_licence(self):
        regulated = set(
            Trade.objects.filter(requires_license=True).values_list("slug", flat=True)
        )
        self.assertEqual(regulated, {"electrician", "plumber", "hvac"})

    def test_launch_region_exists_and_is_active(self):
        region = Region.objects.get(slug=rules.DEFAULT_REGION_SLUG)
        self.assertTrue(region.is_active)


class AboutPageTests(TestCase):
    """The public explanation of how the money works.

    Readable signed out — someone deciding whether to trust us with a day's pay
    should not have to hand over an account to find out the terms.
    """

    def test_readable_without_an_account(self):
        response = self.client.get(reverse("core:about"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "What we do")

    def test_quotes_the_real_platform_fee(self):
        """The page must not be able to disagree with business_rules.

        A hardcoded "12%" in the template would survive a fee change and become
        a false statement about someone's pay. Reading it from the view is what
        makes that impossible, and this is the test that keeps it that way.
        """
        response = self.client.get(reverse("core:about"))
        expected = f"{rules.PLATFORM_FEE_PCT * 100:.2f}".rstrip("0").rstrip(".")
        self.assertContains(response, f"{expected}%")

    def test_states_the_approval_window_in_hours(self):
        response = self.client.get(reverse("core:about"))
        hours = int(rules.CLIENT_APPROVAL_WINDOW.total_seconds() // 3600)
        self.assertContains(response, f"{hours} hours")

    def test_says_we_do_not_verify_licences(self):
        """A promise we deliberately do not make, stated where people look."""
        response = self.client.get(reverse("core:about"))
        self.assertContains(response, "do not verify licence numbers")

    def test_links_to_the_whitepaper(self):
        response = self.client.get(reverse("core:about"))
        self.assertContains(response, reverse("core:whitepaper"))


class WhitepaperTests(TestCase):
    """Served from docs/whitepaper.md, so the file and the page cannot differ."""

    def test_readable_without_an_account(self):
        response = self.client.get(reverse("core:whitepaper"))
        self.assertEqual(response.status_code, 200)

    def test_renders_the_repository_file(self):
        from core.views import WHITEPAPER_PATH

        self.assertTrue(WHITEPAPER_PATH.exists())
        response = self.client.get(reverse("core:whitepaper"))
        self.assertContains(response, "Deliberate limits of v1")

    def test_markdown_is_converted_not_dumped(self):
        """A page showing raw '## 1. Summary' means the renderer silently failed."""
        body = self.client.get(reverse("core:whitepaper")).content.decode()
        self.assertIn("<h2", body)
        self.assertNotIn("## 1. Summary", body)

    def test_the_lifecycle_diagram_survives_as_a_code_block(self):
        """It is ASCII art; without fenced_code it collapses into a paragraph."""
        body = self.client.get(reverse("core:whitepaper")).content.decode()
        self.assertIn("<pre>", body)
        self.assertIn("escrow_held", body)

    def test_the_comparison_table_survives(self):
        body = self.client.get(reverse("core:whitepaper")).content.decode()
        self.assertIn("<table>", body)

    def test_reachable_from_the_footer_on_any_page(self):
        response = self.client.get(reverse("jobs:list"))
        self.assertContains(response, reverse("core:whitepaper"))

    def test_reachable_from_the_top_bar(self):
        """Two routes, because the footer alone proved easy to miss."""
        body = self.client.get(reverse("jobs:list")).content.decode()
        nav = body.split('class="desktop-nav"')[1].split("</nav>")[0]
        self.assertIn(reverse("core:whitepaper"), nav)

    def test_the_top_bar_link_marks_itself_current_on_the_page(self):
        body = self.client.get(reverse("core:whitepaper")).content.decode()
        nav = body.split('class="desktop-nav"')[1].split("</nav>")[0]
        self.assertIn('aria-current="page"', nav)

    def test_a_missing_file_is_a_404_not_a_500(self):
        from core import views

        with patch.object(views, "WHITEPAPER_PATH", Path("/nope/missing.md")):
            views._rendered = None
            response = self.client.get(reverse("core:whitepaper"))
        views._rendered = None
        self.assertEqual(response.status_code, 404)
