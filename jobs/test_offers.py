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
from .models import Counter, CounterStatus
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
        job.state = JobState.ACCEPTED
        job.assigned_worker = self.worker_profile
        job.save(update_fields=["state", "assigned_worker"])
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("jobs:detail", args=[job.pk]))
        self.assertNotContains(response, reverse("worklog:finish", args=[job.pk]))
