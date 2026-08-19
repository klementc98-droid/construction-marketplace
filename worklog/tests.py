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
        # gig_date is overridable, because a booking needs a day per row —
        # see the double-booking index. It was hard-coded below, so any caller
        # asking for one got "multiple values for gig_date".
        extra.setdefault("gig_date", timezone.localdate())
        # Escrow on by default in this fixture: these tests are about the
        # escrow path. The model default is off — most deals are settled
        # directly — so the ones exercising that path say so explicitly.
        extra.setdefault("use_escrow", True)
        job = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.trade,
            region=self.region,
            title="Framing help",
            description="Second storey.",
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
        """Still refused — by the service now, not the transition table.

        The table had to start allowing ACCEPTED -> IN_PROGRESS, or a job
        settled directly could never start. Only the service knows whether
        this particular job has escrow on it.
        """
        job = self.make_job(state=JobState.ACCEPTED)          # use_escrow=True
        with self.assertRaises(services.WorkflowError):
            services.check_in(job, self.worker_profile)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.ACCEPTED)

    def test_a_job_without_escrow_can_start_straight_away(self):
        """Nothing to wait for when nobody is holding the money."""
        job = self.make_job(state=JobState.ACCEPTED, use_escrow=False)
        services.check_in(job, self.worker_profile)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.IN_PROGRESS)

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
        # The board, not the job. A booked job is only readable by the two
        # people it is between, so sending an outsider there would answer one
        # refusal with a second.
        self.assertRedirects(response, reverse("jobs:list"))

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


