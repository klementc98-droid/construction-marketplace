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
from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from accounts.models import AvailabilityStatus, WorkerProfile
from core.state_machine import JobState

from .models import (
    Application,
    ApplicationStatus,
    Job,
    JobType,
    Offer,
    OfferStatus,
    Party,
)
from decimal import Decimal

from .forms import OfferForm
from .models import Counter, CounterStatus
from accounts.models import ClientProfile

from .views.offers import _offerable_jobs
from .tests import JobFactoryMixin, make_user


class OfferTests(JobFactoryMixin, TestCase):
    def setUp(self):
        self.other_worker = WorkerProfile.objects.create(
            user=make_user("nosy@example.com"), region=self.region
        )

    def offer_payload(self, **overrides):
        return {
            "trade": self.carpentry.pk,
            "experience_wanted": "none",
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


class OfferAnExistingJobTests(JobFactoryMixin, TestCase):
    """Sending somebody a post you already have, rather than retyping it.

    The point of the feature is that nothing is copied: a retyped gig is a
    second job meaning the same work, and the two drift the moment either is
    edited. So the cases here are mostly about what does *not* happen.
    """

    def url(self):
        return reverse("jobs:offer", args=[self.worker_profile.pk])

    def send(self, job, note="Yours if you want it."):
        self.client.force_login(self.client_user)
        return self.client.post(
            self.url(), {"pick": "1", "job": job.pk, "note": note}
        )

    def test_the_list_opens_when_the_client_already_has_work_up(self):
        job = self.gig(title="Framing, Thursday")
        self.client.force_login(self.client_user)
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "jobs/offer_choose.html")
        self.assertContains(response, "Framing, Thursday")
        self.assertIn(job, response.context["bookings"])

    def test_a_client_with_nothing_posted_goes_straight_to_the_form(self):
        """No list to show, so showing one would be a step for its own sake."""
        self.client.force_login(self.client_user)
        response = self.client.get(self.url())

        self.assertTemplateUsed(response, "jobs/offer_form.html")
        self.assertFalse(response.context["has_open_jobs"])

    def test_new_is_how_you_leave_the_list(self):
        self.gig()
        self.client.force_login(self.client_user)
        response = self.client.get(self.url() + "?new=1")

        self.assertTemplateUsed(response, "jobs/offer_form.html")
        self.assertTrue(response.context["has_open_jobs"])

    def test_offering_it_writes_an_offer_and_not_a_second_job(self):
        job = self.gig(title="Framing, Thursday")
        before = Job.objects.count()

        response = self.send(job)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Job.objects.count(), before)
        offer = Offer.objects.get()
        self.assertEqual(offer.job, job)
        self.assertEqual(offer.worker, self.worker_profile)
        self.assertEqual(offer.status, OfferStatus.PENDING)

    def test_the_post_stays_on_the_public_board(self):
        """An invitation, not a withdrawal. Other people can still apply, and
        whoever is confirmed, the rest get a definite answer."""
        job = self.gig()
        self.send(job)

        job.refresh_from_db()
        self.assertFalse(job.is_private)
        self.assertIn(job, Job.objects.public())

    def test_the_note_arrives_as_the_first_message(self):
        from messaging.models import Conversation

        job = self.gig()
        self.send(job, note="Same site as the framing.")

        conversation = Conversation.objects.get(job=job, worker=self.worker_profile)
        self.assertEqual(
            [m.body for m in conversation.messages.all()],
            ["Same site as the framing."],
        )

    def test_a_job_already_out_with_somebody_is_not_offered_again(self):
        """one_pending_offer_per_job: two people cannot both hold an answer."""
        job = self.gig(title="Spoken for")
        rival = WorkerProfile.objects.create(
            user=make_user("rival-offer@example.com"), region=self.region
        )
        Offer.objects.create(job=job, worker=rival)

        self.client.force_login(self.client_user)
        response = self.client.get(self.url())

        self.assertTemplateUsed(response, "jobs/offer_form.html")
        self.assertNotIn(job, _offerable_jobs(self.client_profile))

    def test_somebody_elses_job_cannot_be_offered_by_pk(self):
        """The list is the whitelist — validation can only match what is in it."""
        stranger = ClientProfile.objects.create(
            user=make_user("stranger@example.com"), region=self.region
        )
        theirs = self.gig(client=stranger, title="Not yours")
        mine = self.gig(title="Mine")

        response = self.send(theirs)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Offer.objects.filter(job=theirs).exists())
        self.assertIn("job", response.context["form"].errors)
        self.assertIsNotNone(mine)

    def test_a_booking_is_offered_whole(self):
        """Sent Tuesday and left wondering about Wednesday is the confusion
        _booking_days exists to prevent."""
        from uuid import uuid4

        group = uuid4()
        days = [
            self.gig(
                title="Three days",
                gig_date=timezone.localdate() + timedelta(days=n),
                offer_group=group,
            )
            for n in (3, 4, 5)
        ]

        self.send(days[0])

        self.assertEqual(Offer.objects.count(), 3)
        self.assertEqual(
            sorted(o.job_id for o in Offer.objects.all()),
            sorted(d.pk for d in days),
        )

    def test_asking_again_after_a_no_reuses_the_offer(self):
        job = self.gig()
        Offer.objects.create(
            job=job, worker=self.worker_profile, status=OfferStatus.DECLINED
        )

        self.send(job)

        offer = Offer.objects.get(job=job, worker=self.worker_profile)
        self.assertEqual(offer.status, OfferStatus.PENDING)
        self.assertIsNone(offer.responded_at)

    def test_a_standing_position_is_not_offered_from_here(self):
        """An offer is a dated shift with a price on it. A position is neither,
        and inviting somebody into one is a different conversation."""
        self.standing()
        self.client.force_login(self.client_user)
        response = self.client.get(self.url())

        self.assertTemplateUsed(response, "jobs/offer_form.html")


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
            "experience_wanted": "none",
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

    def test_accepting_takes_the_whole_booking(self):
        """Yes to a week is yes to the week.

        The days are separate rows because each one carries its own escrow and
        its own sign-off — not so that they can be answered separately. The
        reader is shown one offer (see collapse_rows), answers it once, and the
        answer has to reach every day of it. Leaving the rest pending left the
        client holding live offers nobody would ever answer and days still on
        the board, which is the thing a "yes" was supposed to end.
        """
        self.send(self.days(3))
        offers = list(Offer.objects.order_by("job__gig_date"))

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offers[0].pk]), {"answer": "accept"}
        )

        for offer in offers:
            offer.refresh_from_db()
            self.assertEqual(offer.status, OfferStatus.ACCEPTED)
            self.assertEqual(offer.job.state, JobState.ACCEPTED)
            self.assertEqual(offer.job.assigned_worker, self.worker_profile)

    def test_declining_answers_the_whole_booking(self):
        """No is one answer too.

        They were shown one offer and said no to it once. Leaving the other
        days pending had the app going on telling them a job was waiting after
        they had turned it down, and the client going on waiting for an answer
        that had already been given.
        """
        self.send(self.days(3))
        offers = list(Offer.objects.order_by("job__gig_date"))

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offers[0].pk]),
            {"answer": "decline", "response_note": "Booked that week."},
        )

        for offer in offers:
            offer.refresh_from_db()
            self.assertEqual(offer.status, OfferStatus.DECLINED)
            self.assertEqual(offer.response_note, "Booked that week.")

    def test_a_declined_booking_stops_asking_to_be_answered(self):
        """The badge counts pending offers, so one left behind kept saying a
        job was waiting for somebody who had already said no."""
        from .waiting import waiting_for

        self.send(self.days(3))
        offer = Offer.objects.order_by("job__gig_date").first()

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offer.pk]), {"answer": "decline"}
        )

        self.assertEqual(waiting_for(self.worker_user).offers, 0)

    def test_a_booking_leaves_the_board_once_it_is_agreed(self):
        """Two people agreed; nobody else should still be able to apply."""
        group = __import__("uuid").uuid4()
        days = [
            self.gig(
                is_private=False,
                offer_group=group,
                gig_date=timezone.localdate() + timedelta(days=n),
            )
            for n in (3, 4, 5)
        ]
        offers = [
            Offer.objects.create(job=d, worker=self.worker_profile) for d in days
        ]

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offers[0].pk]), {"answer": "accept"}
        )

        self.assertEqual(Job.objects.public().filter(offer_group=group).count(), 0)

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


