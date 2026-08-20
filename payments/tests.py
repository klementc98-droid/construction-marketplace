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
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import ClientProfile, WorkerProfile
from config import business_rules as rules
from core.models import Region, Trade
from core.state_machine import Actor, IllegalTransition, JobState
from jobs.models import Job, JobType

from . import gateway, reconciliation, services
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

    @patch("payments.gateway.create_checkout_session", autospec=True)
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

    @patch("payments.gateway.create_checkout_session", autospec=True)
    def test_the_frozen_split_survives_a_later_fee_change(self, mock):
        """An in-flight gig settles on the terms both sides actually saw."""
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")
        escrow = EscrowPayment.objects.get(job=self.job)

        with patch.object(rules, "PLATFORM_FEE_PCT", Decimal("0.30")):
            escrow.refresh_from_db()
            self.assertEqual(escrow.platform_fee, Decimal("10.80"))
            self.assertEqual(escrow.worker_payout, Decimal("79.20"))

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
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

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
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

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
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

    @patch("payments.gateway.create_checkout_session", autospec=True)
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

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
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

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
    def test_a_session_stripe_cannot_find_is_treated_as_gone(self, mock, live):
        """A stale id must not make a job permanently unfundable."""
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")

        live.side_effect = gateway.ObjectMissing("No such session")
        mock.return_value = ("cs_2", "https://checkout/2")
        url = services.start_funding(self.job, success_url="a", cancel_url="b")

        self.assertEqual(url, "https://checkout/2")

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
    def test_but_stripe_not_answering_opens_nothing(self, mock, live):
        """A timeout is not an answer, and used to be read as one.

        The catch-all here turned "Stripe did not reply" into "there is no
        session", and the next line opened a second one — re-making the hole
        the idempotency key was added to close, on the one occasion when
        Stripe may already be holding money against the first.
        """
        mock.return_value = ("cs_1", "https://checkout/1")
        services.start_funding(self.job, success_url="a", cancel_url="b")
        mock.reset_mock()

        live.side_effect = RuntimeError("connection timed out")

        with self.assertRaises(RuntimeError):
            services.start_funding(self.job, success_url="a", cancel_url="b")

        mock.assert_not_called()
        self.escrow_row().refresh_from_db()
        self.assertEqual(self.escrow_row().funding_attempts, 1)

    def escrow_row(self):
        return EscrowPayment.objects.get(job=self.job)

    @patch("payments.gateway.create_checkout_session", autospec=True)
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


