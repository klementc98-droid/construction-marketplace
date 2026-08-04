"""Gig expiry: what retires, what must not, and who gets told.

The costly mistakes here are both silent. Expiring too eagerly kills a job
somebody is actually working — the worker turns up and the post says the day
never happened. Expiring too timidly leaves unfillable gigs on the board, and
workers waste applications on dates that went by last week.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.state_machine import ESCROW_FUNDED_STATES, JobState
from messaging.models import Conversation, Message

from .models import Application, ApplicationStatus, Job, JobType
from .services import due_for_expiry, expire, expire_stale_gigs
from .tests import JobFactoryMixin


class ExpirySelectionTests(JobFactoryMixin, TestCase):
    """Which rows the sweep picks up — and, more importantly, which it does not."""

    def yesterday(self):
        return timezone.localdate() - timedelta(days=1)

    def test_posted_gig_past_its_date_expires(self):
        job = self.gig(gig_date=self.yesterday())
        self.assertIn(job, due_for_expiry())
        expire_stale_gigs()
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.EXPIRED)

    def test_accepted_but_unfunded_gig_expires(self):
        """The state machine has always allowed this route — see its own note:
        an unfunded acceptance is not yet a promise worth enforcing."""
        job = self.gig(gig_date=self.yesterday(), state=JobState.ACCEPTED)
        expire_stale_gigs()
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.EXPIRED)

    def test_todays_gig_is_not_expired(self):
        """The day is over when the date has passed, not while it is running.
        A gig booked for this morning is still a gig this afternoon."""
        job = self.gig(gig_date=timezone.localdate())
        expire_stale_gigs()
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.POSTED)

    def test_future_gig_is_not_expired(self):
        job = self.gig(gig_date=timezone.localdate() + timedelta(days=2))
        expire_stale_gigs()
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.POSTED)

    def test_standing_position_never_expires(self):
        """It has no date. It stays open until the client closes it."""
        job = self.standing()
        self.assertNotIn(job, due_for_expiry())
        expire_stale_gigs()
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.POSTED)

    def test_funded_and_later_states_are_untouched(self):
        """Once the money is committed the ordinary lifecycle owns the job,
        whatever the original date says. A worker who checked in a day late
        still gets paid."""
        for state in sorted(ESCROW_FUNDED_STATES):
            with self.subTest(state=state):
                job = self.gig(gig_date=self.yesterday(), state=state)
                expire_stale_gigs()
                job.refresh_from_db()
                self.assertEqual(job.state, state)

    def test_terminal_states_are_untouched(self):
        for state in (JobState.PAID_OUT, JobState.CANCELLED, JobState.REFUNDED):
            with self.subTest(state=state):
                job = self.gig(gig_date=self.yesterday(), state=state)
                expire_stale_gigs()
                job.refresh_from_db()
                self.assertEqual(job.state, state)

    def test_running_twice_is_a_no_op(self):
        job = self.gig(gig_date=self.yesterday())
        self.assertEqual(len(expire_stale_gigs()), 1)
        self.assertEqual(expire_stale_gigs(), [])
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.EXPIRED)

    def test_one_bad_row_does_not_abandon_the_batch(self):
        """A row that moves on between the queryset and the lock is the
        ordinary race, not an error."""
        doomed = self.gig(gig_date=self.yesterday(), title="A")
        other = self.gig(gig_date=self.yesterday(), title="B")
        Job.objects.filter(pk=doomed.pk).update(state=JobState.CANCELLED)

        expired = expire_stale_gigs()

        self.assertEqual([j.pk for j in expired], [other.pk])
        other.refresh_from_db()
        self.assertEqual(other.state, JobState.EXPIRED)


class ExpiryNotificationTests(JobFactoryMixin, TestCase):
    """Anyone still waiting on an answer hears that the gig is off."""

    def setUp(self):
        self.job = self.gig(gig_date=timezone.localdate() - timedelta(days=1))

    def test_pending_applicant_gets_a_message(self):
        Application.objects.create(
            job=self.job, worker=self.worker_profile, message="Free that day."
        )
        expire(self.job)

        conversation = Conversation.objects.get(job=self.job, worker=self.worker_profile)
        body = Message.objects.get(conversation=conversation).body
        self.assertIn("expired", body)
        self.assertIn("Nothing was charged", body)

    def test_the_thread_is_opened_if_nobody_had_spoken_yet(self):
        """Most applications never get a thread. Applying is what earns the
        pair a channel, so creating it here is legitimate."""
        Application.objects.create(job=self.job, worker=self.worker_profile)
        self.assertFalse(Conversation.objects.filter(job=self.job).exists())

        expire(self.job)

        self.assertTrue(Conversation.objects.filter(job=self.job).exists())

    def test_the_message_drives_the_unread_badge(self):
        """It is only useful if it is unread and from the other side — that is
        what the header counts."""
        Application.objects.create(job=self.job, worker=self.worker_profile)
        expire(self.job)

        message = Message.objects.get()
        self.assertIsNone(message.read_at)
        self.assertEqual(message.sender, self.client_profile.user)

    def test_withdrawn_and_passed_over_applicants_are_not_messaged(self):
        """They already have their answer. Telling them again is noise."""
        for status in (ApplicationStatus.WITHDRAWN, ApplicationStatus.PASSED_OVER):
            with self.subTest(status=status):
                Message.objects.all().delete()
                Application.objects.all().delete()
                Application.objects.create(
                    job=self.job, worker=self.worker_profile, status=status
                )
                expire(Job.objects.get(pk=self.job.pk))
                self.assertEqual(Message.objects.count(), 0)
                Job.objects.filter(pk=self.job.pk).update(state=JobState.POSTED)

    def test_conversation_last_message_at_is_touched(self):
        """Otherwise the thread sinks to the bottom of the inbox and the
        message nobody sees is the one that mattered."""
        Application.objects.create(job=self.job, worker=self.worker_profile)
        expire(self.job)
        conversation = Conversation.objects.get()
        self.assertIsNotNone(conversation.last_message_at)


class ExpiryVisibilityTests(JobFactoryMixin, TestCase):
    """Off the board, still on Mine, and impossible to apply to."""

    def setUp(self):
        self.job = self.gig(gig_date=timezone.localdate() - timedelta(days=1))
        expire(self.job)

    def test_dropped_from_the_public_board(self):
        self.assertNotIn(self.job, Job.objects.public())
        self.assertNotIn(self.job, Job.objects.open())

    def test_dropped_from_search(self):
        self.assertNotIn(
            self.job, Job.objects.public().matching("Framing")
        )

    def test_still_listed_for_the_client_who_posted_it(self):
        """They may want to reference it, or repost the same work."""
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:mine"))
        self.assertContains(response, self.job.title)

    def test_mine_shows_it_as_expired(self):
        # expire() locks and saves a *copy* of the row, so the instance held
        # here is still on the old state. Read it back before asking it what
        # tone it should be, or this asserts against POSTED's colour.
        self.job.refresh_from_db()
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:mine"))
        self.assertContains(response, "Expired")
        # The tone class is what makes it read as "over" rather than "live".
        self.assertEqual(self.job.state_tone, "over")
        self.assertContains(response, "state-over")

    def test_applying_directly_is_refused(self):
        """An old link or a notification must not get anyone through."""
        self.client.force_login(self.worker_user)
        response = self.client.post(
            reverse("jobs:apply", args=[self.job.pk]),
            {"message": "Still keen"},
            follow=True,
        )
        self.assertEqual(Application.objects.count(), 0)
        self.assertContains(response, "no longer taking applications")

    def test_the_apply_button_is_gone_from_the_job_page(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("jobs:detail", args=[self.job.pk]))
        self.assertNotContains(
            response, reverse("jobs:apply", args=[self.job.pk])
        )

    def test_the_state_is_stored_not_computed(self):
        """The point of doing this as a transition: every reader agrees,
        and it is queryable."""
        self.assertEqual(
            Job.objects.filter(state=JobState.EXPIRED).count(), 1
        )
        self.job.refresh_from_db()
        self.assertTrue(self.job.state == JobState.EXPIRED)
        self.assertFalse(self.job.is_open)


class ExpiryCommandTests(JobFactoryMixin, TestCase):
    def test_dry_run_changes_nothing(self):
        job = self.gig(gig_date=timezone.localdate() - timedelta(days=1))
        out = StringIO()
        call_command("expire_stale_gigs", "--dry-run", stdout=out)

        self.assertIn("would expire", out.getvalue())
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.POSTED)

    def test_command_expires_and_reports(self):
        job = self.gig(gig_date=timezone.localdate() - timedelta(days=1))
        out = StringIO()
        call_command("expire_stale_gigs", stdout=out)

        self.assertIn("Expired 1", out.getvalue())
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.EXPIRED)

    def test_quiet_when_there_is_nothing_to_do(self):
        out = StringIO()
        call_command("expire_stale_gigs", "--dry-run", stdout=out)
        self.assertIn("Nothing to expire", out.getvalue())