class DoubleBookingTests(JobFactoryMixin, TestCase):
    """One worker, one of each day.

    Nothing used to say so. A booking was sealed a day at a time and no step
    asked whether the day was already spoken for, so the same person could be
    confirmed for the same week twice by two clients who each believed they had
    them — and the first either would learn of it is a morning nobody turns up
    to.
    """

    def days(self, count, *, start_in=3):
        start = timezone.localdate() + timedelta(days=start_in)
        return [start + timedelta(days=n) for n in range(count)]

    def booked(self, dates, *, worker=None):
        """A booking already sealed on these dates."""
        import uuid

        group = uuid.uuid4()
        return [
            self.gig(
                offer_group=group,
                gig_date=day,
                state=JobState.ACCEPTED,
                assigned_worker=worker or self.worker_profile,
            )
            for day in dates
        ]

    def offer_on(self, dates):
        """A pending offer to our worker covering these dates."""
        import uuid

        group = uuid.uuid4()
        jobs = [
            self.gig(offer_group=group, gig_date=day, is_private=True)
            for day in dates
        ]
        offers = [
            Offer.objects.create(job=job, worker=self.worker_profile)
            for job in jobs
        ]
        return jobs, offers

    # -- the worker's own diary -------------------------------------------

    def test_a_worker_cannot_accept_an_offer_over_days_they_have_sold(self):
        dates = self.days(3)
        self.booked(dates[:2])
        _, offers = self.offer_on(dates)

        self.client.force_login(self.worker_user)
        response = self.client.post(
            reverse("jobs:offer_respond", args=[offers[0].pk]), {"answer": "accept"}
        )

        # Nothing half-done: the days that did not clash are not taken either,
        # because a booking is answered whole or not at all.
        for offer in offers:
            offer.refresh_from_db()
            offer.job.refresh_from_db()
            self.assertEqual(offer.status, OfferStatus.PENDING)
            self.assertEqual(offer.job.state, JobState.POSTED)
            self.assertIsNone(offer.job.assigned_worker)
        self.assertEqual(response.status_code, 302)

    def test_the_refusal_names_the_days(self):
        """"Already booked" without the dates leaves them hunting."""
        dates = self.days(2)
        self.booked(dates)
        _, offers = self.offer_on(dates)

        self.client.force_login(self.worker_user)
        response = self.client.post(
            reverse("jobs:offer_respond", args=[offers[0].pk]),
            {"answer": "accept"},
            follow=True,
        )

        body = response.content.decode()
        self.assertIn("already booked", body.lower())
        for day in dates:
            self.assertIn(date_format(day, "j M"), body)

    def test_an_offer_that_does_not_clash_is_still_accepted(self):
        """The rule must not be a blanket "no" to anybody with work on."""
        self.booked(self.days(2))
        free = self.days(2, start_in=30)
        _, offers = self.offer_on(free)

        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:offer_respond", args=[offers[0].pk]), {"answer": "accept"}
        )

        for offer in offers:
            offer.job.refresh_from_db()
            self.assertEqual(offer.job.state, JobState.ACCEPTED)

    # -- the client's side -------------------------------------------------

    def test_a_client_cannot_confirm_an_applicant_who_is_already_booked(self):
        dates = self.days(2)
        self.booked(dates)

        import uuid

        group = uuid.uuid4()
        posted = [
            self.gig(offer_group=group, gig_date=day) for day in dates
        ]
        application = Application.objects.create(
            job=posted[0], worker=self.worker_profile
        )

        self.client.force_login(self.client_user)
        response = self.client.post(
            reverse("jobs:application_select", args=[application.pk]), follow=True
        )

        for job in posted:
            job.refresh_from_db()
            self.assertEqual(job.state, JobState.POSTED)
            self.assertIsNone(job.assigned_worker)
        self.assertIn("already booked", response.content.decode().lower())

    # -- and the guarantee underneath --------------------------------------

    def test_the_database_refuses_two_live_jobs_on_one_day(self):
        """The view check explains; this is what makes it true.

        Two clients can confirm the same worker in the same second and neither
        view sees the other.
        """
        day = self.days(1)[0]
        self.booked([day])

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.gig(
                    gig_date=day,
                    state=JobState.ACCEPTED,
                    assigned_worker=self.worker_profile,
                )

    def test_history_may_hold_the_overlaps_it_now_prevents(self):
        """The index covers live states only.

        A day that has been paid out or closed is history — and this app has
        real history containing exactly the overlap this now stops.
        """
        day = self.days(1)[0]
        self.gig(gig_date=day, state=JobState.CLOSED, assigned_worker=self.worker_profile)
        self.gig(gig_date=day, state=JobState.PAID_OUT, assigned_worker=self.worker_profile)

        self.assertEqual(
            Job.objects.filter(
                assigned_worker=self.worker_profile, gig_date=day
            ).count(),
            2,
        )


