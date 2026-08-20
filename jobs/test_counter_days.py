"""Countering an offer with the days you can actually do.

An offer can be a week, and the commonest honest answer to a week is "all of it
except Wednesday" — somebody is already booked that day. Until now the only
ways to say that were to decline the whole booking or to counter with a single
different date and lose the other four.

So a counter proposes a set of days, and accepting it makes the booking run on
them. The rules underneath that are: reuse a row before creating one, create
one before cancelling one, and never touch a day nobody said anything about.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.state_machine import JobState

from .models import Counter, CounterStatus, Job, JobType, Party
from .tests import JobFactoryMixin


class CounterDaysFixture(JobFactoryMixin, TestCase):
    """One client, one worker, and a booking of three days."""

    def setUp(self):
        self.group = uuid4()
        self.first = timezone.localdate() + timedelta(days=7)
        self.days = [self.first + timedelta(days=n) for n in range(3)]
        self.rows = [
            self.gig(gig_date=day, offer_group=self.group) for day in self.days
        ]
        self.job = self.rows[0]

    def counter_from_worker(self, **terms):
        return Counter.objects.create(
            job=self.job,
            worker=self.worker_profile,
            proposed_by=Party.WORKER,
            **terms,
        )

    def accept_as_client(self, counter):
        self.client.force_login(self.client_user)
        return self.client.post(
            reverse("jobs:counter_respond", kwargs={"pk": counter.pk}),
            {"answer": "accept"},
        )

    def booking(self):
        return Job.objects.filter(offer_group=self.group).order_by("gig_date")


class DroppingADayTests(CounterDaysFixture):
    """The case the whole thing exists for."""

    def test_a_worker_can_agree_to_some_of_the_days(self):
        keep = [self.days[0], self.days[2]]
        counter = self.counter_from_worker(
            gig_dates=[d.isoformat() for d in keep], fixed_pay=Decimal("280")
        )
        self.accept_as_client(counter)

        taken = self.booking().filter(state=JobState.ACCEPTED)
        self.assertEqual([j.gig_date for j in taken], keep)
        self.assertTrue(all(j.assigned_worker == self.worker_profile for j in taken))

    def test_the_day_they_left_out_does_not_stay_open(self):
        """The client pressed accept on a screen that said two days. Ending up
        with a third gig still live on the board is the worse surprise."""
        keep = [self.days[0], self.days[2]]
        counter = self.counter_from_worker(gig_dates=[d.isoformat() for d in keep])
        self.accept_as_client(counter)

        dropped = self.booking().get(gig_date=self.days[1])
        self.assertEqual(dropped.state, JobState.CANCELLED)
        self.assertIsNone(dropped.assigned_worker)

    def test_the_agreed_price_lands_on_the_day_that_was_answered(self):
        """And not smeared across the booking — countering one day's money says
        nothing about another's."""
        counter = self.counter_from_worker(
            gig_dates=[d.isoformat() for d in self.days], fixed_pay=Decimal("280")
        )
        self.accept_as_client(counter)

        answered = self.booking().get(pk=self.job.pk)
        others = self.booking().exclude(pk=self.job.pk)
        self.assertEqual(answered.fixed_pay, Decimal("280"))
        self.assertTrue(all(j.fixed_pay == Decimal("90") for j in others))


class MovingADayTests(CounterDaysFixture):
    """A day nobody wants is re-dated rather than binned and rebuilt."""

    def test_a_single_day_gig_moved_is_still_the_same_row(self):
        """"Move it to Tuesday" has always been one row with a new date on it,
        and it should stay that way: a cancellation plus a creation would read
        as two events in both parties' lists."""
        job = self.gig()
        moved = job.gig_date + timedelta(days=1)
        counter = Counter.objects.create(
            job=job,
            worker=self.worker_profile,
            proposed_by=Party.WORKER,
            gig_dates=[moved.isoformat()],
        )
        self.accept_as_client(counter)

        job.refresh_from_db()
        self.assertEqual(job.gig_date, moved)
        self.assertEqual(job.state, JobState.ACCEPTED)

    def test_swapping_one_day_of_a_booking_reuses_its_row(self):
        before = set(self.booking().values_list("pk", flat=True))
        instead = self.days[2] + timedelta(days=4)
        wanted = [self.days[0], self.days[1], instead]

        counter = self.counter_from_worker(
            gig_dates=[d.isoformat() for d in wanted]
        )
        self.accept_as_client(counter)

        after = set(self.booking().values_list("pk", flat=True))
        self.assertEqual(before, after, "no row should have been created or lost")
        self.assertEqual([j.gig_date for j in self.booking()], sorted(wanted))


