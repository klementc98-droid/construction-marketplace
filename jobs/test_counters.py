"""Counter-offers: negotiating the terms of a direct offer, from either side.

The cases worth having are the ones that cost somebody money or a day's work:
a price moving on a job nobody agreed to move it on, both sides holding a live
proposal at once, or somebody outside the deal putting a number on it.

The happy path — offer, counter, counter back, accept — is covered because it
is the feature, but the invariants below are what the file is for.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import WorkerProfile
from core.state_machine import JobState

from .models import (
    Application,
    ApplicationStatus,
    Counter,
    CounterStatus,
    Job,
    JobType,
    Offer,
    OfferStatus,
    Review,
    Party,
)
from .tests import JobFactoryMixin, make_user


class CounterFixture(JobFactoryMixin, TestCase):
    #: Private by default — most cases here are the direct-offer thread. The
    #: public-board cases flip it, since that is the difference under test.
    private = True

    def setUp(self):
        self.job = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.carpentry,
            region=self.region,
            title="Second fix, Tuesday",
            description="Hanging doors on the first floor.",
            gig_date=timezone.localdate() + timedelta(days=5),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("240"),
            is_private=self.private,
        )
        self.offer = (
            Offer.objects.create(job=self.job, worker=self.worker_profile)
            if self.private
            else None
        )

    def as_worker(self):
        self.client.force_login(self.worker_user)

    def as_client(self):
        self.client.force_login(self.client_user)

    def counter(self, by, worker=None, **terms):
        return Counter.objects.create(
            job=self.job,
            worker=worker or self.worker_profile,
            proposed_by=by,
            **terms,
        )

    def counter_url(self, party, worker=None):
        """A worker negotiates for themselves; a client names the person."""
        if party == Party.WORKER:
            return reverse("jobs:counter", kwargs={"pk": self.job.pk})
        return reverse(
            "jobs:counter_to",
            kwargs={"pk": self.job.pk, "worker_pk": (worker or self.worker_profile).pk},
        )

    def post_counter(self, party=Party.WORKER, worker=None, **payload):
        return self.client.post(
            self.counter_url(party, worker),
            {
                "fixed_pay": "280",
                "gig_hours": "8",
                "gig_date": self.job.gig_date.isoformat(),
                "note": "",
            }
            | payload,
        )

    def respond(self, counter, answer):
        return self.client.post(
            reverse("jobs:counter_respond", kwargs={"pk": counter.pk}),
            {"answer": answer},
        )

    def live(self, worker=None):
        return self.job.live_counter_from(worker or self.worker_profile)


class TurnTakingTests(CounterFixture):
    """Who owes an answer, derived from the offer rather than stored."""

    def test_a_fresh_offer_waits_on_the_worker(self):
        self.assertEqual(self.offer.awaiting_from, Party.WORKER)

    def test_a_worker_counter_moves_the_turn_to_the_client(self):
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.assertEqual(self.offer.awaiting_from, Party.CLIENT)

    def test_a_client_counter_moves_the_turn_back(self):
        first = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        first.status = CounterStatus.SUPERSEDED
        first.save()
        self.counter(Party.CLIENT, fixed_pay=Decimal("260"))
        self.assertEqual(self.offer.awaiting_from, Party.WORKER)

    def test_a_closed_offer_waits_on_nobody(self):
        self.offer.status = OfferStatus.DECLINED
        self.offer.save()
        self.assertIsNone(self.offer.awaiting_from)

    def test_countering_out_of_turn_is_refused(self):
        """Both sides holding a proposal would mean two agreed prices."""
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="300")
        self.assertEqual(self.job.counters.count(), 1)

    def test_only_one_live_counter_per_pair_can_exist(self):
        """Enforced in the database, not only by the view."""
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.counter(Party.CLIENT, fixed_pay=Decimal("260"))

    def test_a_new_counter_supersedes_the_last(self):
        first = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.post_counter(Party.CLIENT, fixed_pay="260")
        first.refresh_from_db()
        self.assertEqual(first.status, CounterStatus.SUPERSEDED)
        self.assertEqual(self.job.counters.count(), 2)

    def test_superseded_rounds_are_kept_not_deleted(self):
        """How a figure was arrived at is part of deciding whether to take it."""
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.post_counter(Party.CLIENT, fixed_pay="260")
        self.assertEqual(self.job.counters.count(), 2)
        self.assertTrue(self.offer.has_history)


class TermsSafetyTests(CounterFixture):
    """The job's price is what the client's card is charged."""

    def test_proposing_does_not_move_the_job(self):
        """Nothing on the job changes until somebody actually agrees."""
        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="280")
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("240"))

    def test_declining_does_not_move_the_job(self):
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.respond(counter, "decline")
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("240"))

    def test_accepting_writes_the_agreed_terms_onto_the_job(self):
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.respond(counter, "accept")
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("280"))
        self.assertEqual(self.job.state, JobState.ACCEPTED)
        self.assertEqual(self.job.assigned_worker, self.worker_profile)

    def test_a_counter_only_moves_what_it_names(self):
        """Money-only counters must leave the date and hours alone."""
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        original_date, original_hours = self.job.gig_date, self.job.gig_hours
        self.as_client()
        self.respond(counter, "accept")
        self.job.refresh_from_db()
        self.assertEqual(self.job.gig_date, original_date)
        self.assertEqual(self.job.gig_hours, original_hours)

    def test_a_counter_can_move_the_date_as_well(self):
        moved = self.job.gig_date + timedelta(days=1)
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"), gig_date=moved)
        self.as_client()
        self.respond(counter, "accept")
        self.job.refresh_from_db()
        self.assertEqual(self.job.gig_date, moved)

    def test_accepting_the_offer_outright_takes_the_live_terms(self):
        """The worker sees the revised figure, so that is what gets written."""
        self.counter(Party.CLIENT, fixed_pay=Decimal("300"))
        self.as_worker()
        self.client.post(
            reverse("jobs:offer_respond", kwargs={"pk": self.offer.pk}),
            {"answer": "accept", "response_note": ""},
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("300"))

    def test_a_counter_on_a_job_taken_meanwhile_does_not_land(self):
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.job.state = JobState.CANCELLED
        self.job.save(update_fields=["state"])
        self.as_client()
        self.respond(counter, "accept")
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("240"))
        self.assertIsNone(self.job.assigned_worker)


class PermissionTests(CounterFixture):
    def test_a_stranger_cannot_counter_a_private_offer(self):
        outsider = WorkerProfile.objects.create(
            user=make_user("nosy@example.com"), region=self.region
        )
        self.client.force_login(outsider.user)
        response = self.post_counter(Party.WORKER)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.job.counters.count(), 0)

    def test_a_stranger_cannot_answer_a_counter(self):
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        outsider = WorkerProfile.objects.create(
            user=make_user("nosy2@example.com"), region=self.region
        )
        self.client.force_login(outsider.user)
        self.assertEqual(self.respond(counter, "accept").status_code, 404)

    def test_you_cannot_accept_your_own_counter(self):
        """Otherwise either side could agree with themselves and book the job."""
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_worker()
        self.assertEqual(self.respond(counter, "accept").status_code, 404)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.POSTED)

    def test_a_client_cannot_haggle_with_themselves(self):
        """The client posting their own job is not a worker on it."""
        self.as_client()
        self.post_counter(Party.CLIENT)
        self.assertEqual(self.job.counters.count(), 0)

    def test_answering_twice_is_a_no_op(self):
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.respond(counter, "accept")
        self.respond(counter, "decline")
        counter.refresh_from_db()
        self.assertEqual(counter.status, CounterStatus.ACCEPTED)

    def test_a_standing_position_has_no_terms_to_negotiate(self):
        standing = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.STANDING,
            trade=self.carpentry,
            region=self.region,
            title="Site carpenter",
            description="Ongoing.",
            position_type="ongoing",
            rate_type="hourly",
            rate_min=Decimal("30"),
        )
        self.as_worker()
        self.client.post(reverse("jobs:counter", kwargs={"pk": standing.pk}), {})
        self.assertEqual(standing.counters.count(), 0)


class OutcomeTests(CounterFixture):
    def test_turning_terms_down_rejects_the_price_not_the_person(self):
        """"No" to a number is not "no" to the deal.

        Conflating them would end negotiations over a figure somebody was
        willing to move on. Both sides still have a plain no elsewhere —
        withdraw for the client, decline for the worker.
        """
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.respond(counter, "decline")
        counter.refresh_from_db()
        self.offer.refresh_from_db()
        self.assertEqual(counter.status, CounterStatus.DECLINED)
        self.assertEqual(self.offer.status, OfferStatus.PENDING)

    def test_after_a_rejected_price_the_original_offer_still_stands(self):
        counter = self.counter(Party.CLIENT, fixed_pay=Decimal("250"))
        self.as_worker()
        self.respond(counter, "decline")
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("240"))
        self.assertTrue(self.job.is_open)

    def test_a_full_round_trip_closes_the_deal(self):
        """Offer 240 -> worker wants 280 -> client says 260 -> worker accepts."""
        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="280")

        self.as_client()
        self.post_counter(Party.CLIENT, fixed_pay="260")

        self.as_worker()
        live = self.live()
        self.assertEqual(live.proposed_by, Party.CLIENT)
        self.respond(live, "accept")

        self.job.refresh_from_db()
        self.offer.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("260"))
        self.assertEqual(self.job.state, JobState.ACCEPTED)
        self.assertEqual(self.offer.status, OfferStatus.ACCEPTED)
        self.assertEqual(self.offer.rounds, 2)


class PublicBoardTests(CounterFixture):
    """The point of the feature: haggling on any open gig, not just offers.

    A public gig has as many negotiations as it has interested workers, so the
    turn-taking is per pair and several people may be asking several prices at
    once.
    """

    private = False

    def setUp(self):
        super().setUp()
        self.rival = WorkerProfile.objects.create(
            user=make_user("rival@example.com"), region=self.region
        )

    def test_any_worker_can_name_their_price_on_an_open_gig(self):
        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="280")
        counter = self.live()
        self.assertIsNotNone(counter)
        self.assertEqual(counter.fixed_pay, Decimal("280"))

    def test_naming_a_price_puts_you_forward(self):
        """Making somebody apply first, at a price they have said no to, is a
        step for its own sake."""
        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="280")
        self.assertTrue(
            Application.objects.filter(
                job=self.job, worker=self.worker_profile,
                status=ApplicationStatus.APPLIED,
            ).exists()
        )

    def test_naming_a_price_reaches_the_client_as_a_message(self):
        """An applicant list is somewhere you go. A thread comes to you.

        The unread badge in the header is the only thing in this app that tells
        a client something happened, so a counter that never becomes a message
        waits to be stumbled on.
        """
        from messaging.models import Conversation, Message

        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="280", note="Long day for that.")

        conversation = Conversation.objects.get(
            job=self.job, worker=self.worker_profile
        )
        message = Message.objects.get(conversation=conversation)
        # The worker sends it, so the client can answer the terms by replying.
        self.assertEqual(message.sender, self.worker_user)
        self.assertIsNone(message.read_at)
        self.assertIn("280", message.body)
        self.assertIn("Long day for that.", message.body)

    def test_the_message_says_how_the_proposed_terms_would_be_paid(self):
        """The one term that decides whose money is held has to be in the
        sentence, not left for the client to go and look up."""
        from messaging.models import Message

        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="280", use_escrow="False")

        body = Message.objects.get().body
        self.assertIn("settled directly", body)

    def test_the_gig_goes_to_whoever_claims_it_first(self):
        """Two people answering the same gig in the same second.

        The loser has to be told, and the winner has to keep the job. The
        failure this guards is not an error page — it is both workers being
        told the job is theirs, because the second write overwrote the first.
        """
        from unittest import mock

        from core.state_machine import Actor

        from .views import _seal

        now = timezone.now()

        # The interleaving, forced open. assert_transition runs after _seal has
        # read the job and before it writes, which is exactly the window a
        # second request would land in — so a rival claiming the gig from
        # inside it reproduces the race without threads or a real database
        # scheduler, both of which make a test that fails once a fortnight.
        real = _seal.__globals__["assert_transition"]

        def steal(*args, **kwargs):
            result = real(*args, **kwargs)
            Job.objects.filter(pk=self.job.pk, state=JobState.POSTED).update(
                state=JobState.ACCEPTED,
                assigned_worker=self.rival,
                filled_at=now,
                updated_at=now,
            )
            return result

        with mock.patch("jobs.views.assert_transition", side_effect=steal):
            with transaction.atomic():
                sealed = _seal(
                    self.job.pk, self.worker_profile, None, now, actor=Actor.CLIENT
                )

        # Nought rows updated, so the caller is told rather than guessing.
        self.assertIsNone(sealed)

        self.job.refresh_from_db()
        self.assertEqual(self.job.assigned_worker, self.rival)
        self.assertEqual(self.job.state, JobState.ACCEPTED)

    def test_nothing_is_written_when_the_claim_is_lost(self):
        """Returning early inside the caller's atomic block still commits, so
        the counter must not be marked accepted for a job somebody else won."""
        from unittest import mock

        from core.state_machine import Actor

        from .views import _seal

        now = timezone.now()
        counter = self.counter(
            Party.WORKER, worker=self.worker_profile, fixed_pay=Decimal("280")
        )

        real = _seal.__globals__["assert_transition"]

        def steal(*args, **kwargs):
            result = real(*args, **kwargs)
            Job.objects.filter(pk=self.job.pk, state=JobState.POSTED).update(
                state=JobState.ACCEPTED, assigned_worker=self.rival, updated_at=now
            )
            return result

        with mock.patch("jobs.views.assert_transition", side_effect=steal):
            with transaction.atomic():
                sealed = _seal(
                    self.job.pk, self.worker_profile, counter, now, actor=Actor.CLIENT
                )

        self.assertIsNone(sealed)
        counter.refresh_from_db()
        self.assertEqual(counter.status, CounterStatus.PENDING)
        # And the price on the counter never reached the job.
        self.job.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("240"))

    def test_agreeing_terms_takes_the_whole_booking_off_the_board(self):
        """Two people agreed. Nobody else should still be able to apply to it.

        The counter was made on one day because that is where the numbers live,
        but it was answered once, and a booking half agreed leaves days on the
        board that are no longer available.
        """
        from uuid import uuid4

        group = uuid4()
        self.job.offer_group = group
        self.job.save(update_fields=["offer_group"])
        siblings = [
            Job.objects.create(
                client=self.client_profile,
                job_type=JobType.GIG,
                trade=self.carpentry,
                region=self.region,
                title="Second fix, Tuesday",
                description="Hanging doors on the first floor.",
                gig_date=self.job.gig_date + timedelta(days=n),
                gig_hours=Decimal("8"),
                fixed_pay=Decimal("240"),
                is_private=self.private,
                offer_group=group,
            )
            for n in (1, 2)
        ]

        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.respond(counter, "accept")

        self.assertEqual(Job.objects.public().filter(offer_group=group).count(), 0)
        for job in [self.job, *siblings]:
            job.refresh_from_db()
            self.assertEqual(job.state, JobState.ACCEPTED)
            self.assertEqual(job.assigned_worker, self.worker_profile)

    def test_the_agreed_price_is_written_to_the_day_it_was_agreed_on(self):
        """And not smeared across the booking: countering one day's money says
        nothing about another's, and the dates differ by definition."""
        from uuid import uuid4

        group = uuid4()
        self.job.offer_group = group
        self.job.save(update_fields=["offer_group"])
        other = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.carpentry,
            region=self.region,
            title="Second fix, Tuesday",
            description="Hanging doors on the first floor.",
            gig_date=self.job.gig_date + timedelta(days=1),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("240"),
            is_private=self.private,
            offer_group=group,
        )

        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        self.respond(counter, "accept")

        self.job.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("280"))
        self.assertEqual(other.fixed_pay, Decimal("240"))
        self.assertEqual(other.gig_date, self.job.gig_date + timedelta(days=1))

    def test_a_booking_is_rated_once_however_many_days_it_ran(self):
        """Five days for one client is one opinion, not five — and five would
        count five times towards a rating average that is meant to say how many
        jobs somebody has been rated on."""
        from uuid import uuid4

        group = uuid4()
        self.job.offer_group = group
        self.job.state = JobState.CLOSED
        self.job.assigned_worker = self.worker_profile
        self.job.save(update_fields=["offer_group", "state", "assigned_worker"])
        later = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.carpentry,
            region=self.region,
            title="Second fix, Tuesday",
            description="Hanging doors on the first floor.",
            gig_date=self.job.gig_date + timedelta(days=1),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("240"),
            is_private=self.private,
            offer_group=group,
            state=JobState.CLOSED,
            assigned_worker=self.worker_profile,
        )

        self.as_client()
        first = self.client.post(
            reverse("jobs:review", kwargs={"pk": self.job.pk}),
            {"rating": 5, "comment": "Good week."},
        )
        self.assertEqual(first.status_code, 302)

        # Arriving from another day of the same booking is the same rating.
        self.client.post(
            reverse("jobs:review", kwargs={"pk": later.pk}),
            {"rating": 1, "comment": "Second bite."},
        )

        self.assertEqual(Review.objects.count(), 1)
        self.worker_profile.refresh_from_db()
        self.assertEqual(self.worker_profile.rating_count, 1)

    def test_the_negotiate_button_is_offered_on_an_open_gig(self):
        self.as_worker()
        response = self.client.get(reverse("jobs:detail", kwargs={"pk": self.job.pk}))
        self.assertTrue(response.context["can_negotiate"])
        self.assertContains(response, reverse("jobs:counter", kwargs={"pk": self.job.pk}))

    def test_two_workers_can_ask_two_different_prices(self):
        """Across pairs the one-live-counter rule deliberately does not apply."""
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.counter(Party.WORKER, worker=self.rival, fixed_pay=Decimal("260"))
        self.assertEqual(self.job.live_counters.count(), 2)

    def test_the_client_sees_each_asking_price_on_the_applicants_page(self):
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.counter(Party.WORKER, worker=self.rival, fixed_pay=Decimal("260"))
        Application.objects.create(job=self.job, worker=self.worker_profile)
        Application.objects.create(job=self.job, worker=self.rival)

        self.as_client()
        response = self.client.get(
            reverse("jobs:applicants", kwargs={"pk": self.job.pk})
        )
        self.assertContains(response, "$280")
        self.assertContains(response, "$260")

    def test_accepting_one_price_settles_the_job_and_moots_the_rest(self):
        mine = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        theirs = self.counter(Party.WORKER, worker=self.rival, fixed_pay=Decimal("260"))

        self.as_client()
        self.respond(theirs, "accept")

        self.job.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(self.job.fixed_pay, Decimal("260"))
        self.assertEqual(self.job.assigned_worker, self.rival)
        self.assertEqual(mine.status, CounterStatus.SUPERSEDED)

    def test_losing_applicants_still_get_a_definite_answer(self):
        """The courtesy application_select has always paid, owed by every route."""
        theirs = self.counter(Party.WORKER, worker=self.rival, fixed_pay=Decimal("260"))
        mine = Application.objects.create(job=self.job, worker=self.worker_profile)

        self.as_client()
        self.respond(theirs, "accept")

        mine.refresh_from_db()
        self.assertEqual(mine.status, ApplicationStatus.PASSED_OVER)

    def test_selecting_someone_who_asked_for_more_is_blocked(self):
        """The plain select button would book them at a price they said no to."""
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        application = Application.objects.create(
            job=self.job, worker=self.worker_profile
        )
        self.as_client()
        self.client.post(
            reverse("jobs:application_select", kwargs={"pk": application.pk})
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.POSTED)
        self.assertEqual(self.job.fixed_pay, Decimal("240"))

    def test_a_client_cannot_open_a_negotiation_with_a_silent_worker(self):
        """Approaching one named person unprompted is what a direct offer is."""
        self.as_client()
        self.post_counter(Party.CLIENT, worker=self.rival, fixed_pay="200")
        self.assertEqual(self.job.counters.count(), 0)

    def test_the_client_can_counter_a_worker_who_asked(self):
        self.counter(Party.WORKER, worker=self.rival, fixed_pay=Decimal("280"))
        self.as_client()
        self.post_counter(Party.CLIENT, worker=self.rival, fixed_pay="260")
        live = self.job.live_counter_from(self.rival)
        self.assertEqual(live.proposed_by, Party.CLIENT)
        self.assertEqual(live.fixed_pay, Decimal("260"))

    def test_a_client_who_countered_gets_no_accept_button(self):
        """The bug this class of check exists for.

        After countering, the client is waiting on the worker. An accept button
        there would be the client agreeing to their own proposal on the worker's
        behalf — a deal the worker has never seen. The server refuses it, but a
        button that 404s on the screen where money is agreed is its own defect.
        """
        self.counter(Party.CLIENT, worker=self.rival, fixed_pay=Decimal("260"))
        Application.objects.create(job=self.job, worker=self.rival)

        self.as_client()
        response = self.client.get(
            reverse("jobs:applicants", kwargs={"pk": self.job.pk})
        )
        live = self.job.live_counter_from(self.rival)
        self.assertNotContains(
            response, reverse("jobs:counter_respond", kwargs={"pk": live.pk})
        )
        self.assertContains(response, "waiting on")

    def test_a_client_awaiting_an_answer_cannot_hire_at_the_posted_price_either(self):
        """Their own outstanding counter is the live proposal; $240 is not."""
        self.counter(Party.CLIENT, worker=self.rival, fixed_pay=Decimal("260"))
        application = Application.objects.create(job=self.job, worker=self.rival)

        self.as_client()
        self.client.post(
            reverse("jobs:application_select", kwargs={"pk": application.pk})
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.POSTED)

    def test_the_client_cannot_accept_their_own_counter(self):
        counter = self.counter(Party.CLIENT, worker=self.rival, fixed_pay=Decimal("260"))
        self.as_client()
        self.assertEqual(self.respond(counter, "accept").status_code, 404)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.POSTED)
        self.assertIsNone(self.job.assigned_worker)

    def test_the_accept_button_is_back_once_the_worker_answers(self):
        """The other half: when it IS their turn, the button must be there."""
        live = self.counter(Party.WORKER, worker=self.rival, fixed_pay=Decimal("280"))
        Application.objects.create(job=self.job, worker=self.rival)

        self.as_client()
        response = self.client.get(
            reverse("jobs:applicants", kwargs={"pk": self.job.pk})
        )
        self.assertContains(
            response, reverse("jobs:counter_respond", kwargs={"pk": live.pk})
        )

    def test_a_filled_gig_cannot_be_haggled_over(self):
        self.job.state = JobState.ACCEPTED
        self.job.assigned_worker = self.rival
        self.job.save(update_fields=["state", "assigned_worker"])
        self.as_worker()
        self.post_counter(Party.WORKER, fixed_pay="280")
        self.assertEqual(self.job.counters.count(), 0)


