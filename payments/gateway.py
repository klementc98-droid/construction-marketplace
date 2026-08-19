"""The only module that talks to Stripe over the network.

Everything else in :mod:`payments` calls these functions. That boundary is what
makes the rest of the app testable without keys, a network, or a fixture full
of recorded HTTP — the tests replace this module's functions and assert on what
they were asked to do.

It is also where dollars become cents. Stripe counts in the currency's minor
unit; the business rules, the templates and the models all count in dollars.
Converting in exactly one place means nobody else has to remember which side of
the boundary they are on.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import stripe
from django.conf import settings

from config import business_rules as rules


class StripeNotConfigured(RuntimeError):
    """Raised when a payment operation is attempted with no API key.

    A distinct type rather than a generic error so views can catch it and show
    "payments aren't set up yet" instead of a 500 — which is the right
    experience for a developer running the rest of the app without Stripe.
    """


def configured() -> bool:
    return bool(settings.STRIPE_SECRET_KEY)


def _client() -> None:
    if not configured():
        raise StripeNotConfigured(
            "STRIPE_SECRET_KEY is not set — add it to .env to take payments."
        )
    stripe.api_key = settings.STRIPE_SECRET_KEY


def to_cents(amount: Decimal) -> int:
    """Dollars to Stripe's minor unit, rounded half-up to the cent."""
    return int(
        (Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def from_cents(amount: int) -> Decimal:
    return (Decimal(amount) / Decimal(100)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Connected accounts (workers)
# ---------------------------------------------------------------------------


def create_express_account(
    email: str, *, country: str, idempotency_key: str | None = None
) -> str:
    """Create the worker's Connect account and return its id.

    Express, so Stripe owns identity verification, the payout schedule and the
    dashboard the worker sees. Building any of that ourselves would mean
    holding identity documents, which v1 has no business doing.

    ``country`` is required and has no default. It used to default to "US",
    which is a coherent answer for one launch market and silently wrong for
    every other: country decides which capabilities a Connect account can have,
    what onboarding asks the worker for, and whether payouts are possible at
    all. A default here is a business decision hidden in a function signature,
    so the caller now has to say it — see Region.country.

    ``idempotency_key`` is Stripe's own protection against the same creation
    running twice. Two requests arriving together both find no local account
    and both call this; with a key derived from the worker, Stripe returns the
    *same* account to both instead of opening a second one that nothing in this
    database will ever point at.
    """
    _client()
    account = stripe.Account.create(
        type="express",
        country=country,
        email=email,
        capabilities={
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        business_type="individual",
        **({"idempotency_key": idempotency_key} if idempotency_key else {}),
    )
    return account.id


def create_account_link(account_id: str, refresh_url: str, return_url: str) -> str:
    """A one-time onboarding URL. These expire quickly and are single-use."""
    _client()
    link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return link.url


def retrieve_account(account_id: str) -> dict:
    _client()
    account = stripe.Account.retrieve(account_id)
    return {
        "details_submitted": bool(account.details_submitted),
        "charges_enabled": bool(account.charges_enabled),
        "payouts_enabled": bool(account.payouts_enabled),
    }


# ---------------------------------------------------------------------------
# Taking the money
# ---------------------------------------------------------------------------


def create_checkout_session(
    *,
    job_title: str,
    amount: Decimal,
    platform_fee: Decimal,
    destination_account_id: str,
    success_url: str,
    cancel_url: str,
    metadata: dict[str, str],
    idempotency_key: str | None = None,
) -> tuple[str, str]:
    """Authorise (do not capture) the client's card. Returns (id, url).

    ``capture_method="manual"`` is the whole escrow mechanism: the money is
    committed by the client but not taken, and stays that way until the work is
    done. See ``ESCROW_AUTHORIZATION_MAX_DAYS`` in the business rules for the
    expiry this buys us and what it costs.

    ``application_fee_amount`` with ``transfer_data.destination`` makes this a
    destination charge: at capture Stripe splits the money, sending the payout
    to the worker's account and our cut to the platform, in one movement. Doing
    the split ourselves afterwards would mean a window where the full amount
    sits in our balance and the worker is an unsecured creditor.

    ``idempotency_key`` makes a repeated call return the session the first one
    created rather than opening a second. It matters more here than anywhere
    else in this module: a second session is a second PaymentIntent, and a
    second PaymentIntent that somebody completes is a second hold on a real
    card that this database has no record of and will never release. The key
    belongs to one *attempt* — see ``start_funding``, which changes it when the
    client genuinely starts again.
    """
    _client()
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": rules.CURRENCY,
                    "product_data": {"name": job_title},
                    "unit_amount": to_cents(amount),
                },
                "quantity": 1,
            }
        ],
        payment_intent_data={
            "capture_method": "manual",
            "application_fee_amount": to_cents(platform_fee),
            "transfer_data": {"destination": destination_account_id},
            "metadata": metadata,
        },
        metadata=metadata,
        success_url=success_url,
        cancel_url=cancel_url,
        **({"idempotency_key": idempotency_key} if idempotency_key else {}),
    )
    return session.id, session.url


def retrieve_session(session_id: str) -> dict:
    _client()
    session = stripe.checkout.Session.retrieve(session_id)
    return {
        "payment_intent": session.payment_intent,
        "payment_status": session.payment_status,
        "status": session.status,
    }


def capture_payment_intent(
    payment_intent_id: str, amount: Decimal | None = None
) -> dict:
    """Take the held funds — all of them, or part.

    A partial capture is what phase 5 needs for a job that ended early: the
    remainder of the authorisation is released back to the client
    automatically, which is exactly the prorated-payment behaviour the spec
    describes.
    """
    _client()
    kwargs = {}
    if amount is not None:
        kwargs["amount_to_capture"] = to_cents(amount)
    intent = stripe.PaymentIntent.capture(payment_intent_id, **kwargs)
    return {
        "id": intent.id,
        "status": intent.status,
        "amount_received": from_cents(intent.amount_received or 0),
    }


def cancel_payment_intent(payment_intent_id: str) -> dict:
    """Release an authorisation without taking anything.

    For a gig cancelled before work starts this is better than a refund: the
    client's money was never moved, so there is nothing to give back and no
    refund to wait on their statement.
    """
    _client()
    intent = stripe.PaymentIntent.cancel(payment_intent_id)
    return {"id": intent.id, "status": intent.status}


def construct_event(payload: bytes, signature: str):
    """Verify and parse a webhook.

    Raises if the signature does not check out. Skipping this would mean
    accepting any POST that claims a payment succeeded.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    return stripe.Webhook.construct_event(
        payload, signature, settings.STRIPE_WEBHOOK_SECRET
    )