class AddingADayTests(CounterDaysFixture):
    """"Not Wednesday, but I could do the Saturday."""

    def test_a_day_nobody_posted_is_created_on_agreement(self):
        extra = self.days[2] + timedelta(days=3)
        wanted = self.days + [extra]
        counter = self.counter_from_worker(
            gig_dates=[d.isoformat() for d in wanted]
        )
        self.accept_as_client(counter)

        self.assertEqual([j.gig_date for j in self.booking()], sorted(wanted))
        added = self.booking().get(gig_date=extra)
        self.assertEqual(added.state, JobState.ACCEPTED)
        self.assertEqual(added.assigned_worker, self.worker_profile)

    def test_it_carries_the_terms_of_the_job_it_was_cloned_from(self):
        """It is a day of the same job, not a new posting."""
        extra = self.days[2] + timedelta(days=3)
        counter = self.counter_from_worker(
            gig_dates=[d.isoformat() for d in self.days + [extra]]
        )
        self.accept_as_client(counter)

        added = self.booking().get(gig_date=extra)
        self.assertEqual(added.trade, self.job.trade)
        self.assertEqual(added.title, self.job.title)
        self.assertEqual(added.gig_hours, self.job.gig_hours)
        self.assertEqual(added.offer_group, self.group)

    def test_a_single_day_gig_that_gains_a_day_becomes_a_booking(self):
        """Both rows have to agree on which booking they are, and a lone gig
        has no group to join."""
        job = self.gig()
        extra = job.gig_date + timedelta(days=1)
        counter = Counter.objects.create(
            job=job,
            worker=self.worker_profile,
            proposed_by=Party.WORKER,
            gig_dates=[job.gig_date.isoformat(), extra.isoformat()],
        )
        self.accept_as_client(counter)

        job.refresh_from_db()
        self.assertIsNotNone(job.offer_group)
        pair = Job.objects.filter(offer_group=job.offer_group)
        self.assertEqual(pair.count(), 2)
        self.assertTrue(all(j.state == JobState.ACCEPTED for j in pair))


class SayingNothingAboutDaysTests(CounterDaysFixture):
    """A counter names only what it wants changed — days included."""

    def test_a_price_only_counter_leaves_every_day_alone(self):
        counter = self.counter_from_worker(fixed_pay=Decimal("280"))
        self.accept_as_client(counter)

        self.assertEqual([j.gig_date for j in self.booking()], self.days)
        self.assertTrue(
            all(j.state == JobState.ACCEPTED for j in self.booking())
        )

    def test_such_a_counter_stores_no_days(self):
        counter = self.counter_from_worker(fixed_pay=Decimal("280"))
        self.assertIsNone(counter.gig_dates)
        self.assertEqual(counter.proposed_days, [])
        self.assertIsNone(counter.gig_date)


class DoubleBookingTests(CounterDaysFixture):
    """A day already sold cannot be agreed here either."""

    def test_a_day_the_worker_is_already_booked_on_is_refused(self):
        taken = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.carpentry,
            region=self.region,
            title="Elsewhere",
            description="Another site.",
            gig_date=self.days[1],
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("90"),
            state=JobState.ACCEPTED,
            assigned_worker=self.worker_profile,
        )
        self.assertEqual(taken.state, JobState.ACCEPTED)

        counter = self.counter_from_worker(
            gig_dates=[d.isoformat() for d in self.days]
        )
        response = self.accept_as_client(counter)

        # A redirect, not a 404: this test is about the guard refusing, and a
        # test that passed because the view was never reached would prove
        # nothing at all.
        self.assertEqual(response.status_code, 302)
        counter.refresh_from_db()
        self.assertEqual(counter.status, CounterStatus.PENDING)
        self.assertTrue(
            all(j.state == JobState.POSTED for j in self.booking()),
            "nothing should have been sealed",
        )


class CounterFormDaysTests(CounterDaysFixture):
    """What the form does with the calendar."""

    def _form(self, **data):
        from types import SimpleNamespace

        from .forms import CounterForm

        terms = SimpleNamespace(
            fixed_pay=Decimal("90"),
            gig_hours=Decimal("8"),
            use_escrow=False,
            gig_dates=list(self.days),
            gig_date=self.days[0],
        )
        payload = {"fixed_pay": "90", "gig_hours": "8", "note": ""} | data
        return CounterForm(data=payload, terms=terms, worker=self.worker_profile)

    def test_it_offers_the_days_already_on_the_table(self):
        """Somebody dropping one day out of three unticks one box rather than
        typing three dates."""
        from types import SimpleNamespace

        from .forms import CounterForm

        terms = SimpleNamespace(
            fixed_pay=Decimal("90"),
            gig_hours=Decimal("8"),
            use_escrow=False,
            gig_dates=list(self.days),
            gig_date=self.days[0],
        )
        form = CounterForm(terms=terms, worker=self.worker_profile)
        self.assertEqual(
            form.fields["gig_dates"].initial,
            ", ".join(d.isoformat() for d in self.days),
        )

    def test_it_is_the_multi_day_calendar_and_not_a_single_date_box(self):
        from .forms import CounterForm

        attrs = CounterForm().fields["gig_dates"].widget.attrs
        self.assertIn("data-date-list", attrs)
        self.assertNotIn("data-date-single", attrs)

    def test_dropping_a_day_counts_as_a_change(self):
        """Otherwise the form would refuse it as "nothing to answer"."""
        keep = [self.days[0], self.days[2]]
        form = self._form(gig_dates=", ".join(d.isoformat() for d in keep))
        self.assertTrue(form.is_valid(), form.errors.as_text())

    def test_the_same_days_and_nothing_else_is_not_a_counter(self):
        form = self._form(gig_dates=", ".join(d.isoformat() for d in self.days))
        self.assertFalse(form.is_valid())

    def test_a_day_the_worker_has_sold_is_refused(self):
        Job.objects.create(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.carpentry,
            region=self.region,
            title="Elsewhere",
            description="Another site.",
            gig_date=self.days[1],
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("90"),
            state=JobState.ACCEPTED,
            assigned_worker=self.worker_profile,
        )
        form = self._form(gig_dates=", ".join(d.isoformat() for d in self.days))
        self.assertFalse(form.is_valid())
        self.assertIn("gig_dates", form.errors)

    def test_a_day_in_the_past_is_refused(self):
        gone = timezone.localdate() - timedelta(days=1)
        form = self._form(gig_dates=gone.isoformat())
        self.assertFalse(form.is_valid())
