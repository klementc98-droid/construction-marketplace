"""What gets queued, what does not, and what happens when sending goes wrong.

The interesting cases are nearly all about restraint. An email that arrives five
times teaches people to filter the sender, which costs far more than the four
extra copies — so most of this file is about the emails that must NOT be
written.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core import mail as django_mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import AvailabilityStatus, ClientProfile, WorkerProfile
from core.models import Region, Trade
from jobs.models import Job, JobType
from notifications.models import Kind, Notification
from notifications.services import audience_for, booking_key, notify

User = get_user_model()


class NotificationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.filter(is_active=True).first()
        cls.trade = Trade.objects.get(slug="carpenter")
        cls.client_user = User.objects.create_user(
            email="poster@example.com", full_name="Poster"
        )
        cls.client_profile = ClientProfile.objects.create(
            user=cls.client_user, region=cls.region
        )
        cls.worker_user = User.objects.create_user(
            email="worker@example.com", full_name="Worker"
        )
        cls.worker_profile = WorkerProfile.objects.create(
            user=cls.worker_user, region=cls.region
        )

    def gig(self, **overrides):
        defaults = dict(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.trade,
            region=self.region,
            title="Framing help",
            description="Second storey rebuild.",
            gig_date=timezone.localdate() + timedelta(days=3),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("90"),
        )
        return Job.objects.create(**(defaults | overrides))


class WhatIsQueuedTests(NotificationTestCase):
    def test_a_notification_is_written(self):
        job = self.gig()
        row = notify(self.worker_user, Kind.SELECTED, job=job, job_title=job.title)

        self.assertIsNotNone(row)
        self.assertEqual(row.recipient, self.worker_user)
        self.assertIsNone(row.sent_at)

    def test_nobody_is_told_what_they_just_did_themselves(self):
        """The commonest way to make an app feel stupid."""
        job = self.gig()
        row = notify(
            self.client_user, Kind.APPLICATION, job=job, actor=self.client_user
        )
        self.assertIsNone(row)

    def test_turning_email_off_turns_it_off(self):
        self.worker_user.email_notifications = False
        self.worker_user.save(update_fields=["email_notifications"])

        row = notify(self.worker_user, Kind.SELECTED, job=self.gig())
        self.assertIsNone(row)
        self.assertEqual(Notification.objects.count(), 0)

    def test_a_booking_is_one_email(self):
        """Five days of one offer is one piece of news."""
        group = uuid4()
        days = [
            self.gig(
                offer_group=group,
                gig_date=timezone.localdate() + timedelta(days=n),
            )
            for n in (3, 4, 5, 6, 7)
        ]
        for day in days:
            notify(
                self.worker_user,
                Kind.OFFER_RECEIVED,
                job=day,
                dedupe=booking_key("offer", day, self.worker_profile.pk),
            )

        self.assertEqual(Notification.objects.count(), 1)

    def test_two_applicants_to_one_booking_are_two_emails(self):
        """The other half of the dedupe rule, and the half that is easy to get
        wrong: collapsing these would tell the client about the first person and
        silently swallow the second."""
        rival = WorkerProfile.objects.create(
            user=User.objects.create_user(email="rival@example.com"),
            region=self.region,
        )
        job = self.gig(offer_group=uuid4())

        for applicant in (self.worker_profile, rival):
            notify(
                self.client_user,
                Kind.APPLICATION,
                job=job,
                dedupe=booking_key("application", job, applicant.pk),
            )

        self.assertEqual(Notification.objects.count(), 2)

    def test_a_repeat_is_allowed_once_the_first_has_gone_out(self):
        """Being offered the same week again next month is news again."""
        job = self.gig()
        key = booking_key("offer", job, self.worker_profile.pk)

        first = notify(self.worker_user, Kind.OFFER_RECEIVED, job=job, dedupe=key)
        Notification.objects.filter(pk=first.pk).update(sent_at=timezone.now())
        second = notify(self.worker_user, Kind.OFFER_RECEIVED, job=job, dedupe=key)

        self.assertIsNotNone(second)


class BroadcastTests(NotificationTestCase):
    """Who hears that a job was posted.

    The only notification in the app that reaches people with no connection to
    the job, so it is the only one that can annoy somebody who never asked. All
    of these are about who must NOT get it.
    """

    def setUp(self):
        self.worker_profile.trades.add(self.trade)

    def test_the_trade_is_told(self):
        job = self.gig()
        self.assertIn(self.worker_profile, audience_for(job))

    def test_another_trade_is_not(self):
        """One irrelevant email is all it takes for the next to go unread."""
        other = Trade.objects.exclude(pk=self.trade.pk).first()
        job = self.gig(trade=other)
        self.assertNotIn(self.worker_profile, audience_for(job))

    def test_somebody_not_taking_work_is_not(self):
        """"Not currently available" is an answer to this question too."""
        self.worker_profile.availability_status = AvailabilityStatus.UNAVAILABLE
        self.worker_profile.save(update_fields=["availability_status"])

        self.assertNotIn(self.worker_profile, audience_for(self.gig()))

    def test_a_direct_offer_is_never_announced(self):
        """Announcing it would mislead everyone else and leak who is being
        offered what."""
        job = self.gig(is_private=True)
        self.assertEqual(list(audience_for(job)), [])

    def test_posting_a_booking_tells_each_person_once(self):
        """Four days must not be four emails to every plumber in the city."""
        group = uuid4()
        days = [
            self.gig(
                offer_group=group,
                gig_date=timezone.localdate() + timedelta(days=n),
            )
            for n in (3, 4, 5, 6)
        ]
        for day in days:
            for worker in audience_for(day):
                notify(
                    worker.user,
                    Kind.JOB_POSTED,
                    job=day,
                    dedupe=booking_key("posted", day, worker.pk),
                )

        self.assertEqual(
            Notification.objects.filter(kind=Kind.JOB_POSTED).count(), 1
        )

    def test_a_client_is_not_told_about_their_own_post(self):
        """Somebody who is both a client and a plumber matches their own job."""
        both = WorkerProfile.objects.create(
            user=self.client_user, region=self.region
        )
        both.trades.add(self.trade)
        job = self.gig()

        self.assertIn(both, audience_for(job))
        self.assertIsNone(
            notify(self.client_user, Kind.JOB_POSTED, job=job, actor=self.client_user)
        )


class ReminderTests(NotificationTestCase):
    def test_nobody_with_a_clear_list_is_nudged(self):
        call_command("send_reminders")
        self.assertEqual(Notification.objects.filter(kind=Kind.REMINDER).count(), 0)

    def test_somebody_with_an_unanswered_offer_is(self):
        from jobs.models import Offer

        job = self.gig(is_private=True)
        Offer.objects.create(job=job, worker=self.worker_profile)

        call_command("send_reminders")

        row = Notification.objects.get(kind=Kind.REMINDER)
        self.assertEqual(row.recipient, self.worker_user)
        self.assertEqual(row.payload["offers"], 1)

    def test_running_it_again_the_same_week_does_not_nudge_twice(self):
        """One email listing four things, not four emails — and not a fresh one
        every time cron fires."""
        from jobs.models import Offer

        Offer.objects.create(job=self.gig(is_private=True), worker=self.worker_profile)

        call_command("send_reminders")
        call_command("send_reminders")

        self.assertEqual(Notification.objects.filter(kind=Kind.REMINDER).count(), 1)


class SendingTests(NotificationTestCase):
    def queue(self, **overrides):
        job = self.gig()
        return notify(
            self.worker_user,
            Kind.SELECTED,
            job=job,
            client="Poster",
            job_title=job.title,
            pay="90",
            hours="8",
            **overrides,
        )

    def test_the_command_sends_and_marks_it_sent(self):
        row = self.queue()
        call_command("send_notifications")

        row.refresh_from_db()
        self.assertIsNotNone(row.sent_at)
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertEqual(django_mail.outbox[0].to, ["worker@example.com"])

    def test_a_sent_email_is_never_sent_twice(self):
        self.queue()
        call_command("send_notifications")
        call_command("send_notifications")

        self.assertEqual(len(django_mail.outbox), 1)

    def test_the_body_carries_the_job_and_a_link_back(self):
        row = self.queue()
        call_command("send_notifications")

        body = django_mail.outbox[0].body
        self.assertIn("Framing help", body)
        self.assertIn(row.job.get_absolute_url(), body)

    def test_it_is_written_in_the_recipients_language(self):
        """Not in the language of whoever pressed the button. A Greek worker
        gets Greek when an English client picked them."""
        self.worker_user.language = "el"
        self.worker_user.save(update_fields=["language"])

        self.queue()
        call_command("send_notifications")

        self.assertNotIn("You're getting this because", django_mail.outbox[0].body)

    def test_one_bad_row_does_not_stop_the_rest(self):
        """A run that dies on one broken row is a queue that stops delivering
        the moment anything is odd."""
        broken = self.queue()
        Notification.objects.filter(pk=broken.pk).update(kind="no_such_kind")
        good = notify(
            self.worker_user, Kind.JOB_CLOSED, job=self.gig(), job_title="Other"
        )

        call_command("send_notifications")

        good.refresh_from_db()
        broken.refresh_from_db()
        self.assertIsNotNone(good.sent_at)
        self.assertIsNone(broken.sent_at)
        self.assertEqual(broken.attempts, 1)
        self.assertTrue(broken.last_error)

    def test_a_row_that_keeps_failing_is_eventually_left_alone(self):
        """Something is wrong with the template or the address, not with the
        network, and retrying it every five minutes forever turns a sending
        queue into a log nobody reads."""
        broken = self.queue()
        Notification.objects.filter(pk=broken.pk).update(
            kind="no_such_kind", attempts=5
        )

        call_command("send_notifications")

        broken.refresh_from_db()
        self.assertEqual(broken.attempts, 5)
