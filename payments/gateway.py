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


class ObjectMissing(RuntimeError):
    """Stripe answered, and what we asked about is not there.

    Deliberately distinct from every other failure, because the two demand
    opposite responses and were being treated the same. A session Stripe has
    never heard of means "start again"; a timeout means "Stripe may well have
    it, and may already have taken money — do not start anything new". Catching
    both as "gone" is how a network blip turns into a second payment attempt.
    """


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
    email: str,
    *,
    country: str,
    idempotency_key: str | None = None,
    metadata: dict[str, str] | None = None,
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

    That protection has a horizon: Stripe forgets a key after 24 hours. A
    process that dies between this call and the insert leaves an account nobody
    knows about, and a retry the next day opens a second one. ``metadata`` is
    what makes the first one findable — the worker's id travels with the
    account, so reconciliation can ask Stripe "whose is this?" instead of
    guessing.
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
        metadata=metadata or {},
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
    """What Stripe currently says about a checkout.

    Raises :class:`ObjectMissing` when Stripe has no such session, and lets
    every other failure through untouched — a timeout is not an answer, and
    the caller must not be able to mistake it for one.
    """
    _client()
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except stripe.InvalidRequestError as exc:
        raise ObjectMissing(str(exc)) from exc
    return {
        "payment_intent": session.payment_intent,
        "payment_status": session.payment_status,
        "status": session.status,
    }


def capture_payment_intent(
    payment_intent_id: str,
    amount: Decimal | None = None,
    application_fee: Decimal | None = None,
) -> dict:
    """Take the held funds — all of them, or part.

    A partial capture is what a job that ended early needs: the remainder of
    the authorisation is released back to the client automatically, which is
    the prorated-payment behaviour the spec describes.

    ``application_fee`` has to move with it, and this is not a refinement — it
    is the difference between the worker being paid correctly and being short
    changed. The fee was fixed on the session at the *full* amount, and Stripe
    does not prorate it when less is captured: it takes the whole original fee
    out of the smaller capture. On a €100 day ending early at €60, a 12% fee
    stops being €7.20 and becomes €12, and every cent of that comes out of the
    worker's half. Passing the fee for the amount actually captured is what
    keeps the split the one both sides agreed to.
    """
    _client()
    kwargs = {}
    if amount is not None:
        kwargs["amount_to_capture"] = to_cents(amount)
    if application_fee is not None:
        kwargs["application_fee_amount"] = to_cents(application_fee)
    intent = stripe.PaymentIntent.capture(payment_intent_id, **kwargs)
    return {
        "id": intent.id,
        "status": intent.status,
        "amount_received": from_cents(intent.amount_received or 0),
    }


def retrieve_payment_intent(payment_intent_id: str) -> dict:
    """What Stripe currently says about a hold. Reconciliation's only question.

    Everything the app believes about money is a local record of something that
    happened somewhere else; this is how it checks. ``amount_received`` is what
    was actually taken, which is the number that decides whether a local row
    reading AUTHORIZED is simply behind.
    """
    _client()
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.InvalidRequestError as exc:
        raise ObjectMissing(str(exc)) from exc
    return {
        "id": intent.id,
        "status": intent.status,
        "amount_received": from_cents(intent.amount_received or 0),
    }


def find_account_for(worker_id: int) -> str | None:
    """An account Stripe holds for this worker that we have lost track of.

    Only reachable because ``create_express_account`` writes the worker's id
    into the account's metadata. Without that this question has no answer and
    an orphan stays an orphan — there is no other handle on an account whose id
    was never written down.
    """
    _client()
    for account in stripe.Account.list(limit=100).auto_paging_iter():
        if str((account.metadata or {}).get("worker_id", "")) == str(worker_id):
            return account.id
    return None


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
