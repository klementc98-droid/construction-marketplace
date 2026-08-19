"""The two ways a deal can run, end to end.

The point of these is that neither path can quietly acquire a dependency on
the other. Path A is the ordinary marketplace: two people agree work and
settle it themselves, and nothing in it may touch Stripe, an escrow row, or a
payment state. Path B is the same marketplace with escrow switched on for that
one deal, and it must still enforce everything escrow exists to enforce.

Written as full journeys rather than unit tests on purpose. The failure these
guard against is not a broken function — it is a step somewhere in the middle
that starts requiring a payment, which only shows up when you walk the whole
thing.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.state_machine import JobState
from messaging.models import Conversation, Message
from worklog import services as worklog_services

from .models import Application, Counter, Job, Party, Review
from .tests import JobFactoryMixin


class DealWithoutEscrowTests(JobFactoryMixin, TestCase):
    """Path A: post, apply, negotiate, agree, work, complete, review.

    No Stripe, no escrow row, no payment state — at any step.
    """

    def setUp(self):
        self.job = self.gig(fixed_pay=Decimal("500"))
        self.job.gig_date = timezone.localdate() + timedelta(days=2)
        self.job.save(update_fields=["gig_date"])

    def assertNoPaymentExists(self, job):
        from payments.models import EscrowPayment

        self.assertFalse(EscrowPayment.objects.filter(job=job).exists())
        self.assertFalse(job.is_escrowed)

    def test_a_gig_is_posted_without_escrow_by_default(self):
        """Escrow is opt-in. Nothing asks for it in order to post work."""
        self.assertFalse(self.job.use_escrow)
        self.assertFalse(self.job.is_escrowed)

    def test_the_whole_deal_runs_with_no_payment_anywhere(self):
        job = self.job

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:apply", args=[job.pk]), {"message": "Free that day."}
        )
        self.assertTrue(Application.objects.filter(job=job).exists())

        # 500 asked, 450 countered, 475 agreed.
        self.client.post(
            reverse("jobs:counter", args=[job.pk]),
            {
                "fixed_pay": "450",
                "gig_hours": job.gig_hours,
                "gig_date": job.gig_date.isoformat(),
                "note": "Long day.",
            },
        )
        self.assertEqual(Counter.objects.get(job=job).proposed_by, Party.WORKER)

        self.client.force_login(self.client_user)
        self.client.post(
            reverse("jobs:counter_to", args=[job.pk, self.worker_profile.pk]),
            {
                "fixed_pay": "475",
                "gig_hours": job.gig_hours,
                "gig_date": job.gig_date.isoformat(),
                "note": "Meet you there.",
            },
        )
        agreed = Counter.objects.filter(proposed_by=Party.CLIENT).get()

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:counter_respond", args=[agreed.pk]), {"answer": "accept"}
        )

        job.refresh_from_db()
        self.assertEqual(job.state, JobState.ACCEPTED)
        self.assertEqual(job.fixed_pay, Decimal("475"))
        self.assertEqual(job.assigned_worker, self.worker_profile)

        # A channel to talk on, with no payment involved in getting one.
        # Applying is what grants it; opening the thread is a request away.
        self.client.post(
            reverse("messaging:start", args=[job.pk, self.worker_profile.pk])
        )
        conversation = Conversation.objects.get(job=job, worker=self.worker_profile)
        self.client.post(
            reverse("messaging:thread", args=[conversation.pk]), {"body": "On my way."}
        )
        self.assertTrue(Message.objects.filter(conversation=conversation).exists())

        worklog_services.check_in(job, self.worker_profile)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.IN_PROGRESS)

        self.day_arrives(job)
        worklog_services.mark_work_finished(job, self.worker_profile)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.COMPLETED)

        worklog_services.confirm_closed(job, self.client_user)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.CLOSED)

        for user in (self.client_user, self.worker_user):
            self.assertTrue(job.can_be_reviewed_by(user))
            self.client.force_login(user)
            self.client.post(reverse("jobs:review", args=[job.pk]), {"rating": "5"})
        self.assertEqual(Review.objects.filter(job=job).count(), 2)

        self.assertNoPaymentExists(job)

    def test_completion_never_reaches_the_payment_gateway(self):
        """Stripe is not merely unused here — it is never called."""
        job = self.job
        job.state = JobState.ACCEPTED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])

        with patch("payments.gateway.stripe") as stripe:
            self.day_arrives(job)
            worklog_services.mark_work_finished(job, self.worker_profile)
            worklog_services.confirm_closed(job, self.client_user)
        self.assertEqual(stripe.method_calls, [])

    def test_a_worker_with_no_connected_account_can_do_all_of_it(self):
        from payments.models import StripeAccount

        self.assertFalse(
            StripeAccount.objects.filter(worker=self.worker_profile).exists()
        )
        job = self.job
        job.state = JobState.ACCEPTED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])

        self.day_arrives(job)
        worklog_services.mark_work_finished(job, self.worker_profile)
        worklog_services.confirm_closed(job, self.client_user)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.CLOSED)

    def test_reviewing_depends_on_the_work_being_done_not_on_money(self):
        job = self.job
        job.state = JobState.CLOSED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])
        self.assertTrue(job.can_be_reviewed_by(self.client_user))
        self.assertNoPaymentExists(job)


class DealWithEscrowTests(JobFactoryMixin, TestCase):
    """Path B: the same marketplace, with escrow chosen for this one deal."""

    def setUp(self):
        self.job = self.gig(fixed_pay=Decimal("500"))
        self.job.use_escrow = True
        self.job.state = JobState.ACCEPTED
        self.job.assigned_worker = self.worker_profile
        self.job.save(update_fields=["use_escrow", "state", "assigned_worker"])

    def test_the_job_reports_that_it_uses_escrow(self):
        self.assertTrue(self.job.is_escrowed)

    def test_work_cannot_start_before_the_money_is_held(self):
        """The guarantee escrow exists for, still enforced."""
        with self.assertRaises(worklog_services.WorkflowError):
            worklog_services.check_in(self.job, self.worker_profile)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.ACCEPTED)

    def test_it_cannot_take_the_direct_settlement_shortcut(self):
        """Closing by agreement would skip the release of real money."""
        with self.assertRaises(worklog_services.WorkflowError):
            self.day_arrives(self.job)
            worklog_services.mark_work_finished(self.job, self.worker_profile)

    def test_the_direct_close_is_refused_even_from_completed(self):
        self.job.state = JobState.COMPLETED
        self.job.save(update_fields=["state"])
        with self.assertRaises(worklog_services.WorkflowError):
            worklog_services.confirm_closed(self.job, self.client_user)

    def test_once_funded_the_escrow_path_runs_as_before(self):
        from payments.models import EscrowPayment, EscrowStatus

        self.job.state = JobState.ESCROW_HELD
        self.job.save(update_fields=["state"])
        # Built through the same rules the funding flow uses, so this is a
        # realistic hold rather than a row shaped to suit the test.
        from config import business_rules as rules

        EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=self.job.fixed_pay,
            platform_fee=rules.platform_fee_for(self.job.fixed_pay),
            worker_payout=rules.worker_payout_for(self.job.fixed_pay),
            status=EscrowStatus.AUTHORIZED,
        )

        worklog_services.check_in(self.job, self.worker_profile)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.IN_PROGRESS)

        worklog_services.complete(self.job, self.worker_profile)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.COMPLETED)
        self.assertTrue(hasattr(self.job, "completion"))


class EscrowIsolationTests(JobFactoryMixin, TestCase):
    """Neither path may reach into the other."""

    def finished_direct_job(self):
        job = self.gig()
        job.state = JobState.ACCEPTED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])
        self.day_arrives(job)
        worklog_services.mark_work_finished(job, self.worker_profile)
        return job

    def test_a_direct_deal_creates_no_escrow_row(self):
        from payments.models import EscrowPayment

        job = self.finished_direct_job()
        worklog_services.confirm_closed(job, self.client_user)
        self.assertEqual(EscrowPayment.objects.count(), 0)

    def test_the_settle_sweep_ignores_directly_settled_work(self):
        """No hold means no window, so nothing may release on a timer."""
        job = self.finished_direct_job()
        worklog_services.settle_due()
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.COMPLETED)

    def test_a_standing_position_never_uses_escrow(self):
        """No single day to sign off, whatever the flag says."""
        job = self.standing()
        job.use_escrow = True
        self.assertFalse(job.is_escrowed)