class GatewayContractTests(TestCase):
    """The service layer and the gateway have to agree, and mocks hide it.

    Every test in this file replaces the gateway with a mock, and a mock takes
    any arguments you give it. So a service that calls the gateway with a
    keyword the real function does not have passes the entire suite and fails
    on the first real call — the payment and payout paths, 500, in production,
    on the one code path nobody can exercise locally without keys.

    That is not hypothetical: it is exactly what an outside reviewer thought
    they had found here, and the only reason they were wrong is that the
    signature had in fact been updated. Nothing was enforcing it.

    Two things enforce it now. Every patch in the suite is autospec'd, so a
    mocked call is held to the real signature. And this, which checks the calls
    the services actually make against the functions that will actually run.
    """

    def test_every_gateway_call_the_services_make_is_valid(self):
        import inspect

        from . import gateway

        # The exact keyword sets the service layer uses today. Written out
        # rather than introspected, because the point is to fail when somebody
        # changes one side and not the other — and a check that derives both
        # sides from the same source would never notice.
        calls = {
            "create_express_account": {
                "email": "a@b.c",
                "country": "GR",
                "idempotency_key": "connect-account:1",
                "metadata": {"worker_id": "1"},
            },
            "create_checkout_session": {
                "job_title": "t",
                "amount": Decimal("90"),
                "platform_fee": Decimal("10.80"),
                "destination_account_id": "acct_1",
                "success_url": "u",
                "cancel_url": "u",
                "metadata": {},
                "idempotency_key": "escrow:1:attempt:1",
            },
            "capture_payment_intent": {
                "amount": Decimal("60"),
                "application_fee": Decimal("7.20"),
            },
            "retrieve_session": {},
            "retrieve_payment_intent": {},
            "cancel_payment_intent": {},
            "find_account_for": {},
        }

        positional = {
            "capture_payment_intent": ("pi_1",),
            "retrieve_session": ("cs_1",),
            "retrieve_payment_intent": ("pi_1",),
            "cancel_payment_intent": ("pi_1",),
            "find_account_for": (1,),
        }

        for name, kwargs in calls.items():
            with self.subTest(call=name):
                fn = getattr(gateway, name)
                inspect.signature(fn).bind(*positional.get(name, ()), **kwargs)

    def test_an_autospecced_mock_refuses_an_argument_that_does_not_exist(self):
        """Proof that the suite's mocks now catch what they used to hide."""
        from unittest.mock import patch

        with patch("payments.gateway.cancel_payment_intent", autospec=True) as mock:
            from . import gateway

            with self.assertRaises(TypeError):
                gateway.cancel_payment_intent("pi_1", nonsense=True)
            mock.assert_not_called()


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

    @patch("payments.gateway.capture_payment_intent", autospec=True)
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
        mock.assert_called_once_with(
            "pi_test_1", amount=None, application_fee=Decimal("10.80")
        )

    @patch("payments.gateway.capture_payment_intent", autospec=True)
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

    @patch("payments.gateway.capture_payment_intent", autospec=True)
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

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_releasing_more_than_was_held_is_refused(self, mock):
        with self.assertRaises(services.EscrowError):
            services.release(self.escrow, actor=Actor.CLIENT, amount=Decimal("500"))
        mock.assert_not_called()

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_releasing_twice_captures_only_once(self, mock):
        mock.return_value = {
            "id": "pi_test_1",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }
        services.release(self.escrow, actor=Actor.CLIENT)
        services.release(self.escrow, actor=Actor.CLIENT)
        mock.assert_called_once()

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_a_worker_cannot_release_their_own_payment(self, mock):
        with self.assertRaises(IllegalTransition):
            services.release(self.escrow, actor=Actor.WORKER)
        mock.assert_not_called()

    @patch("payments.gateway.capture_payment_intent", autospec=True)
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

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    def test_calling_off_a_funded_gig_releases_the_hold(self, mock):
        mock.return_value = {"id": "pi_test_1", "status": "canceled"}
        services.refund(self.escrow, actor=Actor.CLIENT)

        self.job.refresh_from_db()
        self.escrow.refresh_from_db()
        self.assertEqual(self.job.state, JobState.REFUNDED)
        self.assertEqual(self.escrow.status, EscrowStatus.REFUNDED)
        mock.assert_called_once_with("pi_test_1")

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    def test_a_worker_cannot_refund_the_client(self, mock):
        with self.assertRaises(IllegalTransition):
            services.refund(self.escrow, actor=Actor.WORKER)
        mock.assert_not_called()