class NoEscrowJobTests(WorkTestCase):
    """A gig the two sides settle themselves.

    The short route: accepted → worker says done → client agrees → closed.
    No funding step, no check-in, no timer, because none of those mean
    anything when the platform is not holding the money.
    """

    def setUp(self):
        self.worker = self.worker_profile
        self.job = self.make_job(state=JobState.ACCEPTED)
        self.job.use_escrow = False
        self.job.assigned_worker = self.worker
        self.job.save(update_fields=["use_escrow", "assigned_worker"])

    def test_the_job_reports_itself_as_unescrowed(self):
        self.assertFalse(self.job.is_escrowed)

    def test_a_standing_position_is_never_escrowed_whatever_the_flag_says(self):
        """There is no single day to sign off, so the flag is not the question."""
        from jobs.models import JobType

        self.job.job_type = JobType.STANDING
        self.job.use_escrow = True
        self.assertFalse(self.job.is_escrowed)

    def test_the_worker_marks_the_work_finished_without_checking_in(self):
        services.mark_work_finished(self.job, self.worker)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.COMPLETED)
        self.assertTrue(self.job.awaiting_client_confirmation)

    def test_no_completion_record_is_written(self):
        """That row is a payout calculation, and there is no payout."""
        services.mark_work_finished(self.job, self.worker)
        self.job.refresh_from_db()
        self.assertFalse(hasattr(self.job, "completion"))

    def test_confirming_twice_at_once_counts_the_job_once(self):
        """jobs_completed is reputation, and an F() + 1 that ran twice for one
        job leaves both track records overstated with nothing to show for it.

        The window is forced open rather than raced for: assert_transition runs
        after confirm_closed has read the job and before it writes, which is
        exactly where a second request would land.
        """
        from unittest import mock

        from jobs.models import Job

        services.mark_work_finished(self.job, self.worker)
        before_worker = type(self.worker).objects.get(pk=self.worker.pk).jobs_completed
        before_client = type(self.client_profile).objects.get(
            pk=self.client_profile.pk
        ).jobs_completed

        real = services.assert_transition

        def close_it_first(*args, **kwargs):
            result = real(*args, **kwargs)
            Job.objects.filter(pk=self.job.pk, state=JobState.COMPLETED).update(
                state=JobState.CLOSED
            )
            return result

        with mock.patch.object(services, "assert_transition", close_it_first):
            with self.assertRaises(services.WorkflowError):
                services.confirm_closed(self.job, self.client_user)

        self.assertEqual(
            type(self.worker).objects.get(pk=self.worker.pk).jobs_completed,
            before_worker,
        )
        self.assertEqual(
            type(self.client_profile).objects.get(
                pk=self.client_profile.pk
            ).jobs_completed,
            before_client,
        )

    def test_the_refusal_says_where_the_job_actually_ended_up(self):
        """Who moved it is not knowable from here; where it went is, and it is
        the half that tells somebody what to do next."""
        from unittest import mock

        from jobs.models import Job

        services.mark_work_finished(self.job, self.worker)
        real = services.assert_transition

        def close_it_first(*args, **kwargs):
            result = real(*args, **kwargs)
            Job.objects.filter(pk=self.job.pk).update(state=JobState.CLOSED)
            return result

        with mock.patch.object(services, "assert_transition", close_it_first):
            with self.assertRaises(services.WorkflowError) as caught:
                services.confirm_closed(self.job, self.client_user)

        self.assertIn(str(JobState.CLOSED.label), str(caught.exception))

    def test_the_client_confirming_closes_it(self):
        services.mark_work_finished(self.job, self.worker)
        services.confirm_closed(self.job, self.client_user)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.CLOSED)
        self.assertTrue(self.job.is_finished)

    def test_closing_counts_on_both_track_records(self):
        """Settled directly still happened. Not counting it would make the
        pair who trust each other look like the pair who never worked."""
        before_worker = self.worker.jobs_completed
        before_client = self.job.client.jobs_completed

        services.mark_work_finished(self.job, self.worker)
        services.confirm_closed(self.job, self.client_user)

        self.worker.refresh_from_db()
        self.job.client.refresh_from_db()
        self.assertEqual(self.worker.jobs_completed, before_worker + 1)
        self.assertEqual(self.job.client.jobs_completed, before_client + 1)

    def test_the_worker_cannot_close_it_alone(self):
        """Both sides or nothing — there is no hold to fall back on."""
        services.mark_work_finished(self.job, self.worker)
        with self.assertRaises(services.WorkflowError):
            services.confirm_closed(self.job, self.worker.user)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.COMPLETED)

    def test_nothing_closes_on_a_timer(self):
        """No money held means no window, so the sweep must leave it alone."""
        services.mark_work_finished(self.job, self.worker)
        services.settle_due()
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.COMPLETED)

    def test_an_escrowed_job_refuses_the_short_route(self):
        # Tomorrow, not today: this worker already has today's job from setUp,
        # and one worker has one of each day — see the double-booking index.
        escrowed = self.make_job(
            state=JobState.ACCEPTED,
            gig_date=timezone.localdate() + timedelta(days=1),
        )
        escrowed.assigned_worker = self.worker
        escrowed.save(update_fields=["assigned_worker"])
        with self.assertRaises(services.WorkflowError):
            services.mark_work_finished(escrowed, self.worker)

    def test_an_unescrowed_job_refuses_the_escrow_release(self):
        services.mark_work_finished(self.job, self.worker)
        with self.assertRaises(services.WorkflowError):
            services.approve(self.job, self.client_user)


