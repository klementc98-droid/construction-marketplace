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
from core.state_machine import JobState
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
        row = notify(
            self.worker_user, Kind.OFFER_RECEIVED, job=job, job_title=job.title
        )

        self.assertIsNotNone(row)
        self.assertEqual(row.recipient, self.worker_user)
        self.assertIsNone(row.sent_at)

    def test_nobody_is_told_what_they_just_did_themselves(self):
        """The commonest way to make an app feel stupid."""
        job = self.gig()
        row = notify(
            self.client_user, Kind.OFFER_RECEIVED, job=job, actor=self.client_user
        )
        self.assertIsNone(row)

    def test_turning_email_off_turns_it_off(self):
        self.worker_user.email_notifications = False
        self.worker_user.save(update_fields=["email_notifications"])

        row = notify(self.worker_user, Kind.OFFER_RECEIVED, job=self.gig())
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

    def test_the_key_keeps_two_actors_apart(self):
        """The other half of the dedupe rule, and the half that is easy to get
        wrong. Collapsing a booking is right; collapsing two different people
        doing the same thing to it would announce the first and silently
        swallow the second, so the actor has to be in the key."""
        job = self.gig(offer_group=uuid4())
        rival = WorkerProfile.objects.create(
            user=User.objects.create_user(email="rival@example.com"),
            region=self.region,
        )

        self.assertNotEqual(
            booking_key("application", job, self.worker_profile.pk),
            booking_key("application", job, rival.pk),
        )

    def test_the_key_treats_every_day_of_a_booking_as_one(self):
        group = uuid4()
        monday = self.gig(offer_group=group)
        friday = self.gig(
            offer_group=group, gig_date=timezone.localdate() + timedelta(days=7)
        )

        self.assertEqual(
            booking_key("offer", monday, self.worker_profile.pk),
            booking_key("offer", friday, self.worker_profile.pk),
        )

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

    def test_the_client_is_still_in_the_audience_but_never_written_to(self):
        """Somebody who is both a client and a plumber matches their own job.
        The audience rule does not know that; notify() does."""
        both = WorkerProfile.objects.create(
            user=self.client_user, region=self.region
        )
        both.trades.add(self.trade)
        job = self.gig()

        self.assertIn(both, audience_for(job))
        self.assertIsNone(
            notify(self.client_user, Kind.JOB_POSTED, job=job, actor=self.client_user)
        )

    def test_announcing_is_currently_switched_off(self):
        """The rule above is kept working and tested; the emails are not sent.
        See ENABLED — this is a decision about volume, not a missing feature."""
        self.assertIsNone(
            notify(self.worker_user, Kind.JOB_POSTED, job=self.gig())
        )


class TomorrowTests(NotificationTestCase):
    """The night-before reminder.

    The one place a booking is deliberately NOT collapsed: tomorrow is a
    particular morning somebody has to turn up on.
    """

    def booked(self, when, **overrides):
        job = self.gig(gig_date=when, **overrides)
        job.state = JobState.ACCEPTED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])
        return job

    def test_tomorrows_job_is_reminded(self):
        job = self.booked(timezone.localdate() + timedelta(days=1))
        call_command("remind_tomorrow")

        row = Notification.objects.get(kind=Kind.TOMORROW)
        self.assertEqual(row.recipient, self.worker_user)
        self.assertEqual(row.job, job)

    def test_a_job_further_out_is_left_alone(self):
        self.booked(timezone.localdate() + timedelta(days=5))
        call_command("remind_tomorrow")
        self.assertEqual(Notification.objects.count(), 0)

    def test_a_job_nobody_took_is_not_reminded(self):
        """No worker, nobody to remind — and it may never be filled at all."""
        self.gig(gig_date=timezone.localdate() + timedelta(days=1))
        call_command("remind_tomorrow")
        self.assertEqual(Notification.objects.count(), 0)

    def test_a_finished_job_is_not_reminded(self):
        job = self.booked(timezone.localdate() + timedelta(days=1))
        job.state = JobState.CLOSED
        job.save(update_fields=["state"])

        call_command("remind_tomorrow")
        self.assertEqual(Notification.objects.count(), 0)

    def test_running_it_twice_in_a_day_reminds_once(self):
        self.booked(timezone.localdate() + timedelta(days=1))

        call_command("remind_tomorrow")
        call_command("remind_tomorrow")

        self.assertEqual(Notification.objects.filter(kind=Kind.TOMORROW).count(), 1)

    def test_each_day_of_a_booking_gets_its_own_night_before(self):
        """The one reminder that must not collapse. Telling somebody about
        Monday and leaving them to remember Thursday is how a week gets a day
        missed."""
        group = uuid4()
        monday = self.booked(
            timezone.localdate() + timedelta(days=1), offer_group=group
        )
        thursday = self.booked(
            timezone.localdate() + timedelta(days=4), offer_group=group
        )

        call_command("remind_tomorrow")
        call_command("remind_tomorrow", "--days", "4")

        reminded = set(
            Notification.objects.filter(kind=Kind.TOMORROW).values_list(
                "job_id", flat=True
            )
        )
        self.assertEqual(reminded, {monday.pk, thursday.pk})

    def test_it_says_where_in_the_booking_the_day_falls(self):
        group = uuid4()
        self.booked(timezone.localdate() + timedelta(days=1), offer_group=group)
        self.booked(timezone.localdate() + timedelta(days=2), offer_group=group)
        self.booked(timezone.localdate() + timedelta(days=3), offer_group=group)

        call_command("remind_tomorrow")

        row = Notification.objects.get(kind=Kind.TOMORROW)
        self.assertEqual(row.payload["day_number"], 1)
        self.assertEqual(row.payload["of_days"], 3)


class EnabledKindsTests(NotificationTestCase):
    """Only two things are emailed. The rest are built, translated and off."""

    def test_an_offer_is_emailed(self):
        self.assertIsNotNone(
            notify(self.worker_user, Kind.OFFER_RECEIVED, job=self.gig())
        )

    def test_the_night_before_is_emailed(self):
        self.assertIsNotNone(
            notify(self.worker_user, Kind.TOMORROW, job=self.gig())
        )

    def test_everything_else_is_written_off_rather_than_written_down(self):
        for kind in (
            Kind.MESSAGE,
            Kind.APPLICATION,
            Kind.SELECTED,
            Kind.JOB_POSTED,
            Kind.JOB_CLOSED,
            Kind.PAYMENT_RELEASED,
            Kind.RATING,
        ):
            with self.subTest(kind=kind):
                self.assertIsNone(notify(self.worker_user, kind, job=self.gig()))
        self.assertEqual(Notification.objects.count(), 0)


class SendingTests(NotificationTestCase):
    def queue(self, **overrides):
        job = self.gig()
        return notify(
            self.worker_user,
            Kind.OFFER_RECEIVED,
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
            self.worker_user,
            Kind.TOMORROW,
            job=self.gig(),
            job_title="Other",
            client="Poster",
            pay="90",
            hours="8",
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
