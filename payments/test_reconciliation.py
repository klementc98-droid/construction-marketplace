"""Reconciliation: the pass that admits Stripe is a separate system.

Every case here is a disagreement that no ordering inside a transaction can
prevent, because the two halves live in different databases. The tests all have
the same shape — Stripe is made to say one thing, the local row says another,
and the pass has to end with the local row matching Stripe.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone

from core.state_machine import JobState

from . import gateway, reconciliation
from .models import EscrowPayment, EscrowStatus, StripeAccount
from .tests import EscrowTestCase


class ReconcileCapturesTests(EscrowTestCase):
    """The crash window: Stripe took the money, the commit never happened."""

    def setUp(self):
        self.ready_account()
        self.job = self.make_gig(state=JobState.COMPLETED)
        self.escrow = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
            status=EscrowStatus.AUTHORIZED,
            payment_intent_id="pi_1",
            authorized_at=timezone.now(),
        )

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_capture_stripe_already_took_is_recorded(self, intent):
        intent.return_value = {
            "id": "pi_1",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }

        report = reconciliation.reconcile()

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.RELEASED)
        self.assertEqual(self.escrow.captured_amount, Decimal("90.00"))
        self.assertEqual(report.repaired, 1)

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_the_real_split_is_written_down_not_the_agreed_one(self, intent):
        """A partial capture settles at its own fee and its own payout."""
        intent.return_value = {
            "id": "pi_1",
            "status": "succeeded",
            "amount_received": Decimal("60.00"),
        }

        reconciliation.reconcile()

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.captured_amount, Decimal("60.00"))
        self.assertEqual(self.escrow.captured_fee, Decimal("7.20"))
        self.assertEqual(self.escrow.captured_payout, Decimal("52.80"))

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_the_job_catches_up_too(self, intent):
        intent.return_value = {
            "id": "pi_1",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }

        reconciliation.reconcile()

        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.PAID_OUT)

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_running_twice_repairs_once(self, intent):
        intent.return_value = {
            "id": "pi_1",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }

        reconciliation.reconcile()
        second = reconciliation.reconcile()

        self.assertEqual(second.repaired, 0)

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_hold_stripe_cancelled_is_recorded_as_returned(self, intent):
        intent.return_value = {
            "id": "pi_1",
            "status": "canceled",
            "amount_received": Decimal("0"),
        }

        reconciliation.reconcile()

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.REFUNDED)

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_stripe_not_answering_repairs_nothing(self, intent):
        """Repairing on a guess is how a reconciler becomes the problem."""
        intent.side_effect = RuntimeError("connection reset")

        report = reconciliation.reconcile()

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(report.repaired, 0)
        self.assertEqual(len(report.unreachable), 1)

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_dry_run_reports_and_writes_nothing(self, intent, cancel):
        intent.return_value = {
            "id": "pi_1",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }

        report = reconciliation.reconcile(dry_run=True)

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(len(report.captured_recorded), 1)


class DeadHoldTests(EscrowTestCase):
    """Money frozen for a job that is not going to happen.

    The divergence mark_authorized writes down and cannot resolve on its own:
    the hold is real, the job has died, and nobody is coming to release it.
    """

    def held_on(self, state):
        self.ready_account()
        job = self.make_gig(state=state)
        return EscrowPayment.objects.create(
            job=job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
            status=EscrowStatus.AUTHORIZED,
            payment_intent_id="pi_dead",
            authorized_at=timezone.now(),
        )

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_hold_on_an_expired_job_is_given_back(self, intent, cancel):
        intent.return_value = {"id": "pi_dead", "status": "requires_capture",
                               "amount_received": Decimal("0")}
        escrow = self.held_on(JobState.EXPIRED)

        report = reconciliation.reconcile()

        cancel.assert_called_once_with("pi_dead")
        escrow.refresh_from_db()
        self.assertEqual(escrow.status, EscrowStatus.REFUNDED)
        self.assertEqual(len(report.holds_released), 1)

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_hold_on_a_live_job_is_left_alone(self, intent, cancel):
        intent.return_value = {"id": "pi_dead", "status": "requires_capture",
                               "amount_received": Decimal("0")}
        escrow = self.held_on(JobState.COMPLETED)

        reconciliation.reconcile()

        cancel.assert_not_called()
        escrow.refresh_from_db()
        self.assertEqual(escrow.status, EscrowStatus.AUTHORIZED)

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_failed_cancellation_puts_the_row_back(self, intent, cancel):
        """A row saying refunded over a live hold is a worse lie than the one
        we started with."""
        intent.return_value = {"id": "pi_dead", "status": "requires_capture",
                               "amount_received": Decimal("0")}
        cancel.side_effect = RuntimeError("Stripe unavailable")
        escrow = self.held_on(JobState.CANCELLED)

        report = reconciliation.reconcile()

        escrow.refresh_from_db()
        self.assertEqual(escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(len(report.unreachable), 1)

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_the_flag_leaves_dead_holds_where_they_are(self, intent, cancel):
        intent.return_value = {"id": "pi_dead", "status": "requires_capture",
                               "amount_received": Decimal("0")}
        self.held_on(JobState.EXPIRED)

        reconciliation.reconcile(release_dead_holds=False)

        cancel.assert_not_called()


class JobsLeftBehindTests(EscrowTestCase):
    """The reconciler repaired one row of two and stopped looking at the other.

    It claims the escrow first and the job second. A lost race on the second
    used to end there: the escrow was no longer AUTHORIZED, so no later pass
    would ever look at it again — a payment and a job disagreeing permanently,
    produced inside the pass whose whole purpose is to end disagreements.
    """

    def escrow_in(self, status, *, job_state):
        self.ready_account()
        job = self.make_gig(state=job_state)
        return EscrowPayment.objects.create(
            job=job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
            status=status,
            payment_intent_id="pi_behind",
            authorized_at=timezone.now(),
        )

    def test_a_released_payment_drags_its_job_to_paid_out(self):
        escrow = self.escrow_in(
            EscrowStatus.RELEASED, job_state=JobState.COMPLETED
        )

        report = reconciliation.reconcile()

        escrow.job.refresh_from_db()
        self.assertEqual(escrow.job.state, JobState.PAID_OUT)
        self.assertEqual(len(report.states_repaired), 1)

    def test_a_refunded_payment_drags_its_job_too(self):
        escrow = self.escrow_in(
            EscrowStatus.REFUNDED, job_state=JobState.ESCROW_HELD
        )

        reconciliation.reconcile()

        escrow.job.refresh_from_db()
        self.assertEqual(escrow.job.state, JobState.REFUNDED)

    def test_a_move_the_lifecycle_forbids_is_reported_not_forced(self):
        """Repairing a disagreement by inventing a worse one is not a repair."""
        escrow = self.escrow_in(
            EscrowStatus.RELEASED, job_state=JobState.CANCELLED
        )

        with self.assertLogs("payments.reconciliation", level="WARNING"):
            reconciliation.reconcile()

        escrow.job.refresh_from_db()
        self.assertEqual(escrow.job.state, JobState.CANCELLED)

    def test_jobs_already_in_step_are_left_alone(self):
        self.escrow_in(EscrowStatus.RELEASED, job_state=JobState.PAID_OUT)

        report = reconciliation.reconcile()

        self.assertEqual(len(report.states_repaired), 0)

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_cancellation_moves_the_job_as_well_as_the_payment(self, intent):
        """The half the cancellation branch was leaving behind."""
        intent.return_value = {
            "id": "pi_behind",
            "status": "canceled",
            "amount_received": Decimal("0"),
        }
        escrow = self.escrow_in(
            EscrowStatus.AUTHORIZED, job_state=JobState.ESCROW_HELD
        )

        reconciliation.reconcile()

        escrow.refresh_from_db()
        escrow.job.refresh_from_db()
        self.assertEqual(escrow.status, EscrowStatus.REFUNDED)
        self.assertEqual(escrow.job.state, JobState.REFUNDED)


class LostAccountTests(EscrowTestCase):
    """The 24-hour hole, closed by a record rather than by a key."""

    @patch("payments.gateway.find_account_for", autospec=True)
    def test_an_account_stripe_holds_is_adopted(self, find):
        StripeAccount.objects.create(worker=self.worker_profile)
        find.return_value = "acct_orphan"

        report = reconciliation.reconcile()

        account = StripeAccount.objects.get(worker=self.worker_profile)
        self.assertEqual(account.account_id, "acct_orphan")
        self.assertEqual(len(report.accounts_adopted), 1)

    @patch("payments.gateway.find_account_for", autospec=True)
    def test_nothing_at_stripe_leaves_the_row_waiting(self, find):
        StripeAccount.objects.create(worker=self.worker_profile)
        find.return_value = None

        reconciliation.reconcile()

        account = StripeAccount.objects.get(worker=self.worker_profile)
        self.assertEqual(account.account_id, "")