@override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
class TwoRowsMustAgreeTests(EscrowTestCase):
    """The escrow and the job are two rows, and both claims decide something.

    Only one of them was being decided. The escrow was claimed with a
    conditional UPDATE and checked; the job was moved with one whose answer was
    discarded — so when the job moved underneath, the code carried on as if it
    had not. The states that produced are individually plausible and jointly
    impossible, which is why nothing else would ever notice.

    Each race here is staged the same way: the function is handed an instance
    that says one thing while the database says another. That is precisely what
    a concurrent write leaves behind, and unlike threads it happens the same
    way every run.
    """

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
            payment_intent_id="pi_held",
            authorized_at=timezone.now(),
        )

    def stale(self, job, *, reads_as, actually):
        """A read taken before somebody else moved the row."""
        Job.objects.filter(pk=job.pk).update(state=actually)
        held = Job.objects.get(pk=job.pk)
        held.state = reads_as
        return held

    def held_job(self, state=JobState.ACCEPTED):
        """An escrow on a fresh job, for the authorising cases."""
        return EscrowPayment.objects.create(
            job=self.make_gig(state=state, days_ahead=1),
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
        )

    # -- releasing -------------------------------------------------------

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_a_dispute_landing_first_stops_the_release(self, capture):
        """The case that used to capture money into a disputed job."""
        held = self.stale(
            self.job, reads_as=JobState.COMPLETED, actually=JobState.DISPUTED
        )

        with patch.object(Job.objects, "get", return_value=held):
            with self.assertRaises(services.EscrowError):
                services.release(self.escrow, actor=Actor.CLIENT)

        capture.assert_not_called()
        self.escrow.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(self.job.state, JobState.DISPUTED)

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_nothing_is_half_done_when_the_release_is_refused(self, capture):
        """Both rows are as they were: the whole thing rolls back."""
        held = self.stale(
            self.job, reads_as=JobState.COMPLETED, actually=JobState.DISPUTED
        )
        with patch.object(Job.objects, "get", return_value=held):
            with self.assertRaises(services.EscrowError):
                services.release(self.escrow, actor=Actor.CLIENT)

        self.escrow.refresh_from_db()
        self.assertIsNone(self.escrow.released_at)
        self.assertIsNone(self.escrow.captured_amount)

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_the_ordinary_release_still_moves_both_rows(self, capture):
        capture.return_value = {"amount_received": Decimal("90.00")}

        services.release(self.escrow, actor=Actor.CLIENT)

        self.escrow.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.RELEASED)
        self.assertEqual(self.job.state, JobState.PAID_OUT)

    # -- refunding -------------------------------------------------------

    @patch("payments.gateway.cancel_payment_intent", autospec=True)
    def test_a_job_that_moved_stops_the_refund(self, cancel):
        """Losing here costs nothing, because the hold is still untouched."""
        held = self.stale(
            self.job, reads_as=JobState.DISPUTED, actually=JobState.PAID_OUT
        )

        with patch.object(Job.objects, "get", return_value=held):
            with self.assertRaises(services.EscrowError):
                services.refund(self.escrow, actor=Actor.ADMIN)

        cancel.assert_not_called()
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)

    # -- authorising -----------------------------------------------------

    def test_a_hold_is_recorded_even_when_the_job_moved_under_it(self):
        """The money exists whatever the job says, so the record must too.

        An authorisation on a real card with nothing in this database pointing
        at it is the one outcome nobody can clean up afterwards.
        """
        escrow = self.held_job()
        held = self.stale(
            escrow.job, reads_as=JobState.ACCEPTED, actually=JobState.EXPIRED
        )

        with patch.object(Job.objects, "get", return_value=held):
            services.mark_authorized(escrow, "pi_live")

        escrow.refresh_from_db()
        self.assertEqual(escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(escrow.payment_intent_id, "pi_live")

    def test_but_the_job_is_not_claimed_to_be_funded(self):
        escrow = self.held_job()
        held = self.stale(
            escrow.job, reads_as=JobState.ACCEPTED, actually=JobState.EXPIRED
        )

        with patch.object(Job.objects, "get", return_value=held):
            services.mark_authorized(escrow, "pi_live")

        self.assertEqual(
            Job.objects.get(pk=escrow.job_id).state, JobState.EXPIRED
        )

    def test_and_the_divergence_is_written_down(self):
        """Nothing else will notice: both rows are individually plausible."""
        escrow = self.held_job()
        held = self.stale(
            escrow.job, reads_as=JobState.ACCEPTED, actually=JobState.EXPIRED
        )

        with patch.object(Job.objects, "get", return_value=held):
            with self.assertLogs("payments.services", level="ERROR"):
                services.mark_authorized(escrow, "pi_live")

        escrow.refresh_from_db()
        self.assertIn("expired", escrow.last_error)

    def test_no_money_is_held_email_on_a_job_that_moved(self):
        """That sentence on an expired job is an invitation to turn up."""
        from notifications.models import Notification

        escrow = self.held_job()
        held = self.stale(
            escrow.job, reads_as=JobState.ACCEPTED, actually=JobState.EXPIRED
        )

        before = Notification.objects.count()
        with patch.object(Job.objects, "get", return_value=held):
            services.mark_authorized(escrow, "pi_live")

        self.assertEqual(Notification.objects.count(), before)


class MultiDaySettlementTests(EscrowTestCase):
    """A week is several captures, and a rollback cannot reach money.

    approve() wrapped the whole booking in one transaction, which reads like
    safety and was the bug: Monday captured at Stripe, Tuesday failed, the
    transaction unwound — and Monday's money was gone while the database had
    forgotten taking it. The next run would find Monday unsettled and capture
    it a second time.
    """

    def booking(self, days=3):
        """A booking with a funded, finished day per date."""
        import uuid

        from worklog.models import Completion

        group = uuid.uuid4()
        made = []
        for n in range(days):
            job = self.make_gig(
                state=JobState.COMPLETED,
                days_ahead=-(days - n),
                offer_group=group,
                use_escrow=True,
            )
            EscrowPayment.objects.create(
                job=job,
                worker=self.worker_profile,
                amount=Decimal("90"),
                platform_fee=Decimal("10.80"),
                worker_payout=Decimal("79.20"),
                status=EscrowStatus.AUTHORIZED,
                payment_intent_id=f"pi_{n}",
                authorized_at=timezone.now(),
            )
            Completion.objects.create(
                job=job,
                hours_worked=Decimal("8"),
                payable_amount=Decimal("90"),
                settles_at=timezone.now(),
            )
            made.append(job)
        return made

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_a_day_that_fails_does_not_unpay_the_days_before_it(self, capture):
        """The finding, and the reason the transaction had to go.

        Stripe keeps Monday's money whatever this database decides afterwards,
        so the only honest thing the database can do is remember taking it.
        """
        from worklog import services as worklog_services
        from worklog.models import Completion

        days = self.booking(3)
        capture.side_effect = [
            {"amount_received": Decimal("90.00")},
            RuntimeError("card network refused"),
            {"amount_received": Decimal("90.00")},
        ]

        with self.assertRaises(Exception):
            worklog_services.approve(days[0], self.client_user)

        first = EscrowPayment.objects.get(job=days[0])
        self.assertEqual(first.status, EscrowStatus.RELEASED)
        self.assertTrue(
            Completion.objects.get(job=days[0]).settled_at,
            "the day whose money moved must stay settled",
        )

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_the_failed_day_and_the_ones_after_it_are_untouched(self, capture):
        days = self.booking(3)
        capture.side_effect = [
            {"amount_received": Decimal("90.00")},
            RuntimeError("card network refused"),
            {"amount_received": Decimal("90.00")},
        ]

        from worklog import services as worklog_services

        with self.assertRaises(Exception):
            worklog_services.approve(days[0], self.client_user)

        for job in days[1:]:
            self.assertEqual(
                EscrowPayment.objects.get(job=job).status,
                EscrowStatus.AUTHORIZED,
            )

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_the_refusal_says_what_was_paid(self, capture):
        """"Something failed" on a week of work is not an answer."""
        days = self.booking(3)
        capture.side_effect = [
            {"amount_received": Decimal("90.00")},
            RuntimeError("card network refused"),
        ]

        from worklog import services as worklog_services

        with self.assertRaises(services.EscrowError) as caught:
            worklog_services.approve(days[0], self.client_user)

        self.assertIn("Paid 1 of 3 days", str(caught.exception))

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_approving_again_picks_up_where_it_stopped(self, capture):
        """The day already paid is not captured a second time."""
        days = self.booking(3)
        capture.side_effect = [
            {"amount_received": Decimal("90.00")},
            RuntimeError("card network refused"),
        ]

        from worklog import services as worklog_services

        with self.assertRaises(Exception):
            worklog_services.approve(days[0], self.client_user)

        capture.reset_mock()
        capture.side_effect = None
        capture.return_value = {"amount_received": Decimal("90.00")}
        worklog_services.approve(days[0], self.client_user)

        # Two days remained; the first is not touched again.
        self.assertEqual(capture.call_count, 2)
        for job in days:
            self.assertEqual(
                EscrowPayment.objects.get(job=job).status, EscrowStatus.RELEASED
            )

    @patch("payments.gateway.capture_payment_intent", autospec=True)
    def test_the_whole_week_settles_when_nothing_goes_wrong(self, capture):
        days = self.booking(3)
        capture.return_value = {"amount_received": Decimal("90.00")}

        from worklog import services as worklog_services

        worklog_services.approve(days[0], self.client_user)

        for job in days:
            self.assertEqual(
                EscrowPayment.objects.get(job=job).status, EscrowStatus.RELEASED
            )


class ClaimBeforeStripeTests(EscrowTestCase):
    """The attempt is claimed locally before the external call, like the rest.

    Every other money path here claims first: nothing external happens until
    the database has said who is doing it. Funding could not, while the
    idempotency key was *derived* from the counter — claiming first would leave
    the caller that loses with no session to hand back, because the winner has
    not been to Stripe yet. Writing the key down is what allows both.
    """

    def setUp(self):
        self.account = self.ready_account()
        self.job = self.make_gig()

    def fund(self):
        return services.start_funding(
            self.job, success_url="a", cancel_url="b"
        )

    @patch("payments.gateway.create_checkout_session", autospec=True)
    def test_the_attempt_is_recorded_before_stripe_is_called(self, mock):
        """Read from inside the call: by then the row already says so."""
        seen = {}

        def look(**kwargs):
            row = EscrowPayment.objects.get(job=self.job)
            seen["attempts"] = row.funding_attempts
            seen["key"] = row.funding_key
            return ("cs_1", "https://checkout/1")

        mock.side_effect = look
        self.fund()

        self.assertEqual(seen["attempts"], 1)
        self.assertEqual(seen["key"], mock.call_args.kwargs["idempotency_key"])

    @patch("payments.gateway.create_checkout_session", autospec=True)
    def test_a_caller_that_loses_the_claim_uses_the_key_that_won(self, mock):
        """Same key, so Stripe hands both the same session — one hold."""
        mock.return_value = ("cs_1", "https://checkout/1")
        row = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=self.job.fixed_pay,
            platform_fee=rules.platform_fee_for(self.job.fixed_pay),
            worker_payout=rules.worker_payout_for(self.job.fixed_pay),
        )
        stale = EscrowPayment.objects.get(pk=row.pk)   # attempts still 0

        # Somebody else claims attempt 1 in the gap between this request's read
        # and its write. Handing the view the older read is what reproduces
        # that gap; without it the function simply reads the new value and is
        # not in a race at all.
        EscrowPayment.objects.filter(pk=row.pk).update(
            funding_attempts=1, funding_key=f"escrow:{row.pk}:attempt:1"
        )

        with patch.object(
            EscrowPayment.objects, "get_or_create", return_value=(stale, False)
        ):
            self.fund()

        self.assertEqual(
            mock.call_args.kwargs["idempotency_key"],
            f"escrow:{row.pk}:attempt:1",
            "the loser must send the winner's key, not one of its own",
        )

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
    def test_a_run_that_died_after_reaching_stripe_leaves_its_key_behind(
        self, mock, live
    ):
        """The scenario an outside review raised, and the reason for the column.

        Stripe created the session; the process died before recording it. The
        next attempt must not open a second one — and derived keys made that a
        coincidence of reading the same counter rather than a guarantee.
        """
        mock.side_effect = RuntimeError("died after Stripe answered")
        with self.assertRaises(RuntimeError):
            self.fund()

        row = EscrowPayment.objects.get(job=self.job)
        self.assertEqual(row.funding_key, f"escrow:{row.pk}:attempt:1")
        self.assertEqual(row.checkout_session_id, "", "nothing was recorded")

        # The retry finds no usable checkout and reuses the recorded key.
        live.side_effect = gateway.ObjectMissing("never recorded")
        mock.side_effect = None
        mock.return_value = ("cs_from_stripe", "https://checkout/1")
        self.fund()

        self.assertEqual(
            mock.call_args.kwargs["idempotency_key"],
            f"escrow:{row.pk}:attempt:2",
        )

    @patch("payments.gateway.retrieve_session", autospec=True)
    @patch("payments.gateway.create_checkout_session", autospec=True)
    def test_a_genuinely_new_attempt_still_gets_its_own_key(self, mock, live):
        mock.return_value = ("cs_1", "https://checkout/1")
        self.fund()

        live.return_value = {"status": "expired", "payment_status": "unpaid",
                             "payment_intent": None}
        mock.return_value = ("cs_2", "https://checkout/2")
        self.fund()

        row = EscrowPayment.objects.get(job=self.job)
        self.assertEqual(row.funding_attempts, 2)
        self.assertEqual(row.funding_key, f"escrow:{row.pk}:attempt:2")


