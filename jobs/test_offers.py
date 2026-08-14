"""Direct offers: a client approaching one worker instead of advertising.

The cases here are the ones that cost somebody real money or a real day — an
offer leaking onto the public board, two people both being told the same gig is
theirs, or a stranger accepting work written for someone else. The happy path
is covered too, but it is not what this file is for.

Kept apart from ``tests.py`` because offers are their own feature with their
own invariants; the factory mixin is shared rather than duplicated.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import AvailabilityStatus, WorkerProfile
from core.state_machine import JobState

from .models import Application, Job, JobType, Offer, OfferStatus
from decimal import Decimal

from .forms import OfferForm
from .tests import JobFactoryMixin, make_user


class OfferTests(JobFactoryMixin, TestCase):
    def setUp(self):
        self.other_worker = WorkerProfile.objects.create(
            user=make_user("nosy@example.com"), region=self.region
        )

    def offer_payload(self, **overrides):
        return {
            "trade": self.carpentry.pk,
            "region": self.region.pk,
            "title": "Second fix, Tuesday",
            "description": "Hanging doors on the first floor.",
            "location": "North side",
            "gig_dates": (timezone.localdate() + timedelta(days=4)).isoformat(),
            "gig_hours": "8",
            "fixed_pay": "240",
            "note": "You did the framing on this one - same site.",
        } | overrides

    def send_offer(self, **overrides):
        self.client.force_login(self.client_user)
        response = self.client.post(
            reverse("jobs:offer", args=[self.worker_profile.pk]),
            self.offer_payload(**overrides),
        )
        return response, Offer.objects.order_by("-created_at").first()

    # -- creating ----------------------------------------------------------

    def test_offer_creates_a_private_job_and_a_pending_offer(self):
        response, offer = self.send_offer()

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(offer)
        self.assertTrue(offer.job.is_private)
        self.assertEqual(offer.job.state, JobState.POSTED)
        self.assertEqual(offer.job.job_type, JobType.GIG)
        self.assertEqual(offer.worker, self.worker_profile)
        self.assertEqual(offer.status, OfferStatus.PENDING)
        self.assertIsNone(offer.job.assigned_worker)

    def test_a_private_offer_never_reaches_the_public_board(self):
        _, offer = self.send_offer()
        public = self.gig(title="Ordinary listing")

        self.assertNotIn(offer.job, Job.objects.public())
        self.assertIn(public, Job.objects.public())

        # And not on the rendered board either, signed out.
        self.client.logout()
        board = self.client.get(reverse("jobs:list"))
        self.assertNotContains(board, "Second fix, Tuesday")
        self.assertContains(board, "Ordinary listing")

    def test_a_stranger_cannot_open_the_offered_job(self):
        _, offer = self.send_offer()
        url = reverse("jobs:detail", args=[offer.job.pk])

        self.client.force_login(self.other_worker.user)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_offered_worker_and_the_client_can_open_it(self):
        _, offer = self.send_offer()
        url = reverse("jobs:detail", args=[offer.job.pk])

        for user in (self.worker_user, self.client_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_covering_note_opens_a_thread(self):
        from messaging.models import Conversation

        _, offer = self.send_offer()
        conversation = Conversation.objects.get(
            job=offer.job, worker=self.worker_profile
        )
        self.assertEqual(
            [m.body for m in conversation.messages.all()],
            ["You did the framing on this one - same site."],
        )

    def test_offer_to_a_worker_not_taking_work_is_refused(self):
        self.worker_profile.availability_status = AvailabilityStatus.UNAVAILABLE
        self.worker_profile.save(update_fields=["availability_status"])

        self.client.force_login(self.client_user)
        response = self.client.post(
            reverse("jobs:offer", args=[self.worker_profile.pk]), self.offer_payload()
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Offer.objects.exists())

    # -- answering ---------------------------------------------------------

    def test_accepting_assigns_the_worker_and_closes_the_job(self):
        _, offer = self.send_offer()

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offer.pk]),
            {"answer": "accept", "response_note": "See you at 7."},
        )

        offer.refresh_from_db()
        offer.job.refresh_from_db()
        self.assertEqual(offer.status, OfferStatus.ACCEPTED)
        self.assertEqual(offer.job.state, JobState.ACCEPTED)
        self.assertEqual(offer.job.assigned_worker, self.worker_profile)
        self.assertIsNotNone(offer.job.filled_at)
        self.assertEqual(offer.response_note, "See you at 7.")

    def test_declining_leaves_the_job_open_for_the_client_to_reuse(self):
        _, offer = self.send_offer()

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offer.pk]), {"answer": "decline"}
        )

        offer.refresh_from_db()
        offer.job.refresh_from_db()
        self.assertEqual(offer.status, OfferStatus.DECLINED)
        # Not cancelled: the client can still offer it to somebody else or put
        # it on the board rather than retyping the whole gig.
        self.assertEqual(offer.job.state, JobState.POSTED)
        self.assertIsNone(offer.job.assigned_worker)

    def test_a_worker_cannot_answer_somebody_elses_offer(self):
        _, offer = self.send_offer()

        self.client.force_login(self.other_worker.user)
        response = self.client.post(
            reverse("jobs:offer_respond", args=[offer.pk]), {"answer": "accept"}
        )

        self.assertEqual(response.status_code, 404)
        offer.job.refresh_from_db()
        self.assertEqual(offer.job.state, JobState.POSTED)
        self.assertIsNone(offer.job.assigned_worker)

    def test_answering_twice_does_not_move_the_job_again(self):
        _, offer = self.send_offer()
        url = reverse("jobs:offer_respond", args=[offer.pk])

        self.client.force_login(self.worker_user)
        self.client.post(url, {"answer": "decline"})
        self.client.post(url, {"answer": "accept"})

        offer.refresh_from_db()
        offer.job.refresh_from_db()
        self.assertEqual(offer.status, OfferStatus.DECLINED)
        self.assertEqual(offer.job.state, JobState.POSTED)

    def test_accepting_a_withdrawn_offer_is_refused(self):
        _, offer = self.send_offer()

        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:offer_withdraw", args=[offer.pk]))

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offer.pk]), {"answer": "accept"}
        )

        offer.refresh_from_db()
        offer.job.refresh_from_db()
        self.assertEqual(offer.status, OfferStatus.WITHDRAWN)
        self.assertIsNone(offer.job.assigned_worker)

    # -- invariants --------------------------------------------------------

    def test_two_workers_cannot_hold_a_live_offer_for_the_same_gig(self):
        _, offer = self.send_offer()

        with self.assertRaises(IntegrityError), transaction.atomic():
            Offer.objects.create(job=offer.job, worker=self.other_worker)

    def test_re_offering_the_same_gig_after_a_decline_is_allowed(self):
        _, offer = self.send_offer()
        offer.status = OfferStatus.DECLINED
        offer.save(update_fields=["status"])

        second = Offer.objects.create(job=offer.job, worker=self.other_worker)
        self.assertEqual(second.status, OfferStatus.PENDING)

    def test_nobody_can_apply_to_a_private_gig(self):
        _, offer = self.send_offer()
        url = reverse("jobs:apply", args=[offer.job.pk])

        self.client.force_login(self.worker_user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"message": "me please"}).status_code, 404)
        self.assertFalse(Application.objects.filter(job=offer.job).exists())

    def test_publishing_a_declined_offer_puts_it_on_the_board(self):
        _, offer = self.send_offer()
        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offer.pk]), {"answer": "decline"}
        )

        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:offer_publish", args=[offer.job.pk]))

        offer.job.refresh_from_db()
        self.assertFalse(offer.job.is_private)
        self.assertIn(offer.job, Job.objects.public())

    def test_publishing_is_refused_while_an_offer_is_outstanding(self):
        _, offer = self.send_offer()

        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:offer_publish", args=[offer.job.pk]))

        offer.job.refresh_from_db()
        self.assertTrue(offer.job.is_private)

    def test_a_private_gig_is_not_a_direct_message_target(self):
        """It has its own thread already, and it is written for one person."""
        _, offer = self.send_offer()

        self.client.force_login(self.client_user)
        response = self.client.get(
            reverse("messaging:start_direct", args=[self.other_worker.pk])
        )
        self.assertNotContains(response, "Second fix, Tuesday")

    def test_a_private_gig_is_not_counted_as_public_hiring(self):
        _, offer = self.send_offer()

        self.assertFalse(self.client_profile.is_hiring)
        self.assertNotIn(offer.job, self.client_profile.open_jobs)


class MultiDayOfferTests(JobFactoryMixin, TestCase):
    """One offer form, several days.

    Each day becomes its own gig on purpose: a gig is one dated shift with its
    own escrow and its own sign-off, so two days cannot share a row when either
    can be finished, disputed or called off while the other runs normally.
    """

    def days(self, count):
        start = timezone.localdate() + timedelta(days=3)
        return [start + timedelta(days=n) for n in range(count)]

    def payload(self, dates, **overrides):
        return {
            "trade": self.carpentry.pk,
            "region": self.region.pk,
            "title": "Second fix",
            "description": "Hanging doors on the first floor.",
            "location": "North side",
            "gig_dates": ", ".join(d.isoformat() for d in dates),
            "gig_hours": "8",
            "fixed_pay": "240",
            "note": "Same site as the framing.",
        } | overrides

    def send(self, dates, **overrides):
        self.client.force_login(self.client_user)
        return self.client.post(
            reverse("jobs:offer", args=[self.worker_profile.pk]),
            self.payload(dates, **overrides),
        )

    def test_three_days_make_three_gigs_and_three_offers(self):
        dates = self.days(3)
        self.send(dates)

        jobs = Job.objects.filter(is_private=True).order_by("gig_date")
        self.assertEqual(jobs.count(), 3)
        self.assertEqual([j.gig_date for j in jobs], dates)
        self.assertEqual(Offer.objects.count(), 3)

    def test_every_day_carries_the_same_terms(self):
        self.send(self.days(3))
        for job in Job.objects.filter(is_private=True):
            self.assertEqual(job.title, "Second fix")
            self.assertEqual(job.fixed_pay, Decimal("240"))
            self.assertEqual(job.gig_hours, Decimal("8"))
            self.assertEqual(job.trade, self.carpentry)

    def test_each_day_gets_its_own_thread(self):
        """The offer is answered per day, so the questions are too."""
        from messaging.models import Conversation

        self.send(self.days(2))
        self.assertEqual(Conversation.objects.count(), 2)

    def test_one_day_still_lands_on_that_job(self):
        """The single-date path is the common one and must not have changed."""
        response = self.send(self.days(1))
        job = Job.objects.get(is_private=True)
        self.assertRedirects(
            response, reverse("jobs:detail", args=[job.pk]), fetch_redirect_response=False
        )

    def test_several_days_land_on_the_list_rather_than_one_of_them(self):
        response = self.send(self.days(3))
        self.assertRedirects(
            response, reverse("jobs:mine"), fetch_redirect_response=False
        )

    def test_the_worker_can_take_one_day_and_leave_the_rest(self):
        """The reason these are separate rows at all."""
        self.send(self.days(3))
        offers = list(Offer.objects.order_by("job__gig_date"))

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offers[0].pk]), {"answer": "accept"}
        )

        offers[0].refresh_from_db()
        offers[1].refresh_from_db()
        self.assertEqual(offers[0].status, OfferStatus.ACCEPTED)
        self.assertTrue(offers[1].is_pending)

    def test_a_duplicate_day_is_not_booked_twice(self):
        dates = self.days(2)
        self.send([dates[0], dates[1], dates[0]])
        self.assertEqual(Job.objects.filter(is_private=True).count(), 2)

    def test_no_days_is_refused(self):
        response = self.send([])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.filter(is_private=True).count(), 0)
        self.assertContains(response, "Pick at least one day")

    def test_a_past_day_is_refused_rather_than_dropped(self):
        """Silently skipping it would send an offer the client never wrote."""
        yesterday = timezone.localdate() - timedelta(days=1)
        response = self.send([yesterday, *self.days(1)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.filter(is_private=True).count(), 0)
        self.assertContains(response, "in the past")

    def test_too_many_days_is_refused_whole(self):
        response = self.send(self.days(OfferForm.MAX_DAYS + 1))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.filter(is_private=True).count(), 0)

    def test_nothing_is_written_when_one_day_is_bad(self):
        """All in one transaction: three good days and one bad writes none."""
        bad = [*self.days(3), timezone.localdate() - timedelta(days=2)]
        self.send(bad)
        self.assertEqual(Job.objects.filter(is_private=True).count(), 0)
        self.assertEqual(Offer.objects.count(), 0)