class BookedJobIsPrivateTests(JobFactoryMixin, TestCase):
    """A taken job is an arrangement, not an advertisement.

    It stayed world-readable at its own URL after somebody got it, so anybody
    holding the link could read a booking they had nothing to do with — the
    worker's name, the price, the days and the site. Nobody browses to it: it
    is off the board and out of search the moment it is taken. It leaks by
    being pasted, which is the case a state-blind check cannot catch.
    """

    def setUp(self):
        self.job = self.gig(
            state=JobState.ACCEPTED, assigned_worker=self.worker_profile
        )
        self.stranger = make_user("stranger@example.com")
        self.stranger_worker = WorkerProfile.objects.create(
            user=self.stranger, region=self.region
        )

    def open_it(self):
        return self.client.get(reverse("jobs:detail", args=[self.job.pk]))

    def test_a_stranger_with_the_link_gets_nothing(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.open_it().status_code, 404)

    def test_a_signed_out_visitor_gets_nothing_either(self):
        self.assertEqual(self.open_it().status_code, 404)

    def test_the_client_still_sees_their_own_job(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self.open_it().status_code, 200)

    def test_the_worker_doing_it_still_sees_it(self):
        self.client.force_login(self.worker_user)
        self.assertEqual(self.open_it().status_code, 200)

    def test_somebody_who_applied_keeps_their_own_history(self):
        """Their list links to it; a 404 from your own list is not privacy."""
        Application.objects.create(job=self.job, worker=self.stranger_worker)
        self.client.force_login(self.stranger)
        self.assertEqual(self.open_it().status_code, 200)

    def name_the_worker(self):
        """A name nothing else on the page could contain.

        str(user) falls back to the email's local part, and "worker" appears
        nine times in an unrelated page — the assertion has to be about the
        person, not about a common word.
        """
        self.worker_user.full_name = "Aristotelis Vlachopoulos"
        self.worker_user.save(update_fields=["full_name"])
        return self.worker_user.full_name

    def test_but_they_do_not_learn_who_got_it(self):
        name = self.name_the_worker()
        Application.objects.create(job=self.job, worker=self.stranger_worker)
        self.client.force_login(self.stranger)
        self.assertNotContains(self.open_it(), name)

    def test_the_pair_do_see_who_it_went_to(self):
        name = self.name_the_worker()
        self.client.force_login(self.client_user)
        self.assertContains(self.open_it(), name)

    def test_an_open_post_is_still_public(self):
        """The board is browsable signed out, and that is the point."""
        open_job = self.gig()
        self.assertEqual(
            self.client.get(reverse("jobs:detail", args=[open_job.pk])).status_code,
            200,
        )

    def test_a_dead_post_with_nobody_on_it_stays_readable(self):
        """Expired or called off with no worker is a dead advertisement, not
        an arrangement — it was public while it stood and holds nothing to
        protect, so an old link to one still opens."""
        dead = self.gig(state=JobState.EXPIRED)
        self.assertEqual(
            self.client.get(reverse("jobs:detail", args=[dead.pk])).status_code, 200
        )


