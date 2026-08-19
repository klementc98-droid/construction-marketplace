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


class InboxOrderTests(MessagingTestCase):
    """Whoever wrote to you last is at the top.

    This did not hold, and nothing caught it: with_unread_for annotates a
    Count, which makes the query a GROUP BY, and Django drops a model's
    default Meta.ordering on those. The inbox came back in whatever order the
    database chose, so the ordering declared on the model was decorative.
    """

    def conversation_with(self, worker_email, when):
        from datetime import timedelta

        worker = WorkerProfile.objects.create(
            user=User.objects.create_user(email=worker_email, full_name=worker_email),
            region=self.region,
        )
        job = self.make_gig()
        Application.objects.create(job=job, worker=worker)
        conversation = Conversation.objects.create(job=job, worker=worker)
        message = Message.objects.create(
            conversation=conversation, sender=worker.user, body="hi"
        )
        Message.objects.filter(pk=message.pk).update(created_at=when)
        conversation.last_message_at = when
        conversation.save(update_fields=["last_message_at"])
        return conversation

    def test_the_most_recent_conversation_comes_first(self):
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        yesterday = self.conversation_with("x@example.com", now - timedelta(days=1))
        today = self.conversation_with("y@example.com", now)

        self.client.force_login(self.client_user)
        listed = list(self.client.get(reverse("messaging:inbox")).context["conversations"])
        self.assertEqual(listed[0].pk, today.pk)
        self.assertEqual(listed[1].pk, yesterday.pk)

    def test_a_thread_nobody_has_written_in_sorts_last(self):
        """No last_message_at at all — "never used", not "just now"."""
        from datetime import timedelta

        from django.utils import timezone

        old = self.conversation_with("x@example.com", timezone.now() - timedelta(days=9))
        silent_worker = WorkerProfile.objects.create(
            user=User.objects.create_user(email="quiet@example.com", full_name="Quiet"),
            region=self.region,
        )
        job = self.make_gig()
        Application.objects.create(job=job, worker=silent_worker)
        silent = Conversation.objects.create(job=job, worker=silent_worker)

        self.client.force_login(self.client_user)
        listed = list(self.client.get(reverse("messaging:inbox")).context["conversations"])
        self.assertEqual(listed[0].pk, old.pk)
        self.assertEqual(listed[-1].pk, silent.pk)

    def test_a_new_reply_moves_its_thread_to_the_top(self):
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        older = self.conversation_with("x@example.com", now - timedelta(days=2))
        newer = self.conversation_with("y@example.com", now - timedelta(days=1))

        self.client.force_login(self.client_user)
        self.client.post(
            reverse("messaging:thread", args=[older.pk]), {"body": "back to you"}
        )
        listed = list(self.client.get(reverse("messaging:inbox")).context["conversations"])
        self.assertEqual(listed[0].pk, older.pk)
        self.assertEqual(listed[1].pk, newer.pk)


class InboxBookingTests(MessagingTestCase):
    """A booking is one conversation in the list, however many days it is.

    Four days is four threads underneath, because a thread belongs to a job and
    each day is its own job. That is storage. An inbox showing the same person
    and the same trade four times is not.
    """

    def booking(self, count=4):
        from uuid import uuid4

        group = uuid4()
        days = []
        for n in range(count):
            job = self.make_gig(
                offer_group=group,
                gig_date=timezone.localdate() + timedelta(days=3 + n),
            )
            days.append(job)
            Conversation.objects.create(job=job, worker=self.worker_profile)
        return group, days

    def test_a_four_day_booking_is_one_row(self):
        self.booking(4)
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("messaging:inbox"))

        self.assertEqual(len(response.context["conversations"]), 1)

    def test_the_row_says_how_many_days_it_stands_for(self):
        self.booking(4)
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("messaging:inbox"))

        self.assertEqual(response.context["conversations"][0].job.group_days, 4)

    def test_separate_jobs_stay_separate_rows(self):
        """The collapse is about one booking, not about one pair of people."""
        for _ in range(3):
            job = self.make_gig()
            Conversation.objects.create(job=job, worker=self.worker_profile)

        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("messaging:inbox"))

        self.assertEqual(len(response.context["conversations"]), 3)

    def test_unread_on_any_day_counts_on_the_row(self):
        """Otherwise a booking whose only unread question sat on Wednesday
        showed a clean row and the question went unanswered."""
        _group, days = self.booking(3)
        # Not the first day, which is the one the row is most likely to be.
        conversation = Conversation.objects.get(job=days[2])
        Message.objects.create(
            conversation=conversation, sender=self.client_user, body="You about?"
        )

        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("messaging:inbox"))

        rows = response.context["conversations"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].unread_count, 1)
