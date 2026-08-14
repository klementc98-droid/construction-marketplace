"""Tests for the shared foundations: state machine, money rules, seed data."""

import importlib
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
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

    def test_starting_work_from_accepted_is_legal(self):
        """Because a deal settled directly has no funding step to wait for.

        This used to be refused here, when every gig went through escrow. The
        rule it enforced — nobody travels to a site on a hold that was never
        funded — has not gone anywhere; it moved to worklog.services.check_in,
        which is where it can ask the question the table cannot: does *this*
        job use escrow? See NoEscrowLifecycleTests and CheckInTests.
        """
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