class ConnectAccountTests(EscrowTestCase):
    """Creating the worker's Stripe account, without leaving one behind.

    Read-then-create with a network call in the middle is not one operation.
    Two requests both find no local account and both ask Stripe for one; the
    OneToOne rejects the second insert *after* Stripe has already opened the
    account, and nothing in this database will ever point at it again.
    """

    @patch("payments.gateway.create_express_account", autospec=True)
    def test_the_account_is_opened_in_the_regions_country(self, mock):
        """Not "US" for everybody, which is what the default argument said."""
        mock.return_value = "acct_new"
        self.region.country = "GR"
        self.region.save(update_fields=["country"])

        services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(mock.call_args.kwargs["country"], "GR")

    @patch("payments.gateway.create_express_account", autospec=True)
    def test_the_request_carries_an_idempotency_key(self, mock):
        """So two concurrent creations get the same account from Stripe."""
        mock.return_value = "acct_new"
        services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(
            mock.call_args.kwargs["idempotency_key"],
            f"connect-account:{self.worker_profile.pk}",
        )

    @patch("payments.gateway.find_account_for", return_value=None, autospec=True)
    @patch("payments.gateway.create_express_account", autospec=True)
    def test_losing_the_row_claim_still_ends_with_one_account(self, mock, find):
        """Two requests both claim; one loses and reads the winner's row.

        The claim now happens before Stripe is asked, so this is the race that
        remains — and both callers still end up naming the same account,
        because the idempotency key made Stripe hand them the same one.
        """
        mock.return_value = "acct_same"
        winner = StripeAccount.objects.create(worker=self.worker_profile)

        with patch.object(
            StripeAccount.objects, "create", side_effect=IntegrityError("dup")
        ):
            account = services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(account.pk, winner.pk)
        self.assertEqual(StripeAccount.objects.count(), 1)

    @patch("payments.gateway.find_account_for", autospec=True)
    @patch("payments.gateway.create_express_account", autospec=True)
    def test_a_crash_before_the_id_was_written_adopts_the_lost_account(
        self, create, find
    ):
        """The 24-hour hole, closed by a record rather than by a key.

        Stripe forgets an idempotency key after a day. A process that died
        between Stripe answering and the insert used to leave an account nobody
        could name, and the retry the next day opened a second one. The blank
        row is what makes the first one findable.
        """
        StripeAccount.objects.create(worker=self.worker_profile)
        find.return_value = "acct_lost_and_found"

        account = services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(account.account_id, "acct_lost_and_found")
        create.assert_not_called()
        self.assertEqual(StripeAccount.objects.count(), 1)

    @patch("payments.gateway.find_account_for", autospec=True)
    @patch("payments.gateway.create_express_account", autospec=True)
    def test_and_opens_a_new_one_when_stripe_holds_nothing(self, create, find):
        StripeAccount.objects.create(worker=self.worker_profile)
        find.return_value = None
        create.return_value = "acct_fresh"

        account = services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(account.account_id, "acct_fresh")

    @patch("payments.gateway.find_account_for", autospec=True)
    @patch("payments.gateway.create_express_account", autospec=True)
    def test_a_failed_lookup_does_not_block_the_worker(self, create, find):
        """A recovery nicety must not stand between somebody and getting paid."""
        StripeAccount.objects.create(worker=self.worker_profile)
        find.side_effect = RuntimeError("listing timed out")
        create.return_value = "acct_fresh"

        account = services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(account.account_id, "acct_fresh")

    @patch("payments.gateway.create_express_account", autospec=True)
    def test_an_existing_account_is_never_recreated(self, mock):
        StripeAccount.objects.create(
            worker=self.worker_profile, account_id="acct_existing"
        )
        account = services.ensure_stripe_account(self.worker_profile)

        self.assertEqual(account.account_id, "acct_existing")
        mock.assert_not_called()