class OfferPageLinksTests(JobFactoryMixin, TestCase):
    """The links on an offer a worker is looking at have to point at the job."""

    def test_ask_for_different_terms_points_at_the_job(self):
        """It pointed at the offer's id, which is a different number entirely.

        The URL took a job pk, the template handed it the offer's, and the two
        are unrelated sequences — so the button led to whatever job happened to
        share that id, or, more often, to a 404.
        """
        # Push the two sequences apart first: in a fresh database the first
        # job and the first offer are both id 1, and a test that passes on that
        # coincidence would have passed on the bug too.
        for _ in range(3):
            self.gig(is_private=True)

        job = self.gig(is_private=True)
        offer = Offer.objects.create(job=job, worker=self.worker_profile)
        self.assertNotEqual(offer.pk, job.pk)

        self.client.force_login(self.worker_user)
        page = self.client.get(reverse("jobs:detail", args=[job.pk]))

        self.assertContains(page, reverse("jobs:counter", args=[job.pk]))
        self.assertNotContains(page, reverse("jobs:counter", args=[offer.pk]))

    def test_and_the_link_actually_opens(self):
        job = self.gig(is_private=True)
        Offer.objects.create(job=job, worker=self.worker_profile)

        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("jobs:counter", args=[job.pk]))
        self.assertEqual(response.status_code, 200)


class LateWritesLeaveNoResidueTests(JobFactoryMixin, TestCase):
    """Writes that cannot win the job, but used to leave something behind.

    None of these can take a job from whoever won it — _seal's conditional
    UPDATE is the only thing that decides that. What they could do is leave a
    counter, or a published board post, attached to work already spoken for.
    """

    def setUp(self):
        self.job = self.gig(is_private=True)

    @contextmanager
    def taken_behind_the_form(self):
        """The database moves; the view keeps the read it started with.

        A patch names the module doing the lookup, and the two views under
        test do it in different ones — countering in jobs.views.negotiation,
        publishing in jobs.views.offers. Both are held open rather than
        pretending one covers the other.
        """
        stale = Job.objects.get(pk=self.job.pk)
        Job.objects.filter(pk=self.job.pk).update(
            state=JobState.ACCEPTED, assigned_worker=self.worker_profile
        )
        with patch(
            "jobs.views.negotiation.get_object_or_404", return_value=stale
        ), patch("jobs.views.offers.get_object_or_404", return_value=stale):
            yield

    def test_a_counter_cannot_be_written_onto_a_job_just_taken(self):
        Offer.objects.create(job=self.job, worker=self.worker_profile)
        self.client.force_login(self.worker_user)

        with self.taken_behind_the_form():
            self.client.post(
                reverse("jobs:counter", args=[self.job.pk]),
                {
                    "fixed_pay": "150",
                    "gig_hours": "8",
                    "gig_date": self.job.gig_date.isoformat(),
                    "note": "",
                },
            )

        self.assertFalse(Counter.objects.filter(job=self.job).exists())

    def test_an_accepted_job_cannot_be_published_to_the_board(self):
        """It would advertise work that is already somebody's."""
        self.client.force_login(self.client_user)

        with self.taken_behind_the_form():
            self.client.post(reverse("jobs:offer_publish", args=[self.job.pk]))

        self.job.refresh_from_db()
        self.assertTrue(self.job.is_private)

    def test_publishing_an_untouched_offer_still_works(self):
        self.client.force_login(self.client_user)

        self.client.post(reverse("jobs:offer_publish", args=[self.job.pk]))

        self.job.refresh_from_db()
        self.assertFalse(self.job.is_private)


