"""Phase 5 tests: arriving, finishing, and who gets what.

The prorating rules and the two windows are the whole point of this phase, so
most of these are arithmetic and clocks. Stripe is faked at the gateway, as in
phase 4 — the amounts asserted here are the amounts it would be told to take.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import ClientProfile, WorkerProfile
from config import business_rules as rules
from core.models import Region, Trade
from core.state_machine import IllegalTransition, JobState
from jobs.models import Job, JobType
from payments.models import EscrowPayment, EscrowStatus, StripeAccount

from . import services
from .models import CheckIn, Completion, Dispute, metres_between, payable_for

User = get_user_model()

CAPTURE = "payments.gateway.capture_payment_intent"


class WorkTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.filter(is_active=True).first()
        cls.trade = Trade.objects.get(slug="carpenter")
        cls.client_user = User.objects.create_user(email="client@example.com")
        cls.client_profile = ClientProfile.objects.create(
            user=cls.client_user, region=cls.region
        )
        cls.worker_user = User.objects.create_user(email="worker@example.com")
        cls.worker_profile = WorkerProfile.objects.create(
            user=cls.worker_user, region=cls.region
        )
        StripeAccount.objects.create(
            worker=cls.worker_profile,
            account_id="acct_1",
            details_submitted=True,
            charges_enabled=True,
            payouts_enabled=True,
        )

    def make_job(self, *, state=JobState.ESCROW_HELD, hours="8", pay="90", **extra):
        job = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.trade,
            region=self.region,
            title="Framing help",
            description="Second storey.",
            gig_date=timezone.localdate(),
            gig_hours=Decimal(hours),
            fixed_pay=Decimal(pay),
            state=state,
            assigned_worker=self.worker_profile,
            **extra,
        )
        EscrowPayment.objects.create(
            job=job,
            worker=self.worker_profile,
            amount=Decimal(pay),
            platform_fee=rules.platform_fee_for(Decimal(pay)),
            worker_payout=rules.worker_payout_for(Decimal(pay)),
            status=EscrowStatus.AUTHORIZED,
            payment_intent_id="pi_1",
        )
        return job

    def captured(self, mock, amount):
        mock.return_value = {
            "id": "pi_1",
            "status": "succeeded",
            "amount_received": Decimal(amount),
        }


class ProratingTests(WorkTestCase):
    def test_a_full_day_is_the_full_price(self):
        job = self.make_job()
        self.assertEqual(payable_for(job, Decimal("8")), Decimal("90.00"))

    def test_half_a_day_is_half_the_price(self):
        job = self.make_job()
        self.assertEqual(payable_for(job, Decimal("4")), Decimal("45.00"))

    def test_twenty_minutes_still_pays_the_guaranteed_minimum(self):
        """The floor that makes it safe to travel across town for a gig."""
        job = self.make_job()
        # 2 guaranteed hours of an 8-hour, $90 day.
        self.assertEqual(payable_for(job, Decimal("0.3")), Decimal("22.50"))
        self.assertEqual(payable_for(job, Decimal("0")), Decimal("22.50"))

    def test_the_floor_never_exceeds_the_booked_day(self):
        """A 1-hour gig cannot owe 2 hours — that is more than is held."""
        job = self.make_job(hours="1", pay="40")
        self.assertEqual(payable_for(job, Decimal("0.25")), Decimal("40.00"))

    def test_working_over_does_not_charge_the_client_more(self):
        job = self.make_job()
        self.assertEqual(payable_for(job, Decimal("12")), Decimal("90.00"))


class CheckInTests(WorkTestCase):
    def test_checking_in_starts_the_job(self):
        job = self.make_job()
        services.check_in(job, self.worker_profile)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.IN_PROGRESS)
        self.assertTrue(CheckIn.objects.filter(job=job).exists())

    def test_gps_near_the_site_reads_as_on_site(self):
        job = self.make_job(
            site_latitude=Decimal("40.712776"), site_longitude=Decimal("-74.005974")
        )
        record = services.check_in(
            job,
            self.worker_profile,
            latitude=Decimal("40.713500"),
            longitude=Decimal("-74.006500"),
            accuracy_m=20,
        )
        self.assertTrue(record.looks_on_site)
        self.assertLess(record.distance_m, rules.CHECKIN_GEOFENCE_RADIUS_M)

    def test_gps_far_away_is_flagged_but_never_blocks(self):
        job = self.make_job(
            site_latitude=Decimal("40.712776"), site_longitude=Decimal("-74.005974")
        )
        record = services.check_in(
            job,
            self.worker_profile,
            latitude=Decimal("40.760000"),
            longitude=Decimal("-73.980000"),
        )
        self.assertFalse(record.looks_on_site)
        job.refresh_from_db()
        # The whole point: flagged, and still checked in.
        self.assertEqual(job.state, JobState.IN_PROGRESS)

    def test_no_gps_at_all_is_normal_and_unjudged(self):
        job = self.make_job()
        record = services.check_in(job, self.worker_profile)
        self.assertIsNone(record.looks_on_site)
        self.assertIsNone(record.distance_m)

    def test_a_poor_accuracy_reading_widens_the_allowance(self):
        """A 2 km accuracy circle cannot tell you the worker is elsewhere."""
        job = self.make_job(
            site_latitude=Decimal("40.712776"), site_longitude=Decimal("-74.005974")
        )
        record = services.check_in(
            job,
            self.worker_profile,
            latitude=Decimal("40.720000"),
            longitude=Decimal("-74.005974"),
            accuracy_m=2000,
        )
        self.assertTrue(record.looks_on_site)

    def test_a_stranger_cannot_check_in_to_someone_elses_job(self):
        job = self.make_job()
        intruder = WorkerProfile.objects.create(
            user=User.objects.create_user(email="nope@example.com"), region=self.region
        )
        with self.assertRaises(services.WorkflowError):
            services.check_in(job, intruder)

    def test_cannot_check_in_before_the_money_is_held(self):
        job = self.make_job(state=JobState.ACCEPTED)
        with self.assertRaises(IllegalTransition):
            services.check_in(job, self.worker_profile)

    def test_distance_maths_is_sane(self):
        # Rough NYC-to-Philadelphia, ~130 km.
        metres = metres_between(40.7128, -74.0060, 39.9526, -75.1652)
        self.assertGreater(metres, 125_000)
        self.assertLess(metres, 135_000)


class CompletionTests(WorkTestCase):
    def setUp(self):
        self.job = self.make_job()
        services.check_in(self.job, self.worker_profile)
        self.job.refresh_from_db()

    def test_completing_starts_the_client_approval_window(self):
        completion = services.complete(self.job, self.worker_profile)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.COMPLETED)
        self.assertEqual(completion.payable_amount, Decimal("90.00"))
        self.assertFalse(completion.ended_early)
        expected = timezone.now() + rules.CLIENT_APPROVAL_WINDOW
        self.assertAlmostEqual(
            completion.settles_at, expected, delta=timedelta(seconds=30)
        )

    def test_an_early_finish_uses_the_short_window_and_prorates(self):
        completion = services.flag_early_end(
            self.job, self.worker_user, hours_worked=Decimal("4")
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.ENDED_EARLY)
        self.assertEqual(completion.payable_amount, Decimal("45.00"))
        expected = timezone.now() + rules.EARLY_END_DISPUTE_WINDOW
        self.assertAlmostEqual(
            completion.settles_at, expected, delta=timedelta(seconds=30)
        )

    def test_either_side_can_flag_an_early_finish(self):
        completion = services.flag_early_end(
            self.job, self.client_user, hours_worked=Decimal("3")
        )
        self.assertEqual(completion.ended_early_by, "client")

    def test_flagging_more_hours_than_booked_is_refused(self):
        with self.assertRaises(services.WorkflowError):
            services.flag_early_end(
                self.job, self.worker_user, hours_worked=Decimal("20")
            )

    def test_the_other_party_is_told_in_the_job_thread(self):
        """An early finish has to reach the other side immediately."""
        from messaging.models import Message

        services.flag_early_end(
            self.job, self.worker_user, hours_worked=Decimal("4"), note="Rained off"
        )
        message = Message.objects.filter(conversation__job=self.job).first()
        self.assertIsNotNone(message)
        self.assertIn("ended early", message.body)
        self.assertIn("Rained off", message.body)

    def test_a_job_cannot_be_closed_out_twice(self):
        services.flag_early_end(self.job, self.worker_user, hours_worked=Decimal("4"))
        with self.assertRaises(services.WorkflowError):
            services.flag_early_end(
                self.job, self.worker_user, hours_worked=Decimal("2")
            )


class SettlementTests(WorkTestCase):
    def setUp(self):
        self.job = self.make_job()
        services.check_in(self.job, self.worker_profile)
        self.job.refresh_from_db()

    @patch(CAPTURE)
    def test_client_approval_captures_the_whole_hold(self, mock):
        self.captured(mock, "90.00")
        services.complete(self.job, self.worker_profile)
        self.job.refresh_from_db()

        services.approve(self.job, self.client_user)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.PAID_OUT)
        # None means "capture it all" — no partial release on a full day.
        self.assertIsNone(mock.call_args.kwargs["amount"])

    @patch(CAPTURE)
    def test_an_early_finish_captures_only_the_prorated_part(self, mock):
        self.captured(mock, "45.00")
        services.flag_early_end(
            self.job, self.worker_user, hours_worked=Decimal("4")
        )
        self.job.refresh_from_db()

        services.approve(self.job, self.client_user)
        self.assertEqual(mock.call_args.kwargs["amount"], Decimal("45.00"))
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.PAID_OUT)

    @patch(CAPTURE)
    def test_silence_releases_the_money_when_the_window_lapses(self, mock):
        """A client who never logs in again cannot strand a worker's pay."""
        self.captured(mock, "90.00")
        completion = services.complete(self.job, self.worker_profile)

        # Nothing is due yet.
        self.assertEqual(services.settle_due(), [])
        mock.assert_not_called()

        Completion.objects.filter(pk=completion.pk).update(
            settles_at=timezone.now() - timedelta(minutes=1)
        )
        settled = services.settle_due()

        self.assertEqual(len(settled), 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.PAID_OUT)
        self.assertIsNotNone(Completion.objects.get(pk=completion.pk).settled_at)

    @patch(CAPTURE)
    def test_settling_twice_captures_once(self, mock):
        self.captured(mock, "90.00")
        completion = services.complete(self.job, self.worker_profile)
        Completion.objects.filter(pk=completion.pk).update(
            settles_at=timezone.now() - timedelta(minutes=1)
        )
        services.settle_due()
        services.settle_due()
        mock.assert_called_once()

    @patch(CAPTURE)
    def test_only_the_client_who_posted_it_can_approve(self, mock):
        services.complete(self.job, self.worker_profile)
        with self.assertRaises(services.WorkflowError):
            services.approve(self.job, self.worker_user)
        mock.assert_not_called()

    @patch(CAPTURE)
    def test_the_management_command_releases_what_is_due(self, mock):
        self.captured(mock, "90.00")
        completion = services.complete(self.job, self.worker_profile)
        Completion.objects.filter(pk=completion.pk).update(
            settles_at=timezone.now() - timedelta(minutes=1)
        )

        out = StringIO()
        call_command("settle_due_jobs", "--dry-run", stdout=out)
        mock.assert_not_called()  # dry run must not move money
        self.assertIn("1 due", out.getvalue())

        out = StringIO()
        call_command("settle_due_jobs", stdout=out)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.PAID_OUT)
        self.assertIn("Released 1", out.getvalue())
        # The skipped count is derived by subtraction; a lazy queryset made it
        # go negative once.
        self.assertNotIn("-1", out.getvalue())
        self.assertNotIn("not releasable", out.getvalue())