class CounterFormTests(CounterFixture):
    def test_a_counter_that_changes_nothing_is_rejected(self):
        """It would offer the other side terms they had already been given."""
        self.as_worker()
        self.post_counter(
            Party.WORKER,
            fixed_pay="240",
            gig_hours="8",
            gig_date=self.job.gig_date.isoformat(),
        )
        self.assertEqual(self.job.counters.count(), 0)

    def test_a_date_in_the_past_is_rejected(self):
        self.as_worker()
        self.post_counter(
            Party.WORKER,
            gig_date=(timezone.localdate() - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(self.job.counters.count(), 0)

    def test_the_form_starts_from_the_terms_on_the_table(self):
        """Most counters move one figure; starting blank makes that a re-entry."""
        self.as_worker()
        response = self.client.get(self.counter_url(Party.WORKER))
        form = response.context["form"]
        self.assertEqual(form.fields["fixed_pay"].initial, Decimal("240"))
        # And it actually reaches the widget — a field initial that never gets
        # rendered is the same as no prefill at all.
        self.assertRegex(
            response.content.decode(), r'name="fixed_pay"[^>]*value="240(\.00)?"'
        )

    def test_countering_back_starts_from_the_other_sides_figure(self):
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.as_client()
        response = self.client.get(self.counter_url(Party.CLIENT))
        self.assertEqual(
            response.context["form"].fields["fixed_pay"].initial, Decimal("280")
        )


class DisplayTests(CounterFixture):
    def test_the_change_is_shown_as_before_and_after(self):
        """A price shown alone is not a proposal anyone can weigh."""
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.assertEqual(counter.changes, [("Pay", "$240", "$280")])

    def test_changes_is_reachable_from_a_template(self):
        """It must take no arguments — a template cannot pass one, and would
        render an empty diff rather than raising."""
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        self.assertNotIn("changes", type(counter).__dict__.get("__annotations__", {}))
        self.assertIsInstance(type(counter).changes, property)

    def test_unchanged_terms_are_not_listed_as_changes(self):
        counter = self.counter(
            Party.WORKER, fixed_pay=Decimal("280"), gig_hours=self.job.gig_hours
        )
        labels = [row[0] for row in counter.changes]
        self.assertEqual(labels, ["Pay"])

    def test_both_sides_see_the_live_terms_on_the_job_page(self):
        self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        for login in (self.as_worker, self.as_client):
            login()
            response = self.client.get(reverse("jobs:detail", kwargs={"pk": self.job.pk}))
            with self.subTest(user=response.context["user"].email):
                self.assertContains(response, "$280")

    def test_only_the_side_whose_turn_it_is_sees_an_accept_button(self):
        counter = self.counter(Party.WORKER, fixed_pay=Decimal("280"))
        accept = reverse("jobs:counter_respond", kwargs={"pk": counter.pk})

        self.as_client()
        self.assertContains(
            self.client.get(reverse("jobs:detail", kwargs={"pk": self.job.pk})), accept
        )
        self.as_worker()
        self.assertNotContains(
            self.client.get(reverse("jobs:detail", kwargs={"pk": self.job.pk})), accept
        )
