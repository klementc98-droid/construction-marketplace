"""The tests every other test in this app cannot be: real calls to Stripe.

Everything else replaces ``payments.gateway`` with a mock, which is what makes
the suite runnable with no keys and no network — and which means the suite
proves things about this application and nothing at all about the thing it
talks to. A mock answers what it was told to answer. It cannot tell you that
Stripe caps an application fee at the captured amount, that an idempotency key
lasts a day, or that a session Stripe has never heard of raises the error this
code catches.

Every finding in this codebase's payment history was one of two kinds: an
assumption about *our* rows, which mocks can test, or an assumption about
*Stripe's* behaviour, which they cannot. This file is for the second kind.

It skips unless STRIPE_SECRET_KEY is set, so it costs nothing to have here.
With a test-mode key it runs against Stripe's real API in test mode — no real
money, real behaviour:

    STRIPE_SECRET_KEY=sk_test_... python manage.py test payments.test_live_stripe

Test-mode keys only. The guard below refuses a live key outright rather than
trusting whoever runs it to have read this paragraph.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from django.conf import settings
from django.test import SimpleTestCase

from config import business_rules as rules

from . import gateway


def _live_key_guard() -> str | None:
    """Why this file is being skipped, or None if it should run."""
    key = settings.STRIPE_SECRET_KEY
    if not key:
        return "STRIPE_SECRET_KEY is not set"
    if not key.startswith("sk_test_"):
        # Not negotiable. These tests create accounts, hold cards and capture
        # money; against a live key that is somebody's real card.
        return "refusing to run against a key that is not sk_test_"
    return None


SKIP = _live_key_guard()


@unittest.skipIf(SKIP, SKIP or "")
class GatewayAgainstStripeTests(SimpleTestCase):
    """What the gateway believes about Stripe, checked against Stripe."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account_id = gateway.create_express_account(
            email="live-test@example.com",
            country=rules.DEFAULT_REGION_COUNTRY,
            idempotency_key=None,
            metadata={"worker_id": "live-test"},
        )

    # -- the assumption every partial capture rests on ---------------------

    def _held_intent(self, amount: Decimal, fee: Decimal):
        """An authorisation, confirmed with a test card, ready to capture."""
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        intent = stripe.PaymentIntent.create(
            amount=gateway.to_cents(amount),
            currency=rules.CURRENCY,
            capture_method="manual",
            payment_method="pm_card_visa",
            confirm=True,
            application_fee_amount=gateway.to_cents(fee),
            transfer_data={"destination": self.account_id},
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        )
        self.assertEqual(intent.status, "requires_capture")
        return intent

    def test_a_partial_capture_takes_the_fee_we_send_it(self):
        """The bug that was short-changing workers, asked of Stripe directly.

        The fee is fixed on the intent at the full amount. Capturing less does
        not prorate it — which is why release recomputes the fee on what is
        actually being captured. This is the assertion that says so from the
        other side of the network.
        """
        intent = self._held_intent(Decimal("100"), rules.platform_fee_for(Decimal("100")))

        captured = Decimal("60")
        result = gateway.capture_payment_intent(
            intent.id,
            amount=captured,
            application_fee=rules.platform_fee_for(captured),
        )

        self.assertEqual(result["amount_received"], captured)
        self.assertEqual(result["status"], "succeeded")

    def test_and_that_sending_no_fee_leaves_the_original_one_in_place(self):
        """The failing half of the same question — the old behaviour.

        Worth asserting rather than assuming, because the whole fix rests on
        it: if Stripe *did* prorate, recomputing would be redundant.
        """
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        full = Decimal("100")
        fee = rules.platform_fee_for(full)
        intent = self._held_intent(full, fee)

        gateway.capture_payment_intent(intent.id, amount=Decimal("60"))

        charge = stripe.PaymentIntent.retrieve(
            intent.id, expand=["latest_charge"]
        ).latest_charge
        self.assertEqual(
            gateway.from_cents(charge.application_fee_amount),
            fee,
            "Stripe kept the original fee against a smaller capture",
        )

    # -- the errors this code branches on ----------------------------------

    def test_an_unknown_session_raises_ObjectMissing(self):
        """The distinction the funding path depends on: gone, not unreachable."""
        with self.assertRaises(gateway.ObjectMissing):
            gateway.retrieve_session("cs_test_does_not_exist_at_all")

    def test_an_unknown_intent_raises_ObjectMissing(self):
        with self.assertRaises(gateway.ObjectMissing):
            gateway.retrieve_payment_intent("pi_does_not_exist_at_all")

    # -- idempotency, which is the whole funding guarantee ------------------

    def test_the_same_key_returns_the_same_checkout_session(self):
        key = f"live-test-session:{self.account_id}"
        first = gateway.create_checkout_session(
            job_title="Live test",
            amount=Decimal("90"),
            platform_fee=rules.platform_fee_for(Decimal("90")),
            destination_account_id=self.account_id,
            success_url="https://example.com/ok",
            cancel_url="https://example.com/no",
            metadata={"job_id": "0", "escrow_id": "0"},
            idempotency_key=key,
        )
        second = gateway.create_checkout_session(
            job_title="Live test",
            amount=Decimal("90"),
            platform_fee=rules.platform_fee_for(Decimal("90")),
            destination_account_id=self.account_id,
            success_url="https://example.com/ok",
            cancel_url="https://example.com/no",
            metadata={"job_id": "0", "escrow_id": "0"},
            idempotency_key=key,
        )

        self.assertEqual(first[0], second[0], "one session, not two")

    def test_the_same_key_returns_the_same_connect_account(self):
        key = "live-test-account:same"
        first = gateway.create_express_account(
            email="live-idem@example.com",
            country=rules.DEFAULT_REGION_COUNTRY,
            idempotency_key=key,
            metadata={"worker_id": "live-idem"},
        )
        second = gateway.create_express_account(
            email="live-idem@example.com",
            country=rules.DEFAULT_REGION_COUNTRY,
            idempotency_key=key,
            metadata={"worker_id": "live-idem"},
        )

        self.assertEqual(first, second, "one account, not two")

    def test_a_lost_account_can_be_found_by_its_metadata(self):
        """What reconciliation does when an id never reached the database."""
        found = gateway.find_account_for("live-test")
        self.assertEqual(found, self.account_id)

    # -- cancelling, which is how a dead hold is given back ----------------

    def test_cancelling_releases_the_hold(self):
        intent = self._held_intent(Decimal("50"), rules.platform_fee_for(Decimal("50")))

        gateway.cancel_payment_intent(intent.id)

        self.assertEqual(
            gateway.retrieve_payment_intent(intent.id)["status"], "canceled"
        )

    def test_a_cancelled_hold_reads_as_cancelled_to_reconciliation(self):
        """The status string the reconciler branches on, from Stripe itself."""
        intent = self._held_intent(Decimal("50"), rules.platform_fee_for(Decimal("50")))
        gateway.cancel_payment_intent(intent.id)

        self.assertEqual(
            gateway.retrieve_payment_intent(intent.id)["status"],
            "canceled",
            "reconciliation matches on this exact string",
        )
