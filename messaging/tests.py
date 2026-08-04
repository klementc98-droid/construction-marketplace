"""Phase 3 tests. Most of these are about who may NOT talk to whom."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import ClientProfile, WorkerProfile
from core.models import Region, Trade
from jobs.models import Application, ApplicationStatus, Job, JobType

from .models import Conversation, Message, can_converse

User = get_user_model()


class MessagingTestCase(TestCase):
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
        cls.stranger = User.objects.create_user(
            email="stranger@example.com", full_name="Stranger"
        )
        WorkerProfile.objects.create(user=cls.stranger, region=cls.region)

    def make_gig(self, **overrides):
        defaults = dict(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.trade,
            region=self.region,
            title="Framing help",
            description="Second storey.",
            gig_date=timezone.localdate() + timedelta(days=3),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("90"),
        )
        return Job.objects.create(**(defaults | overrides))


class AccessTests(MessagingTestCase):
    def test_no_channel_before_any_application(self):
        job = self.make_gig()
        self.assertFalse(can_converse(job, self.worker_profile))

    def test_applying_opens_the_channel(self):
        job = self.make_gig()
        Application.objects.create(job=job, worker=self.worker_profile)
        self.assertTrue(can_converse(job, self.worker_profile))

    def test_withdrawing_closes_it_again(self):
        job = self.make_gig()
        Application.objects.create(
            job=job,
            worker=self.worker_profile,
            status=ApplicationStatus.WITHDRAWN,
        )
        self.assertFalse(can_converse(job, self.worker_profile))

    def test_an_existing_thread_keeps_the_channel_open(self):
        """A client's direct contact survives the worker not having applied."""
        job = self.make_gig()
        Conversation.objects.create(job=job, worker=self.worker_profile)
        self.assertTrue(can_converse(job, self.worker_profile))

    def test_being_assigned_is_enough(self):
        job = self.make_gig()
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["assigned_worker"])
        self.assertFalse(
            Application.objects.filter(job=job, worker=self.worker_profile).exists()
        )
        self.assertTrue(can_converse(job, self.worker_profile))

    def test_starting_a_thread_without_mutual_interest_is_refused(self):
        job = self.make_gig()
        self.client.force_login(self.worker_user)
        response = self.client.post(
            reverse("messaging:start", args=[job.pk, self.worker_profile.pk])
        )
        self.assertRedirects(response, reverse("jobs:detail", args=[job.pk]))
        self.assertFalse(Conversation.objects.exists())

    def test_a_stranger_cannot_open_a_thread_on_someone_elses_job(self):
        job = self.make_gig()
        Application.objects.create(job=job, worker=self.worker_profile)
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("messaging:start", args=[job.pk, self.worker_profile.pk])
        )
        self.assertRedirects(response, reverse("jobs:detail", args=[job.pk]))
        self.assertFalse(Conversation.objects.exists())

    def test_a_stranger_cannot_read_a_thread(self):
        job = self.make_gig()
        conversation = Conversation.objects.create(job=job, worker=self.worker_profile)
        Message.objects.create(
            conversation=conversation, sender=self.client_user, body="Secret rate talk"
        )
        self.client.force_login(self.stranger)
        response = self.client.get(
            reverse("messaging:thread", args=[conversation.pk])
        )
        self.assertRedirects(response, reverse("messaging:inbox"))

    def test_a_stranger_cannot_post_into_a_thread(self):
        job = self.make_gig()
        conversation = Conversation.objects.create(job=job, worker=self.worker_profile)
        self.client.force_login(self.stranger)
        self.client.post(
            reverse("messaging:thread", args=[conversation.pk]), {"body": "Hello"}
        )
        self.assertEqual(conversation.messages.count(), 0)