class OutOfOrderEventTests(EscrowTestCase):
    """Stripe does not promise its events arrive in the order they happened.

    Which makes one of them able to destroy money that exists: a PaymentIntent
    that fails one attempt and succeeds on the next produces both a
    payment_failed and an amount_capturable_updated, and nothing says which
    lands first. Written unconditionally, the late failure took an AUTHORIZED
    row to FAILED with the hold sitting live at Stripe — and nothing could
    reach it afterwards, because release refuses anything but AUTHORIZED and
    reconciliation did not look at FAILED rows at all.
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

    def test_a_late_failure_cannot_unfund_a_held_payment(self):
        services.mark_authorized(self.escrow, "pi_second_attempt")

        services.mark_failed(self.escrow, "The first attempt was declined.")

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)

    def test_nor_reopen_a_payment_already_released(self):
        EscrowPayment.objects.filter(pk=self.escrow.pk).update(
            status=EscrowStatus.RELEASED
        )
        self.escrow.refresh_from_db()

        services.mark_failed(self.escrow, "stale")

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.RELEASED)

    def test_a_failure_on_a_row_still_waiting_is_recorded(self):
        """The ordinary case still has to work."""
        services.mark_failed(self.escrow, "Card declined.")

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.FAILED)
        self.assertIn("declined", self.escrow.last_error)

    def test_the_capturable_event_can_still_arrive_after_a_failure(self):
        """Failure first, success second: the row has to be able to recover."""
        services.mark_failed(self.escrow, "First attempt declined.")

        services.mark_authorized(self.escrow, "pi_second_attempt")

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)


class MistakenFailureTests(EscrowTestCase):
    """The net under it, for rows written before the guard existed."""

    def setUp(self):
        self.ready_account()
        self.job = self.make_gig(state=JobState.ESCROW_HELD)
        self.escrow = EscrowPayment.objects.create(
            job=self.job,
            worker=self.worker_profile,
            amount=Decimal("90"),
            platform_fee=Decimal("10.80"),
            worker_payout=Decimal("79.20"),
            status=EscrowStatus.FAILED,
            payment_intent_id="pi_actually_held",
            last_error="payment_failed arrived late",
        )

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_row_marked_failed_over_live_money_is_put_back(self, intent):
        intent.return_value = {
            "id": "pi_actually_held",
            "status": "requires_capture",
            "amount_received": Decimal("0"),
        }

        with self.assertLogs("payments.reconciliation", level="WARNING"):
            report = reconciliation.reconcile()

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)
        self.assertEqual(len(report.failures_reversed), 1)

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_and_one_whose_money_was_taken_skips_to_released(self, intent):
        intent.return_value = {
            "id": "pi_actually_held",
            "status": "succeeded",
            "amount_received": Decimal("90.00"),
        }

        with self.assertLogs("payments.reconciliation", level="WARNING"):
            reconciliation.reconcile()

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.RELEASED)
        self.assertEqual(self.escrow.captured_payout, Decimal("79.20"))

    @patch("payments.gateway.retrieve_payment_intent", autospec=True)
    def test_a_genuine_failure_is_left_alone(self, intent):
        intent.return_value = {
            "id": "pi_actually_held",
            "status": "requires_payment_method",
            "amount_received": Decimal("0"),
        }

        report = reconciliation.reconcile()

        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.FAILED)
        self.assertEqual(len(report.failures_reversed), 0)


class AccountFlagOrderingTests(EscrowTestCase):
    """The same problem, quieter: flags that decide whether work can be funded.

    An older account.updated arriving after a newer one used to overwrite it,
    so a worker who had just been enabled could read as disabled again — locked
    out of work by the order two HTTP requests happened to arrive in.
    """

    def setUp(self):
        self.account = self.ready_account()

    def event(self, *, created, charges_enabled):
        return {
            "id": f"evt_acct_{created}",
            "type": "account.updated",
            "created": created,
            "data": {
                "object": {
                    "id": self.account.account_id,
                    "details_submitted": True,
                    "charges_enabled": charges_enabled,
                    "payouts_enabled": charges_enabled,
                }
            },
        }

    def post(self, event):
        with patch(
            "payments.gateway.construct_event", return_value=event, autospec=True
        ):
            return self.client.post(
                reverse("payments:webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )

    def test_a_newer_snapshot_is_applied(self):
        self.post(self.event(created=2_000, charges_enabled=True))

        self.account.refresh_from_db()
        self.assertTrue(self.account.charges_enabled)

    def test_an_older_snapshot_arriving_late_is_ignored(self):
        self.post(self.event(created=2_000, charges_enabled=True))
        self.post(self.event(created=1_000, charges_enabled=False))

        self.account.refresh_from_db()
        self.assertTrue(
            self.account.charges_enabled,
            "an older event must not undo a newer one",
        )

    def test_a_later_disable_still_applies(self):
        """Ordering, not stickiness: a genuinely newer 'no' must land."""
        self.post(self.event(created=1_000, charges_enabled=True))
        self.post(self.event(created=3_000, charges_enabled=False))

        self.account.refresh_from_db()
        self.assertFalse(self.account.charges_enabled)


class WebhookLeaseTests(EscrowTestCase):
    """A duplicate delivery must not answer for a run still in progress.

    The claim stopped two deliveries doing the work twice, and introduced the
    opposite failure: the duplicate answered 200, meaning "already handled",
    while the run it collided with was still going. If that run then failed, it
    released its claim — but Stripe had already been told the event was
    delivered, so no retry was coming and the event was simply lost. Nothing
    reads as wrong afterwards, which is what makes it the worse of the two.
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

    def event(self, event_id="evt_lease"):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"escrow_id": str(self.escrow.pk)},
                    "payment_intent": "pi_leased",
                }
            },
        }

    def post(self, event):
        with patch(
            "payments.gateway.construct_event", return_value=event, autospec=True
        ):
            return self.client.post(
                reverse("payments:webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=fake",
            )

    def test_a_duplicate_arriving_mid_flight_is_told_to_come_back(self):
        """409, not 200: nobody here can promise the first run will finish."""
        WebhookEvent.objects.create(
            event_id="evt_lease", event_type="checkout.session.completed"
        )

        response = self.post(self.event())

        self.assertEqual(response.status_code, 409)

    def test_a_duplicate_after_the_work_finished_is_told_it_is_done(self):
        WebhookEvent.objects.create(
            event_id="evt_lease",
            event_type="checkout.session.completed",
            handled_at=timezone.now(),
        )

        response = self.post(self.event())

        self.assertEqual(response.status_code, 200)

    def test_the_row_is_only_a_receipt_once_the_work_is_done(self):
        seen = {}

        def spy(escrow, intent):
            seen["handled"] = WebhookEvent.objects.get(pk="evt_lease").handled_at
            return escrow

        with patch("payments.services.mark_authorized", side_effect=spy):
            self.post(self.event())

        self.assertIsNone(seen["handled"], "still a lease while the work runs")
        self.assertIsNotNone(WebhookEvent.objects.get(pk="evt_lease").handled_at)

    def test_an_abandoned_claim_is_taken_over_after_the_lease(self):
        """A process can die without releasing anything, and then every retry
        would meet 409 for ever."""
        from payments.views import WEBHOOK_LEASE

        stale = timezone.now() - WEBHOOK_LEASE - timedelta(minutes=1)
        WebhookEvent.objects.create(
            event_id="evt_lease", event_type="checkout.session.completed"
        )
        WebhookEvent.objects.filter(pk="evt_lease").update(received_at=stale)

        response = self.post(self.event())

        self.assertEqual(response.status_code, 200)
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.status, EscrowStatus.AUTHORIZED)

    def test_a_failed_run_still_releases_its_claim(self):
        with patch(
            "payments.services.mark_authorized", side_effect=RuntimeError("boom")
        ):
            response = self.post(self.event())

        self.assertEqual(response.status_code, 500)
        self.assertFalse(WebhookEvent.objects.filter(pk="evt_lease").exists())

    def test_but_it_cannot_release_a_claim_that_was_already_handled(self):
        """The delete is conditional, so a failing latecomer cannot erase the
        receipt of a run that succeeded."""
        WebhookEvent.objects.create(
            event_id="evt_lease",
            event_type="checkout.session.completed",
            handled_at=timezone.now(),
        )

        self.post(self.event())

        self.assertTrue(WebhookEvent.objects.filter(pk="evt_lease").exists())


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
        with patch("payments.gateway.construct_event", return_value=event, autospec=True):
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
        with patch("payments.gateway.construct_event", return_value=event, autospec=True):
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