class StatusWritesAreClaimedTests(JobFactoryMixin, TestCase):
    """Every write that moves an Application, Offer or Counter names its old
    state and claims it.

    The money paths learned this; these did not. They were the ordinary CRUD
    endpoints — read the row, write the new status — and each of them could
    overwrite an answer that had already been given.
    """

    def setUp(self):
        self.job = self.gig()

    # -- applications ------------------------------------------------------

    def test_a_selected_applicant_cannot_withdraw_the_application(self):
        """It had no state check at all, so somebody just picked could withdraw
        the application that got them the job — leaving the job saying they
        accepted and the application saying they walked away."""
        application = Application.objects.create(
            job=self.job, worker=self.worker_profile,
            status=ApplicationStatus.SELECTED,
        )

        self.client.force_login(self.worker_user)
        self.client.post(reverse("jobs:application_withdraw", args=[application.pk]))

        application.refresh_from_db()
        self.assertEqual(application.status, ApplicationStatus.SELECTED)

    def test_an_ordinary_withdrawal_still_works(self):
        application = Application.objects.create(
            job=self.job, worker=self.worker_profile
        )

        self.client.force_login(self.worker_user)
        self.client.post(reverse("jobs:application_withdraw", args=[application.pk]))

        application.refresh_from_db()
        self.assertEqual(application.status, ApplicationStatus.WITHDRAWN)

    def test_applying_to_a_job_confirmed_meanwhile_writes_nothing(self):
        """An APPLIED row on a taken job is an application nobody will ever
        answer, sitting in that worker's list looking live."""
        self.client.force_login(self.worker_user)

        stale = Job.objects.get(pk=self.job.pk)
        Job.objects.filter(pk=self.job.pk).update(state=JobState.ACCEPTED)

        with patch("jobs.views.applications.get_object_or_404", return_value=stale):
            self.client.post(
                reverse("jobs:apply", args=[self.job.pk]), {"message": "me please"}
            )

        self.assertFalse(Application.objects.filter(job=self.job).exists())

    # -- offers ------------------------------------------------------------

    def test_an_accepted_offer_cannot_be_withdrawn_underneath_the_worker(self):
        offer = Offer.objects.create(
            job=self.job, worker=self.worker_profile, status=OfferStatus.ACCEPTED
        )

        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:offer_withdraw", args=[offer.pk]))

        offer.refresh_from_db()
        self.assertEqual(offer.status, OfferStatus.ACCEPTED)

    def test_a_pending_offer_can_still_be_withdrawn(self):
        offer = Offer.objects.create(job=self.job, worker=self.worker_profile)

        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:offer_withdraw", args=[offer.pk]))

        offer.refresh_from_db()
        self.assertEqual(offer.status, OfferStatus.WITHDRAWN)

    # -- counters ----------------------------------------------------------

    def test_an_answered_counter_cannot_be_declined_a_second_time(self):
        counter = Counter.objects.create(
            job=self.job,
            worker=self.worker_profile,
            proposed_by=Party.WORKER,
            fixed_pay=Decimal("120"),
            status=CounterStatus.ACCEPTED,
        )

        self.client.force_login(self.client_user)
        self.client.post(
            reverse("jobs:counter_respond", args=[counter.pk]), {"answer": "decline"}
        )

        counter.refresh_from_db()
        self.assertEqual(counter.status, CounterStatus.ACCEPTED)


class StaleEditTests(JobFactoryMixin, TestCase):
    """Editing and cancelling had to claim what they write, like everything else.

    Both read the state, checked it, and wrote — with the whole of a form being
    filled in sitting in the gap. _seal exists because a worker can accept
    during exactly that gap, and these two were the endpoints that had not been
    told.

    Staged the way the other races here are: the request is holding a read that
    the database has already moved past.
    """

    def setUp(self):
        self.job = self.gig()

    def edit_payload(self, **overrides):
        return {
            "trade": self.job.trade.pk,
            "experience_wanted": "none",
            "region": self.job.region.pk,
            "title": self.job.title,
            "description": self.job.description,
            "gig_dates": self.job.gig_date.isoformat(),
            "gig_hours": "6",
            "fixed_pay": "70",
            "use_escrow": "False",
        } | overrides

    def accept_behind_the_form(self):
        """Somebody says yes while the form is open, and the view never sees it.

        The database moves; the request keeps the read it started with. Patching
        the lookup is what makes that reproducible — without it the view reads
        the *new* state and takes an earlier branch, which tests the guard that
        was already there rather than the one being added.
        """
        stale = Job.objects.get(pk=self.job.pk)
        Job.objects.filter(pk=self.job.pk).update(
            state=JobState.ACCEPTED, assigned_worker=self.worker_profile
        )
        return patch("jobs.views.posting.get_object_or_404", return_value=stale)

    def test_a_stale_edit_cannot_change_the_terms_of_an_accepted_job(self):
        """The money case: €100 agreed, €70 written over it afterwards."""
        self.client.force_login(self.client_user)

        with self.accept_behind_the_form():
            response = self.client.post(
                reverse("jobs:edit", args=[self.job.pk]), self.edit_payload()
            )

        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("90"))
        self.assertEqual(self.job.gig_hours, Decimal("8"))
        self.assertEqual(response.status_code, 302)

    def test_and_cannot_erase_the_acceptance_itself(self):
        """job.save() wrote the whole row from a read taken before the accept,
        so state and assigned_worker travelled with the price."""
        self.client.force_login(self.client_user)

        with self.accept_behind_the_form():
            self.client.post(
                reverse("jobs:edit", args=[self.job.pk]), self.edit_payload()
            )

        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.ACCEPTED)
        self.assertEqual(self.job.assigned_worker, self.worker_profile)

    def test_the_client_is_told_rather_than_left_guessing(self):
        self.client.force_login(self.client_user)

        with self.accept_behind_the_form():
            response = self.client.post(
                reverse("jobs:edit", args=[self.job.pk]),
                self.edit_payload(),
                follow=True,
            )

        self.assertContains(response, "answered this job while you were editing")

    def test_an_ordinary_edit_still_goes_through(self):
        self.client.force_login(self.client_user)

        self.client.post(
            reverse("jobs:edit", args=[self.job.pk]), self.edit_payload()
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("70"))
        self.assertEqual(self.job.gig_hours, Decimal("6"))

    def test_cancelling_cannot_land_on_a_job_just_accepted(self):
        """It used to write CANCELLED over an acceptance, leaving a cancelled
        job with a worker assigned to it."""
        self.client.force_login(self.client_user)

        with self.accept_behind_the_form():
            self.client.post(reverse("jobs:cancel", args=[self.job.pk]))

        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.ACCEPTED)
        self.assertEqual(self.job.assigned_worker, self.worker_profile)

    def test_an_ordinary_cancel_still_works(self):
        self.client.force_login(self.client_user)

        self.client.post(reverse("jobs:cancel", args=[self.job.pk]))

        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.CANCELLED)