class DisputeTests(WorkTestCase):
    def setUp(self):
        self.job = self.make_job()
        services.check_in(self.job, self.worker_profile)
        self.job.refresh_from_db()
        services.flag_early_end(
            self.job, self.client_user, hours_worked=Decimal("2")
        )
        self.job.refresh_from_db()

    @patch(CAPTURE)
    def test_a_dispute_freezes_the_money(self, mock):
        services.raise_dispute(
            self.job, self.worker_user, reason="I worked six hours, not two."
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.DISPUTED)

        # And the timer no longer touches it.
        Completion.objects.filter(job=self.job).update(
            settles_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(services.settle_due(), [])
        mock.assert_not_called()

    def test_a_dispute_needs_an_actual_reason(self):
        with self.assertRaises(services.WorkflowError):
            services.raise_dispute(self.job, self.worker_user, reason="   ")

    def test_only_the_two_parties_can_dispute(self):
        outsider = User.objects.create_user(email="nosy@example.com")
        with self.assertRaises(services.WorkflowError):
            services.raise_dispute(self.job, outsider, reason="Just curious")

    def test_one_dispute_per_job(self):
        services.raise_dispute(self.job, self.worker_user, reason="Wrong hours")
        with self.assertRaises(services.WorkflowError):
            services.raise_dispute(self.job, self.client_user, reason="Also wrong")

    def test_the_dispute_records_who_raised_it_and_why(self):
        services.raise_dispute(
            self.job, self.worker_user, reason="I worked six hours, not two."
        )
        dispute = Dispute.objects.get(job=self.job)
        self.assertEqual(dispute.raised_by, self.worker_user)
        self.assertIn("six hours", dispute.reason)
        self.assertTrue(dispute.is_open)


class WorkspaceViewTests(WorkTestCase):
    def test_the_workspace_is_private_to_the_two_parties(self):
        job = self.make_job()
        outsider = User.objects.create_user(email="nosy@example.com")
        ClientProfile.objects.create(user=outsider, region=self.region)
        self.client.force_login(outsider)
        response = self.client.get(reverse("worklog:workspace", args=[job.pk]))
        self.assertRedirects(response, reverse("jobs:detail", args=[job.pk]))

    def test_the_worker_sees_the_check_in_action(self):
        job = self.make_job()
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("worklog:workspace", args=[job.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(JobState.IN_PROGRESS, response.context["moves"])

    def test_checking_in_through_the_view_records_the_position(self):
        job = self.make_job(
            site_latitude=Decimal("40.712776"), site_longitude=Decimal("-74.005974")
        )
        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("worklog:check_in", args=[job.pk]),
            {"latitude": "40.713500", "longitude": "-74.006500", "accuracy": "18"},
        )
        record = CheckIn.objects.get(job=job)
        self.assertEqual(record.accuracy_m, 18)
        self.assertTrue(record.looks_on_site)

    def test_a_junk_position_does_not_stop_the_check_in(self):
        """The form must survive a browser sending nonsense or nothing."""
        job = self.make_job()
        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("worklog:check_in", args=[job.pk]),
            {"latitude": "not-a-number", "longitude": "", "accuracy": "abc"},
        )
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.IN_PROGRESS)
        record = CheckIn.objects.get(job=job)
        self.assertIsNone(record.latitude)
        self.assertIsNone(record.accuracy_m)