class BookingLifecycleTests(WorkTestCase):
    """A booking finishes, closes and counts as one job.

    The days are separate rows so each can carry its own escrow and its own
    sign-off. Nothing about that is meant to reach the two people involved:
    they agreed one week, they finish one week, and it goes on both records
    once.
    """

    def booking(self, count=4, **overrides):
        from uuid import uuid4

        from datetime import timedelta

        group = uuid4()
        days = []
        for n in range(count):
            # A day of its own each, which is what a booking is. They all
            # shared today's date until the double-booking index refused it —
            # correctly: four live gigs on one date for one worker is the very
            # thing that index exists to make impossible.
            overrides.setdefault("gig_date", timezone.localdate())
            job = self.make_job(
                state=JobState.ACCEPTED,
                offer_group=group,
                **{**overrides, "gig_date": overrides["gig_date"] + timedelta(days=n)},
            )
            job.use_escrow = False
            job.assigned_worker = self.worker_profile
            job.save(update_fields=["use_escrow", "assigned_worker"])
            days.append(job)
        return days

    def test_marking_it_finished_reaches_every_day_that_has_happened(self):
        """A booking is signed off in one press — but only as far as today.

        It used to reach the whole week on its first morning, which recorded
        six days of work as done before they existed and handed the worker's
        diary back to her while she was still committed to it.
        """
        days = self.booking(4, gig_date=timezone.localdate() - timedelta(days=1))
        # Yesterday and today have happened; the two after have not.
        services.mark_work_finished(days[0], self.worker_profile)

        for day in days[:2]:
            day.refresh_from_db()
            self.assertEqual(day.state, JobState.COMPLETED)
        for day in days[2:]:
            day.refresh_from_db()
            self.assertEqual(day.state, JobState.ACCEPTED)

    def test_a_day_still_ahead_cannot_be_signed_off_at_all(self):
        """And the refusal says so, rather than reading as a lost race."""
        days = self.booking(3, gig_date=timezone.localdate() + timedelta(days=2))

        with self.assertRaises(services.WorkflowError) as caught:
            services.mark_work_finished(days[0], self.worker_profile)
        self.assertIn("hasn't come yet", str(caught.exception))

        for day in days:
            day.refresh_from_db()
            self.assertEqual(day.state, JobState.ACCEPTED)

    def test_the_worker_stays_booked_for_the_days_still_to_come(self):
        """The point of the rule, from the diary's side."""
        days = self.booking(4, gig_date=timezone.localdate() - timedelta(days=1))
        services.mark_work_finished(days[0], self.worker_profile)
        services.confirm_closed(days[0], self.client_user)

        self.worker_profile.refresh_from_db()
        self.assertEqual(
            self.worker_profile.booked_dates,
            [days[2].gig_date, days[3].gig_date],
        )

    def test_confirming_closes_every_day_that_has_happened(self):
        days = self.booking(4, gig_date=timezone.localdate() - timedelta(days=1))
        services.mark_work_finished(days[0], self.worker_profile)
        services.confirm_closed(days[0], self.client_user)

        for day in days[:2]:
            day.refresh_from_db()
            self.assertEqual(day.state, JobState.CLOSED)
        for day in days[2:]:
            day.refresh_from_db()
            self.assertEqual(day.state, JobState.ACCEPTED)

    def test_a_week_counts_as_one_job_on_both_records(self):
        """The number a profile shows is "how much has this person seen
        through". Five days of one booking is one."""
        days = self.booking(5, gig_date=timezone.localdate() - timedelta(days=4))
        worker_before = type(self.worker_profile).objects.get(
            pk=self.worker_profile.pk
        ).jobs_completed
        client_before = type(self.client_profile).objects.get(
            pk=self.client_profile.pk
        ).jobs_completed

        services.mark_work_finished(days[0], self.worker_profile)
        services.confirm_closed(days[0], self.client_user)

        self.assertEqual(
            type(self.worker_profile).objects.get(
                pk=self.worker_profile.pk
            ).jobs_completed,
            worker_before + 1,
        )
        self.assertEqual(
            type(self.client_profile).objects.get(
                pk=self.client_profile.pk
            ).jobs_completed,
            client_before + 1,
        )

    def test_a_single_day_job_is_untouched_by_any_of_this(self):
        job = self.make_job(state=JobState.ACCEPTED)
        job.use_escrow = False
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["use_escrow", "assigned_worker"])

        services.mark_work_finished(job, self.worker_profile)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.COMPLETED)

        services.confirm_closed(job, self.client_user)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.CLOSED)

    def test_a_day_that_cannot_move_does_not_block_the_rest(self):
        """One stuck day must not stop a client closing the other four."""
        # All four behind us, so the only thing being tested is the stuck day
        # rather than the calendar.
        days = self.booking(4, gig_date=timezone.localdate() - timedelta(days=4))
        services.mark_work_finished(days[0], self.worker_profile)
        # Something happened to Wednesday on its own.
        days[2].state = JobState.DISPUTED
        days[2].save(update_fields=["state"])

        services.confirm_closed(days[0], self.client_user)

        for day in (days[0], days[1], days[3]):
            day.refresh_from_db()
            self.assertEqual(day.state, JobState.CLOSED)
        days[2].refresh_from_db()
        self.assertEqual(days[2].state, JobState.DISPUTED)