class EditingADayOfABookingTests(JobFactoryMixin, TestCase):
    """A booking cannot be edited into holding the same day twice.

    It could, and the consequence was the one that gets reported rather than
    noticed: a three-day booking edited to 20th, 21st, 20th is two rows for one
    day. The worker accepts, two days seal, the duplicate cannot — and the
    client is looking at a job somebody accepted that is still open.
    """

    def booking(self, dates):
        import uuid

        group = uuid.uuid4()
        return [
            self.gig(offer_group=group, gig_date=day) for day in dates
        ]

    def edit(self, job, dates):
        self.client.force_login(self.client_user)
        return self.client.post(
            reverse("jobs:edit", args=[job.pk]),
            {
                "trade": job.trade.pk,
                "experience_wanted": "none",
                "region": job.region.pk,
                "title": job.title,
                "description": job.description,
                "gig_dates": ", ".join(d.isoformat() for d in dates),
                "gig_hours": "8",
                "fixed_pay": "90",
                "use_escrow": "False",
            },
        )

    def test_a_day_cannot_be_moved_onto_one_the_booking_already_has(self):
        start = timezone.localdate() + timedelta(days=3)
        days = self.booking([start, start + timedelta(days=1), start + timedelta(days=2)])

        response = self.edit(days[2], [start])

        self.assertEqual(response.status_code, 200)   # redisplayed, not saved
        days[2].refresh_from_db()
        self.assertEqual(days[2].gig_date, start + timedelta(days=2))
        self.assertContains(response, "already has")

    def test_a_day_can_still_be_moved_somewhere_free(self):
        start = timezone.localdate() + timedelta(days=3)
        days = self.booking([start, start + timedelta(days=1)])
        moved_to = start + timedelta(days=9)

        self.edit(days[1], [moved_to])

        days[1].refresh_from_db()
        self.assertEqual(days[1].gig_date, moved_to)

    def test_a_job_outside_a_booking_is_unaffected(self):
        job = self.gig()
        moved_to = timezone.localdate() + timedelta(days=20)

        self.edit(job, [moved_to])

        job.refresh_from_db()
        self.assertEqual(job.gig_date, moved_to)


class PaymentMethodCounterTests(JobFactoryMixin, TestCase):
    """A worker offered cash-in-hand can come back asking for escrow.

    The answer "yes, but not on trust" — which a board built around held money
    should make easy to give. And it is an answer about the arrangement, not
    about one day, so on a multi-day offer it covers all of them.
    """

    def days(self, count):
        start = timezone.localdate() + timedelta(days=3)
        return [start + timedelta(days=n) for n in range(count)]

    def send_offer(self, dates, *, escrow=False):
        self.client.force_login(self.client_user)
        return self.client.post(
            reverse("jobs:offer", args=[self.worker_profile.pk]),
            {
                "trade": self.carpentry.pk,
                "experience_wanted": "none",
                "region": self.region.pk,
                "title": "Second fix",
                "description": "Doors on the first floor.",
                "gig_dates": ", ".join(d.isoformat() for d in dates),
                "gig_hours": "8",
                "fixed_pay": "240",
                "use_escrow": str(escrow),
                "note": "",
            },
        )

    def counter_asking_for_escrow(self, job):
        self.client.force_login(self.worker_user)
        return self.client.post(
            reverse("jobs:counter", args=[job.pk]),
            {"fixed_pay": "240", "gig_hours": "8",
             "gig_date": job.gig_date.isoformat(),
             "use_escrow": "True", "note": "Rather have it held."},
        )

    def test_an_offer_can_be_written_without_escrow(self):
        self.send_offer(self.days(1))
        job = Job.objects.filter(is_private=True).get()
        self.assertFalse(job.use_escrow)
        self.assertFalse(job.is_escrowed)

    def test_the_worker_can_counter_asking_for_escrow_alone(self):
        """Nothing else moves — the price and the day are agreed already."""
        self.send_offer(self.days(1))
        job = Job.objects.filter(is_private=True).get()
        self.counter_asking_for_escrow(job)

        counter = Counter.objects.get(job=job)
        self.assertIs(counter.use_escrow, True)
        self.assertEqual(counter.fixed_pay, Decimal("240"))
        # A counter that changes nothing is refused, so this one being here at
        # all proves the payment method counts as a change.
        self.assertEqual(counter.status, CounterStatus.PENDING)

    def test_accepting_it_puts_the_job_on_escrow(self):
        self.send_offer(self.days(1))
        job = Job.objects.filter(is_private=True).get()
        self.counter_asking_for_escrow(job)
        counter = Counter.objects.get(job=job)

        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:counter_respond", args=[counter.pk]), {"answer": "accept"})

        job.refresh_from_db()
        self.assertTrue(job.use_escrow)
        self.assertTrue(job.is_escrowed)

    def test_a_multi_day_offer_shares_one_group(self):
        self.send_offer(self.days(3))
        groups = {j.offer_group for j in Job.objects.filter(is_private=True)}
        self.assertEqual(len(groups), 1)
        self.assertIsNotNone(groups.pop())

    def test_a_single_day_offer_has_no_group(self):
        """A group of one implies siblings that do not exist."""
        self.send_offer(self.days(1))
        self.assertIsNone(Job.objects.filter(is_private=True).get().offer_group)

    def test_agreeing_escrow_on_one_day_covers_every_day(self):
        """Escrow on Tuesday and cash on Wednesday is nobody's arrangement."""
        self.send_offer(self.days(3))
        jobs = list(Job.objects.filter(is_private=True).order_by("gig_date"))
        self.counter_asking_for_escrow(jobs[0])
        counter = Counter.objects.get(job=jobs[0])

        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:counter_respond", args=[counter.pk]), {"answer": "accept"})

        for job in jobs:
            job.refresh_from_db()
            self.assertTrue(job.use_escrow, f"{job.gig_date} missed the change")

    def test_the_price_stays_per_day(self):
        """Money genuinely can differ by day; the payment method cannot."""
        self.send_offer(self.days(2))
        jobs = list(Job.objects.filter(is_private=True).order_by("gig_date"))
        self.client.force_login(self.worker_user)
        self.client.post(
            reverse("jobs:counter", args=[jobs[0].pk]),
            {"fixed_pay": "300", "gig_hours": "8",
             "gig_date": jobs[0].gig_date.isoformat(),
             "use_escrow": "True", "note": ""},
        )
        counter = Counter.objects.get(job=jobs[0])
        self.client.force_login(self.client_user)
        self.client.post(reverse("jobs:counter_respond", args=[counter.pk]), {"answer": "accept"})

        jobs[0].refresh_from_db(); jobs[1].refresh_from_db()
        self.assertEqual(jobs[0].fixed_pay, Decimal("300"))
        self.assertEqual(jobs[1].fixed_pay, Decimal("240"))   # untouched
        self.assertTrue(jobs[1].use_escrow)                   # but escrow spread