class ThreadTests(MessagingTestCase):
    def setUp(self):
        self.job = self.make_gig()
        Application.objects.create(job=self.job, worker=self.worker_profile)

    def test_both_sides_can_open_the_same_single_thread(self):
        self.client.force_login(self.client_user)
        self.client.post(
            reverse("messaging:start", args=[self.job.pk, self.worker_profile.pk])
        )
        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("messaging:start", args=[self.job.pk, self.worker_profile.pk])
        )
        self.assertEqual(Conversation.objects.count(), 1)

    def test_only_one_thread_per_job_and_worker(self):
        Conversation.objects.create(job=self.job, worker=self.worker_profile)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Conversation.objects.create(job=self.job, worker=self.worker_profile)

    def test_sending_stamps_the_conversation_for_inbox_sorting(self):
        conversation = Conversation.objects.create(
            job=self.job, worker=self.worker_profile
        )
        self.assertIsNone(conversation.last_message_at)

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("messaging:thread", args=[conversation.pk]),
            {"body": "I can do Thursday."},
        )
        conversation.refresh_from_db()
        self.assertIsNotNone(conversation.last_message_at)
        self.assertEqual(conversation.messages.count(), 1)

    def test_opening_a_thread_marks_the_other_sides_messages_read(self):
        conversation = Conversation.objects.create(
            job=self.job, worker=self.worker_profile
        )
        message = Message.objects.create(
            conversation=conversation, sender=self.client_user, body="Are you free?"
        )
        self.assertIsNone(message.read_at)

        self.client.force_login(self.worker_user)
        self.client.get(reverse("messaging:thread", args=[conversation.pk]))
        message.refresh_from_db()
        self.assertIsNotNone(message.read_at)

    def test_your_own_messages_never_count_as_unread_to_you(self):
        conversation = Conversation.objects.create(
            job=self.job, worker=self.worker_profile
        )
        Message.objects.create(
            conversation=conversation, sender=self.worker_user, body="Mine"
        )
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("jobs:list"))
        self.assertEqual(response.context["unread_messages"], 0)

    def test_the_badge_counts_only_what_the_other_side_sent(self):
        conversation = Conversation.objects.create(
            job=self.job, worker=self.worker_profile
        )
        Message.objects.create(
            conversation=conversation, sender=self.client_user, body="One"
        )
        Message.objects.create(
            conversation=conversation, sender=self.client_user, body="Two"
        )
        Message.objects.create(
            conversation=conversation, sender=self.worker_user, body="Reply"
        )
        self.client.force_login(self.worker_user)
        self.assertEqual(
            self.client.get(reverse("jobs:list")).context["unread_messages"], 2
        )
        self.client.force_login(self.client_user)
        self.assertEqual(
            self.client.get(reverse("jobs:list")).context["unread_messages"], 1
        )

    def test_anonymous_visitors_get_a_zero_badge_not_an_error(self):
        response = self.client.get(reverse("jobs:list"))
        self.assertEqual(response.context["unread_messages"], 0)


class DirectContactTests(MessagingTestCase):
    def test_a_client_must_attach_direct_contact_to_one_of_their_own_gigs(self):
        mine = self.make_gig(title="My gig")
        other_client = User.objects.create_user(email="other@example.com")
        other_profile = ClientProfile.objects.create(
            user=other_client, region=self.region
        )
        theirs = self.make_gig(title="Their gig", client=other_profile)

        self.client.force_login(self.client_user)
        response = self.client.post(
            reverse("messaging:start_direct", args=[self.worker_profile.pk]),
            {"job": theirs.pk},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Conversation.objects.exists())

        self.client.post(
            reverse("messaging:start_direct", args=[self.worker_profile.pk]),
            {"job": mine.pk},
        )
        self.assertTrue(Conversation.objects.filter(job=mine).exists())

    def test_a_worker_cannot_use_direct_contact(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(
            reverse("messaging:start_direct", args=[self.worker_profile.pk])
        )
        self.assertRedirects(response, reverse("accounts:select_role"))


class InboxTests(MessagingTestCase):
    def test_the_inbox_shows_only_your_own_threads(self):
        job = self.make_gig()
        Application.objects.create(job=job, worker=self.worker_profile)
        mine = Conversation.objects.create(job=job, worker=self.worker_profile)

        outsider_worker = WorkerProfile.objects.get(user=self.stranger)
        other_client_user = User.objects.create_user(email="oc@example.com")
        other_client = ClientProfile.objects.create(
            user=other_client_user, region=self.region
        )
        theirs_job = self.make_gig(client=other_client, title="Not yours")
        Conversation.objects.create(job=theirs_job, worker=outsider_worker)

        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("messaging:inbox"))
        conversations = list(response.context["conversations"])
        self.assertEqual(conversations, [mine])
