"""Phase 4 tests.

Nothing here touches the network. :mod:`payments.gateway` is the only module
that talks to Stripe, so replacing its functions is enough to exercise every
rule that decides whether money moves, and how much.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import ClientProfile, WorkerProfile
from config import business_rules as rules
from core.models import Region, Trade
from core.state_machine import Actor, IllegalTransition, JobState
from jobs.models import Job, JobType

from . import gateway, services
from .models import EscrowPayment, EscrowStatus, StripeAccount, WebhookEvent

User = get_user_model()


class FeeArithmeticTests(TestCase):
    def test_the_fee_and_the_payout_always_sum_to_the_total(self):
        """A cent that belongs to nobody is a bug, at any amount."""
        for total in ["90", "0.01", "33.33", "100", "1234.57", "7.77"]:
            amount = Decimal(total)
            fee = rules.platform_fee_for(amount)
            payout = rules.worker_payout_for(amount)
            self.assertEqual(fee + payout, amount, f"split lost a cent at {total}")

    def test_the_default_cut_is_twelve_percent(self):
        self.assertEqual(rules.platform_fee_for(Decimal("90")), Decimal("10.80"))
        self.assertEqual(rules.worker_payout_for(Decimal("90")), Decimal("79.20"))

    def test_cents_conversion_rounds_half_up(self):
        self.assertEqual(gateway.to_cents(Decimal("10.80")), 1080)
        self.assertEqual(gateway.to_cents(Decimal("0.005")), 1)
        self.assertEqual(gateway.from_cents(1080), Decimal("10.80"))


class EscrowTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.filter(is_active=True).first()
        cls.trade = Trade.objects.get(slug="carpenter")

        cls.client_user = User.objects.create_user(email="client@example.com")
        cls.client_profile = ClientProfile.objects.create(
            user=cls.client_user, region=cls.region
        )
        cls.worker_user = User.objects.create_user(email="worker@example.com")
        cls.worker_profile = WorkerProfile.objects.create(
            user=cls.worker_user, region=cls.region
        )

    def make_gig(self, *, state=JobState.ACCEPTED, days_ahead=2, **overrides):
        defaults = dict(
            client=self.client_profile,
            job_type=JobType.GIG,
            trade=self.trade,
            region=self.region,
            title="Framing help",
            description="Second storey.",
            gig_date=timezone.localdate() + timedelta(days=days_ahead),
            gig_hours=Decimal("8"),
            fixed_pay=Decimal("90"),
            state=state,
            assigned_worker=self.worker_profile,
        )
        return Job.objects.create(**(defaults | overrides))

    def ready_account(self, worker=None):
        return StripeAccount.objects.create(
            worker=worker or self.worker_profile,
            account_id="acct_test_123",
            details_submitted=True,
            charges_enabled=True,
            payouts_enabled=True,
        )


class FundingBlockerTests(EscrowTestCase):
    def test_a_standing_position_is_never_funded_through_the_platform(self):
        job = Job.objects.create(
            client=self.client_profile,
            job_type=JobType.STANDING,
            trade=self.trade,
            region=self.region,
            title="Carpenter wanted",
            description="x",
            rate_type="hourly",
            rate_min=Decimal("30"),
            position_type="ongoing",
            state=JobState.ACCEPTED,
        )
        self.assertIn("Only gigs", services.funding_blocker(job))

    def test_a_job_with_nobody_on_it_cannot_be_funded(self):
        job = self.make_gig(state=JobState.POSTED, assigned_worker=None)
        self.assertIn("Pick a worker", services.funding_blocker(job))

    def test_funding_is_blocked_until_the_worker_can_receive_payouts(self):
        job = self.make_gig()
        self.assertIn("payouts", services.funding_blocker(job))

        account = self.ready_account()
        account.payouts_enabled = False
        account.save()
        self.assertIn("payouts", services.funding_blocker(job))

        account.payouts_enabled = True
        account.save()
        self.assertIsNone(services.funding_blocker(job))

    def test_funding_a_gig_further_out_than_a_hold_survives_is_refused(self):
        """Card authorisations expire; this is the guard, not a preference."""
        self.ready_account()
        job = self.make_gig(days_ahead=rules.ESCROW_AUTHORIZATION_MAX_DAYS + 3)
        blocker = services.funding_blocker(job)
        self.assertIsNotNone(blocker)
        self.assertIn("expire", blocker)

    def test_an_already_funded_job_is_past_the_funding_stage(self):
        self.ready_account()
        job = self.make_gig(state=JobState.ESCROW_HELD)
        self.assertIn("past the funding stage", services.funding_blocker(job))


class StartFundingTests(EscrowTestCase):
    def setUp(self):
        self.account = self.ready_account()
        self.job = self.make_gig()

    @patch("payments.gateway.create_checkout_session")
    def test_funding_freezes_the_split_and_sends_stripe_the_right_numbers(self, mock):
        mock.return_value = ("cs_test_1", "https://checkout.stripe.test/cs_test_1")

        url = services.start_funding(
            self.job, success_url="https://x/ok", cancel_url="https://x/no"
        )
        self.assertEqual(url, "https://checkout.stripe.test/cs_test_1")

        escrow = EscrowPayment.objects.get(job=self.job)
        self.assertEqual(escrow.amount, Decimal("90.00"))
        self.assertEqual(escrow.platform_fee, Decimal("10.80"))
        self.assertEqual(escrow.worker_payout, Decimal("79.20"))
        self.assertEqual(escrow.status, EscrowStatus.PENDING)

        kwargs = mock.call_args.kwargs
        self.assertEqual(kwargs["amount"], Decimal("90.00"))
        self.assertEqual(kwargs["platform_fee"], Decimal("10.80"))
        self.assertEqual(kwargs["destination_account_id"], "acct_test_123")
        self.assertEqual(kwargs["metadata"]["job_id"], str(self.job.pk))

    @patch("payments.gateway.create_checkout_session")
    def test_the_frozen_split_survives_a_later_fee_change(self, mock):
        """An in-flight gig settles on the terms both sides actually saw."""
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")
        escrow = EscrowPayment.objects.get(job=self.job)

        with patch.object(rules, "PLATFORM_FEE_PCT", Decimal("0.30")):
            escrow.refresh_from_db()
            self.assertEqual(escrow.platform_fee, Decimal("10.80"))
            self.assertEqual(escrow.worker_payout, Decimal("79.20"))

    @patch("payments.gateway.retrieve_session")
    @patch("payments.gateway.create_checkout_session")
    def test_abandoning_checkout_and_retrying_reuses_one_escrow_row(
        self, mock, live
    ):
        """The old session has expired, so a new one opens onto the same row."""
        live.return_value = {"status": "expired", "payment_status": "unpaid",
                             "payment_intent": None}
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")
        mock.return_value = ("cs_2", "https://checkout/2")
        services.start_funding(self.job, success_url="a", cancel_url="b")

        self.assertEqual(EscrowPayment.objects.filter(job=self.job).count(), 1)
        self.assertEqual(
            EscrowPayment.objects.get(job=self.job).checkout_session_id, "cs_2"
        )


class FundingAttemptTests(EscrowTestCase):
    """Starting to fund is a claim, not a read.

    Two requests a double-click apart both found a row that was not yet
    AUTHORIZED and both opened a Checkout Session. Each carries its own
    PaymentIntent, so a client who completed both put two holds on one card
    while this database recorded one — and nothing here would ever release the
    other. It would sit there until it expired.
    """

    def setUp(self):
        self.account = self.ready_account()
        self.job = self.make_gig()

    @patch("payments.gateway.retrieve_session")
    @patch("payments.gateway.create_checkout_session")
    def test_a_second_press_returns_the_checkout_already_open(self, mock, live):
        mock.return_value = ("cs_1", "https://checkout/1")
        first = services.start_funding(self.job, success_url="a", cancel_url="b")

        live.return_value = {"status": "open", "payment_status": "unpaid",
                             "payment_intent": None}
        second = services.start_funding(self.job, success_url="a", cancel_url="b")

        self.assertEqual(first, second)
        # The point of the whole exercise: one session, so one PaymentIntent,
        # so one hold on the card.
        self.assertEqual(mock.call_count, 1)

    @patch("payments.gateway.retrieve_session")
    @patch("payments.gateway.create_checkout_session")
    def test_a_session_already_paid_does_not_open_another(self, mock, live):
        """The webhook has not landed yet, and the client has already paid."""
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")

        live.return_value = {"status": "complete", "payment_status": "paid",
                             "payment_intent": "pi_live_1"}
        with self.assertRaises(services.EscrowError):
            services.start_funding(self.job, success_url="a", cancel_url="b")

        self.assertEqual(mock.call_count, 1)
        escrow = EscrowPayment.objects.get(job=self.job)
        # And the browser getting here first recorded it, rather than the page
        # sitting on a hold nobody had written down.
        self.assertEqual(escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(escrow.payment_intent_id, "pi_live_1")

    @patch("payments.gateway.create_checkout_session")
    def test_two_simultaneous_presses_send_stripe_the_same_key(self, mock):
        """The deterministic half of the race, without threads.

        Both requests read the same row before either has written, which is
        exactly what a double-click produces. They must compute the same
        idempotency key — that is what makes Stripe hand them one session
        instead of opening two.
        """
        mock.return_value = ("cs_1", "https://checkout/1")
        stale = EscrowPayment.objects.get_or_create(
            job=self.job,
            defaults={
                "worker": self.worker_profile,
                "amount": self.job.fixed_pay,
                "platform_fee": rules.platform_fee_for(self.job.fixed_pay),
                "worker_payout": rules.worker_payout_for(self.job.fixed_pay),
            },
        )[0]

        services.start_funding(self.job, success_url="a", cancel_url="b")
        first_key = mock.call_args.kwargs["idempotency_key"]

        # The second request is holding the row as it was before the first
        # wrote — the same read, a moment later.
        stale.refresh_from_db()
        self.assertEqual(stale.funding_attempts, 1)
        self.assertEqual(
            first_key, f"escrow:{stale.pk}:attempt:1"
        )

    @patch("payments.gateway.retrieve_session")
    @patch("payments.gateway.create_checkout_session")
    def test_a_genuinely_new_attempt_gets_a_new_key(self, mock, live):
        """Otherwise a client who abandoned yesterday could never start again."""
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")

        live.return_value = {"status": "expired", "payment_status": "unpaid",
                             "payment_intent": None}
        mock.return_value = ("cs_2", "https://checkout/2")
        services.start_funding(self.job, success_url="a", cancel_url="b")

        escrow = EscrowPayment.objects.get(job=self.job)
        self.assertEqual(escrow.funding_attempts, 2)
        self.assertEqual(
            mock.call_args.kwargs["idempotency_key"],
            f"escrow:{escrow.pk}:attempt:2",
        )

    @patch("payments.gateway.retrieve_session")
    @patch("payments.gateway.create_checkout_session")
    def test_a_session_stripe_cannot_find_is_treated_as_gone(self, mock, live):
        """A stale id must not make a job permanently unfundable."""
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")

        live.side_effect = RuntimeError("No such session")
        mock.return_value = ("cs_2", "https://checkout/2")
        url = services.start_funding(self.job, success_url="a", cancel_url="b")

        self.assertEqual(url, "https://checkout/2")

    @patch("payments.gateway.create_checkout_session")
    def test_funding_an_already_held_job_is_refused(self, mock):
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")
        escrow = EscrowPayment.objects.get(job=self.job)
        services.mark_authorized(escrow, "pi_test_1")

        with self.assertRaises(services.EscrowError):
            services.start_funding(self.job, success_url="a", cancel_url="b")


class AuthorizationTests(EscrowTestCase):
    def setUp(self):
        self.ready_account()
        self.job = self.make_gig()
        self.escrow = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
        )

    def test_authorising_moves_the_job_to_escrow_held(self):
        services.mark_authorized(self.escrow, "pi_test_1")
        self.job.refresh_from_db()
        self.escrow.refresh_from_db()
        self.assertEqual(self.job.state, JobState.ESCROW_HELD)
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(self.escrow.payment_intent_id, "pi_test_1")
        self.assertIsNotNone(self.escrow.authorized_at)

    def test_authorising_twice_is_a_no_op(self):
        """The webhook and the browser redirect race on every single payment."""
        services.mark_authorized(self.escrow, "pi_test_1")
        first_time = EscrowPayment.objects.get(pk=self.escrow.pk).authorized_at

        services.mark_authorized(self.escrow, "pi_test_1")
        self.escrow.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(self.escrow.authorized_at, first_time)
        self.assertEqual(self.job.state, JobState.ESCROW_HELD)


class ReleaseTests(EscrowTestCase):
    def setUp(self):
        self.ready_account()
        self.job = self.make_gig(state=JobState.COMPLETED)
        self.escrow = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
            status=EscrowStatus.AUTHORIZED,
            payment_intent_id="pi_test_1",
        )

    @patch("payments.gateway.capture_payment_intent")
    def test_releasing_captures_the_hold_and_finishes_the_job(self, mock):
        mock.return_value = {
            "id": "pi_test_1",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }
        services.release(self.escrow, actor=Actor.CLIENT)

        self.job.refresh_from_db()
        self.escrow.refresh_from_db()
        self.assertEqual(self.job.state, JobState.PAID_OUT)
        self.assertEqual(self.escrow.status, EscrowStatus.RELEASED)
        self.assertEqual(self.escrow.captured_amount, Decimal("90.00"))
        mock.assert_called_once_with("pi_test_1", amount=None)

    @patch("payments.gateway.capture_payment_intent")
    def test_a_lost_race_never_reaches_stripe(self, mock):
        """A client approving as the settlement cron fires.

        Both read an AUTHORIZED hold from their own instance and both would
        capture the same intent. Stripe refuses the second, so the money is
        safe either way — but the loser must not get that far, or the app takes
        a gateway exception on a job that was in fact paid correctly.
        """
        from payments.models import EscrowPayment

        # The window: assert_transition runs after release() has read the
        # escrow and before it claims it, which is where the other caller lands.
        real = services.assert_transition

        def settle_it_first(*args, **kwargs):
            result = real(*args, **kwargs)
            EscrowPayment.objects.filter(
                pk=self.escrow.pk, status=EscrowStatus.AUTHORIZED
            ).update(status=EscrowStatus.RELEASED)
            return result

        with patch.object(services, "assert_transition", settle_it_first):
            returned = services.release(self.escrow, actor=Actor.SYSTEM)

        mock.assert_not_called()
        self.assertEqual(returned.status, EscrowStatus.RELEASED)

    @patch("payments.gateway.capture_payment_intent")
    def test_a_partial_capture_recomputes_what_the_worker_nets(self, mock):
        """The prorated path phase 5 will use for a job that ended early."""
        mock.return_value = {
            "id": "pi_test_1",
            "status": "succeeded",
            "amount_received": Decimal("45.00"),
        }
        services.release(self.escrow, actor=Actor.CLIENT, amount=Decimal("45.00"))

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.captured_amount, Decimal("45.00"))
        self.assertEqual(self.escrow.net_to_worker, Decimal("39.60"))
        self.assertEqual(mock.call_args.kwargs["amount"], Decimal("45.00"))

    @patch("payments.gateway.capture_payment_intent")
    def test_releasing_more_than_was_held_is_refused(self, mock):
        with self.assertRaises(services.EscrowError):
            services.release(self.escrow, actor=Actor.CLIENT, amount=Decimal("500"))
        mock.assert_not_called()

    @patch("payments.gateway.capture_payment_intent")
    def test_releasing_twice_captures_only_once(self, mock):
        mock.return_value = {
            "id": "pi_test_1",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }
        services.release(self.escrow, actor=Actor.CLIENT)
        services.release(self.escrow, actor=Actor.CLIENT)
        mock.assert_called_once()

    @patch("payments.gateway.capture_payment_intent")
    def test_a_worker_cannot_release_their_own_payment(self, mock):
        with self.assertRaises(IllegalTransition):
            services.release(self.escrow, actor=Actor.WORKER)
        mock.assert_not_called()

    @patch("payments.gateway.capture_payment_intent")
    def test_money_cannot_be_released_straight_out_of_the_hold(self, mock):
        """ESCROW_HELD has no path to PAID_OUT — work has to happen first."""
        self.job.state = JobState.ESCROW_HELD
        self.job.save(update_fields=["state"])
        with self.assertRaises(IllegalTransition):
            services.release(self.escrow, actor=Actor.CLIENT)
        mock.assert_not_called()


class RefundTests(EscrowTestCase):
    def setUp(self):
        self.ready_account()
        self.job = self.make_gig(state=JobState.ESCROW_HELD)
        self.escrow = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
            status=EscrowStatus.AUTHORIZED,
            payment_intent_id="pi_test_1",
        )

    @patch("payments.gateway.cancel_payment_intent")
    def test_calling_off_a_funded_gig_releases_the_hold(self, mock):
        mock.return_value = {"id": "pi_test_1", "status": "canceled"}
        services.refund(self.escrow, actor=Actor.CLIENT)

        self.job.refresh_from_db()
        self.escrow.refresh_from_db()
        self.assertEqual(self.job.state, JobState.REFUNDED)
        self.assertEqual(self.escrow.status, EscrowStatus.REFUNDED)
        mock.assert_called_once_with("pi_test_1")

    @patch("payments.gateway.cancel_payment_intent")
    def test_a_worker_cannot_refund_the_client(self, mock):
        with self.assertRaises(IllegalTransition):
            services.refund(self.escrow, actor=Actor.WORKER)
        mock.assert_not_called()


@override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
class ConnectAccountTests(EscrowTestCase):
    """Creating the worker's Stripe account, without leaving one behind.

    Read-then-create with a network call in the middle is not one operation.
    Two requests both find no local account and both ask Stripe for one; the
    OneToOne rejects the second insert *after* Stripe has already opened the
    account, and nothing in this database will ever point at it again.
    """

    @patch("payments.gateway.create_express_account")
    def test_the_account_is_opened_in_the_regions_country(self, mock):
        """Not "US" for everybody, which is what the default argument said."""
        mock.return_value = "acct_new"
        self.region.country = "GR"
        self.region.save(update_fields=["country"])

        services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(mock.call_args.kwargs["country"], "GR")

    @patch("payments.gateway.create_express_account")
    def test_the_request_carries_an_idempotency_key(self, mock):
        """So two concurrent creations get the same account from Stripe."""
        mock.return_value = "acct_new"
        services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(
            mock.call_args.kwargs["idempotency_key"],
            f"connect-account:{self.worker_profile.pk}",
        )

    @patch("payments.gateway.create_express_account")
    def test_losing_the_insert_returns_the_row_that_won(self, mock):
        """The race itself, made deterministic.

        The row appears *while we are talking to Stripe* — which is the only
        window there is, and the one a read-then-create cannot see. Our insert
        then loses to the OneToOne, and the caller must come away with the
        winner's account rather than with an IntegrityError.
        """

        def stripe_answers_slowly(**kwargs):
            StripeAccount.objects.create(
                worker=self.worker_profile, account_id="acct_winner"
            )
            return "acct_ours"

        mock.side_effect = stripe_answers_slowly

        account = services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(account.account_id, "acct_winner")
        self.assertEqual(StripeAccount.objects.count(), 1)

    @patch("payments.gateway.create_express_account")
    def test_an_existing_account_is_never_recreated(self, mock):
        StripeAccount.objects.create(
            worker=self.worker_profile, account_id="acct_existing"
        )
        account = services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(account.account_id, "acct_existing")
        mock.assert_not_called()


class WebhookClaimTests(EscrowTestCase):
    """The event id is claimed before the work, not recorded after it.

    "Does this exist?" followed by "do the work" is two steps with a gap, and
    two deliveries of the same event — a retry arriving while the first is
    still running — both find nothing and both proceed. The insert is one
    atomic claim, so exactly one of them wins.
    """

    def setUp(self):
        self.ready_account()
        self.job = self.make_gig()
        self.escrow = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
        )

    def event(self, event_id="evt_claim"):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"escrow_id": str(self.escrow.pk)},
                    "payment_intent": "pi_claimed",
                }
            },
        }

    def post(self, event):
        with patch("payments.gateway.construct_event", return_value=event):
            return self.client.post(
                reverse("payments:webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )

    def test_the_claim_exists_before_the_work_is_done(self):
        """Proved from inside the work: by then the row is already there.

        Which is the whole difference. A duplicate arriving at this moment
        finds the claim taken and stops, where before it found nothing.
        """
        seen = {}

        def spy(escrow, intent):
            seen["claimed"] = WebhookEvent.objects.filter(pk="evt_claim").exists()
            return escrow

        with patch("payments.services.mark_authorized", side_effect=spy):
            self.post(self.event())

        self.assertTrue(seen["claimed"])

    def test_a_replay_does_no_work_at_all(self):
        with patch("payments.services.mark_authorized") as work:
            self.post(self.event())
            self.post(self.event())

        self.assertEqual(work.call_count, 1)
        self.assertEqual(WebhookEvent.objects.filter(pk="evt_claim").count(), 1)

    def test_a_failed_run_gives_the_claim_back(self):
        """Otherwise the event is on record as handled and nothing happened —
        worse than doing it twice, because the retry that would have fixed it
        never comes."""
        with patch(
            "payments.services.mark_authorized", side_effect=RuntimeError("boom")
        ):
            response = self.post(self.event())

        self.assertEqual(response.status_code, 500)
        self.assertFalse(WebhookEvent.objects.filter(pk="evt_claim").exists())

    def test_and_stripes_retry_then_does_the_work_for_real(self):
        with patch(
            "payments.services.mark_authorized", side_effect=RuntimeError("boom")
        ):
            self.post(self.event())

        response = self.post(self.event())

        self.assertEqual(response.status_code, 200)
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)


class WebhookTests(EscrowTestCase):
    def setUp(self):
        self.ready_account()
        self.job = self.make_gig()
        self.escrow = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
        )

    def post_event(self, event):
        with patch("payments.gateway.construct_event", return_value=event):
            return self.client.post(
                reverse("payments:webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )

    def checkout_event(self, event_id="evt_1"):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "payment_intent": "pi_test_1",
                    "metadata": {
                        "job_id": str(self.job.pk),
                        "escrow_id": str(self.escrow.pk),
                    },
                }
            },
        }

    def test_a_completed_checkout_puts_the_job_into_escrow_held(self):
        response = self.post_event(self.checkout_event())
        self.assertEqual(response.status_code, 200)

        self.job.refresh_from_db()
        self.escrow.refresh_from_db()
        self.assertEqual(self.job.state, JobState.ESCROW_HELD)
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)

    def test_a_replayed_event_changes_nothing(self):
        """Stripe retries. Releasing or re-authorising twice is unrecoverable."""
        self.post_event(self.checkout_event(event_id="evt_dup"))
        first = EscrowPayment.objects.get(pk=self.escrow.pk).authorized_at

        response = self.post_event(self.checkout_event(event_id="evt_dup"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            EscrowPayment.objects.get(pk=self.escrow.pk).authorized_at, first
        )
        self.assertEqual(WebhookEvent.objects.filter(pk="evt_dup").count(), 1)

    def test_an_unsigned_webhook_is_rejected(self):
        with patch(
            "payments.gateway.construct_event", side_effect=ValueError("bad sig")
        ):
            response = self.client.post(
                reverse("payments:webhook"),
                data=b"{}",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, JobState.ACCEPTED)

    def test_a_failed_payment_is_recorded_with_its_reason(self):
        self.post_event(
            {
                "id": "evt_fail",
                "type": "payment_intent.payment_failed",
                "data": {
                    "object": {
                        "id": "pi_test_1",
                        "metadata": {"escrow_id": str(self.escrow.pk)},
                        "last_payment_error": {"message": "Your card was declined."},
                    }
                },
            }
        )
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.FAILED)
        self.assertIn("declined", self.escrow.last_error)

    def test_account_updated_refreshes_the_cached_payout_flags(self):
        account = StripeAccount.objects.get(worker=self.worker_profile)
        account.payouts_enabled = False
        account.save()

        self.post_event(
            {
                "id": "evt_acct",
                "type": "account.updated",
                "data": {
                    "object": {
                        "id": account.account_id,
                        "details_submitted": True,
                        "charges_enabled": True,
                        "payouts_enabled": True,
                    }
                },
            }
        )
        account.refresh_from_db()
        self.assertTrue(account.payouts_enabled)


class PayoutViewTests(EscrowTestCase):
    def test_payouts_page_says_so_when_stripe_is_not_configured(self):
        self.client.force_login(self.worker_user)
        with override_settings(STRIPE_SECRET_KEY=""):
            response = self.client.get(reverse("payments:payouts"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["stripe_configured"])

    def test_a_client_only_account_is_sent_to_add_a_worker_profile(self):
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("payments:payouts"))
        self.assertRedirects(response, reverse("accounts:select_role"))

    def test_an_outsider_cannot_see_a_jobs_money(self):
        self.ready_account()
        job = self.make_gig()
        outsider = User.objects.create_user(email="nosy@example.com")
        ClientProfile.objects.create(user=outsider, region=self.region)
        self.client.force_login(outsider)

        response = self.client.get(reverse("payments:escrow", args=[job.pk]))
        # The board: the job itself is no longer readable by an outsider
        # either, so redirecting there would be a second refusal.
        self.assertRedirects(response, reverse("jobs:list"))

    def test_the_worker_on_the_job_can_see_its_money_view(self):
        self.ready_account()
        job = self.make_gig()
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("payments:escrow", args=[job.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_client"])