class ReviewTests(JobFactoryMixin, TestCase):
    """Rating the other side, once the job is over and its day has gone."""

    def finished_job(self, *, days_ago=1):
        from core.state_machine import JobState

        job = self.gig()
        job.state = JobState.CLOSED
        job.assigned_worker = self.worker_profile
        job.gig_date = timezone.localdate() - timedelta(days=days_ago)
        job.save(update_fields=["state", "assigned_worker", "gig_date"])
        return job

    def rate(self, job, user, score=5, comment=""):
        self.client.force_login(user)
        return self.client.post(
            reverse("jobs:review", args=[job.pk]),
            {"rating": str(score), "comment": comment},
        )

    def test_both_sides_can_rate_and_the_directions_differ(self):
        from jobs.models import Review, ReviewDirection

        job = self.finished_job()
        self.rate(job, self.client_user, 5)
        self.rate(job, self.worker_user, 4)

        directions = set(Review.objects.filter(job=job).values_list("direction", flat=True))
        self.assertEqual(
            directions,
            {ReviewDirection.CLIENT_ON_WORKER, ReviewDirection.WORKER_ON_CLIENT},
        )

    def test_the_score_lands_on_the_profile_being_rated(self):
        job = self.finished_job()
        self.rate(job, self.client_user, 4)

        self.worker_profile.refresh_from_db()
        self.assertEqual(self.worker_profile.rating_count, 1)
        self.assertEqual(self.worker_profile.rating_sum, 4)
        self.assertEqual(self.worker_profile.average_rating, Decimal("4.0"))

    def test_nobody_can_rate_twice(self):
        from jobs.models import Review

        job = self.finished_job()
        self.rate(job, self.client_user, 5)
        self.rate(job, self.client_user, 1)
        self.assertEqual(Review.objects.filter(job=job).count(), 1)
        self.assertEqual(Review.objects.get(job=job).rating, 5)

    def test_a_job_still_running_cannot_be_rated(self):
        """Rating before the money moves is leverage over the payment."""
        from jobs.models import Review

        job = self.gig()
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["assigned_worker"])
        self.assertFalse(job.can_be_reviewed_by(self.client_user))
        self.rate(job, self.client_user)
        self.assertEqual(Review.objects.count(), 0)

    def test_a_job_closed_before_its_day_can_still_be_rated(self):
        """Being finished is the whole test.

        This used to also require the gig date to have passed, which sounded
        right and was wrong: a job only reaches CLOSED because both sides said
        the work happened. Asking the calendar to agree after that left people
        staring at a finished job with no way to rate it.
        """
        from jobs.models import Review

        job = self.finished_job(days_ago=-2)          # closed, day still ahead
        self.assertTrue(job.can_be_reviewed_by(self.client_user))
        self.rate(job, self.client_user)
        self.assertEqual(Review.objects.count(), 1)

    def test_a_booking_can_only_be_rated_once_however_many_days_it_runs(self):
        """The rule that was a view's habit rather than a rule.

        review_create collapsed to the first day before writing, so through the
        button one booking meant one rating. Underneath, the service checked
        the day and so did the constraint — which left a second rating on the
        same booking representable, and it happened.
        """
        import uuid

        from core.state_machine import JobState
        from jobs.models import Review
        from jobs.services import ReviewError, leave_review

        group = uuid.uuid4()
        days = [
            self.gig(
                offer_group=group,
                state=JobState.CLOSED,
                assigned_worker=self.worker_profile,
                gig_date=timezone.localdate() - timedelta(days=n),
            )
            for n in (3, 2, 1)
        ]

        leave_review(days[0], self.client_user, rating=5, comment="First day.")

        # Every other day of the same booking now refuses, from either end.
        for day in days[1:]:
            with self.subTest(day=day.gig_date):
                with self.assertRaises(ReviewError):
                    leave_review(day, self.client_user, rating=1, comment="Again.")

        self.assertEqual(Review.objects.filter(booking=group).count(), 1)

    def test_the_database_refuses_a_second_rating_on_a_booking(self):
        """Not only the service. The constraint is the thing that cannot drift."""
        import uuid

        from django.db import IntegrityError, transaction

        from core.state_machine import JobState
        from jobs.models import Review, ReviewDirection

        group = uuid.uuid4()
        first, second = [
            self.gig(
                offer_group=group,
                state=JobState.CLOSED,
                assigned_worker=self.worker_profile,
                gig_date=timezone.localdate() - timedelta(days=n),
            )
            for n in (2, 1)
        ]
        Review.objects.create(
            job=first, author=self.client_user,
            direction=ReviewDirection.CLIENT_ON_WORKER, rating=5,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Review.objects.create(
                    job=second, author=self.client_user,
                    direction=ReviewDirection.CLIENT_ON_WORKER, rating=2,
                )

    def test_the_average_counts_a_booking_once(self):
        """A week rated once must move the average by one job, not by five."""
        import uuid

        from core.state_machine import JobState
        from jobs.services import leave_review

        group = uuid.uuid4()
        days = [
            self.gig(
                offer_group=group,
                state=JobState.CLOSED,
                assigned_worker=self.worker_profile,
                gig_date=timezone.localdate() - timedelta(days=n),
            )
            for n in (2, 1)
        ]
        leave_review(days[0], self.client_user, rating=4)

        self.worker_profile.refresh_from_db()
        self.assertEqual(self.worker_profile.rating_count, 1)
        self.assertEqual(self.worker_profile.rating_sum, 4)

    def test_a_stranger_gets_a_404_not_a_refusal(self):
        """Confirming the job exists would leak who is hiring, and for what."""
        job = self.finished_job()
        outsider = make_user("nosy2@example.com")
        self.client.force_login(outsider)
        response = self.client.get(reverse("jobs:review", args=[job.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_job_page_offers_the_rating_once_it_is_due(self):
        job = self.finished_job()
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:detail", args=[job.pk]))
        self.assertContains(response, reverse("jobs:review", args=[job.pk]))

    def test_and_stops_offering_it_afterwards(self):
        job = self.finished_job()
        self.rate(job, self.client_user, 5)
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:detail", args=[job.pk]))
        self.assertNotContains(response, reverse("jobs:review", args=[job.pk]))


class MutualDoneTests(JobFactoryMixin, TestCase):
    """"Job done" from both sides, reachable from the job page.

    The buttons used to live only in the workspace. A button nobody finds is a
    button that does not exist, and this is the page both parties land on from
    a link, a message or their own list.
    """

    def accepted_no_escrow(self):
        from core.state_machine import JobState

        job = self.gig()
        job.use_escrow = False
        job.state = JobState.ACCEPTED
        job.assigned_worker = self.worker_profile
        job.gig_date = timezone.localdate() - timedelta(days=1)
        job.save(update_fields=["use_escrow", "state", "assigned_worker", "gig_date"])
        return job

    def test_the_worker_is_offered_the_button_on_the_job_page(self):
        job = self.accepted_no_escrow()
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("jobs:detail", args=[job.pk]))
        self.assertContains(response, reverse("worklog:finish", args=[job.pk]))

    def test_the_client_is_not_offered_it_first(self):
        """Mutual means the worker says so first — they did the work."""
        job = self.accepted_no_escrow()
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:detail", args=[job.pk]))
        self.assertNotContains(response, reverse("worklog:confirm", args=[job.pk]))

    def test_after_the_worker_marks_it_the_client_gets_the_button(self):
        from core.state_machine import JobState

        job = self.accepted_no_escrow()
        self.client.force_login(self.worker_user)
        self.client.post(reverse("worklog:finish", args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.COMPLETED)

        self.client.force_login(self.client_user)
        response = self.client.get(reverse("jobs:detail", args=[job.pk]))
        self.assertContains(response, reverse("worklog:confirm", args=[job.pk]))

    def test_both_pressing_it_closes_the_job_and_opens_rating(self):
        from core.state_machine import JobState

        job = self.accepted_no_escrow()
        self.client.force_login(self.worker_user)
        self.client.post(reverse("worklog:finish", args=[job.pk]))
        self.client.force_login(self.client_user)
        self.client.post(reverse("worklog:confirm", args=[job.pk]))

        job.refresh_from_db()
        self.assertEqual(job.state, JobState.CLOSED)
        # The day has passed, so rating is due for both.
        self.assertTrue(job.can_be_reviewed_by(self.client_user))
        self.assertTrue(job.can_be_reviewed_by(self.worker_user))

    def test_an_escrowed_job_keeps_the_escrow_flow(self):
        """No stray "job done" button on a job with money held."""
        from core.state_machine import JobState

        job = self.gig()
        job.use_escrow = True
        job.state = JobState.ACCEPTED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["use_escrow", "state", "assigned_worker"])
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("jobs:detail", args=[job.pk]))
        self.assertNotContains(response, reverse("worklog:finish", args=[job.pk]))
