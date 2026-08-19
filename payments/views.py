"""Phase 4 views: worker payout setup, client funding, and the webhook."""

from __future__ import annotations

import logging

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from config import business_rules as rules
from core.state_machine import Actor
from jobs.models import Job

from . import gateway, services
from .models import EscrowPayment, EscrowStatus, StripeAccount, WebhookEvent

logger = logging.getLogger(__name__)


def _worker(request):
    return getattr(request.user, "worker_profile", None)


def _client(request):
    return getattr(request.user, "client_profile", None)


def _absolute(request, viewname, **kwargs) -> str:
    return request.build_absolute_uri(reverse(viewname, kwargs=kwargs or None))


# ---------------------------------------------------------------------------
# Worker: getting paid
# ---------------------------------------------------------------------------


@login_required
def payouts(request):
    """Where a worker sets up, and checks, their ability to be paid."""
    worker = _worker(request)
    if worker is None:
        flash.info(request, "Add a worker profile to set up payouts.")
        return redirect("accounts:select_role")

    account = StripeAccount.objects.filter(worker=worker).first()
    return render(
        request,
        "payments/payouts.html",
        {
            "account": account,
            "stripe_configured": gateway.configured(),
            "platform_fee_pct": rules.PLATFORM_FEE_PCT * 100,
        },
    )


@login_required
@require_POST
def payouts_start(request):
    """Create the Connect account if needed and bounce to Stripe onboarding."""
    worker = _worker(request)
    if worker is None:
        flash.info(request, "Add a worker profile to set up payouts.")
        return redirect("accounts:select_role")

    try:
        account = services.ensure_stripe_account(worker)
        url = gateway.create_account_link(
            account.account_id,
            refresh_url=_absolute(request, "payments:payouts_start"),
            return_url=_absolute(request, "payments:payouts_return"),
        )
    except gateway.StripeNotConfigured as exc:
        flash.error(request, str(exc))
        return redirect("payments:payouts")

    return redirect(url)


@login_required
def payouts_return(request):
    """Where Stripe sends the worker back.

    Returning here proves nothing on its own — the worker may have closed the
    flow half way. So we re-read the account rather than trusting the redirect.
    """
    worker = _worker(request)
    account = StripeAccount.objects.filter(worker=worker).first()
    if account is not None:
        try:
            services.refresh_account_flags(account)
        except gateway.StripeNotConfigured as exc:
            flash.error(request, str(exc))

    if account and account.is_ready:
        flash.success(request, "Payouts are set up. You can be paid for gigs now.")
    else:
        flash.info(
            request,
            "Stripe still needs something from you before payouts can start.",
        )
    return redirect("payments:payouts")


# ---------------------------------------------------------------------------
# Client: funding the gig
# ---------------------------------------------------------------------------


@login_required
def escrow_detail(request, pk: int):
    """The money view of one job, for whichever side is looking."""
    job = get_object_or_404(
        Job.objects.select_related("client__user", "assigned_worker__user"), pk=pk
    )
    client = _client(request)
    worker = _worker(request)
    is_client = client is not None and job.client_id == client.pk
    is_worker = worker is not None and job.assigned_worker_id == worker.pk
    if not (is_client or is_worker):
        flash.error(request, "That job isn't yours.")
        # The board, not the job. A job with somebody on it is readable only by
        # the two people it is between, so sending an outsider to it would
        # answer one refusal with a 404.
        return redirect("jobs:list")

    escrow = EscrowPayment.objects.filter(job=job).first()
    return render(
        request,
        "payments/escrow_detail.html",
        {
            "job": job,
            "escrow": escrow,
            "is_client": is_client,
            "blocker": services.funding_blocker(job) if is_client else None,
            "platform_fee_pct": rules.PLATFORM_FEE_PCT * 100,
            "stripe_configured": gateway.configured(),
        },
    )


@login_required
@require_POST
def fund(request, pk: int):
    client = _client(request)
    job = get_object_or_404(Job, pk=pk, client=client)
    try:
        url = services.start_funding(
            job,
            success_url=_absolute(request, "payments:fund_return", pk=job.pk),
            cancel_url=_absolute(request, "payments:escrow", pk=job.pk),
        )
    except (services.EscrowError, gateway.StripeNotConfigured) as exc:
        flash.error(request, str(exc))
        return redirect("payments:escrow", pk=job.pk)
    return redirect(url)


@login_required
def fund_return(request, pk: int):
    """Back from Checkout.

    The webhook is the authority on whether the hold exists; this exists so the
    client does not have to sit on a "pending" screen waiting for it. Both call
    the same idempotent function, so whichever lands first wins and the other
    is a no-op.
    """
    client = _client(request)
    job = get_object_or_404(Job, pk=pk, client=client)
    escrow = EscrowPayment.objects.filter(job=job).first()

    if escrow and escrow.checkout_session_id and not escrow.is_held:
        try:
            session = gateway.retrieve_session(escrow.checkout_session_id)
            if session["payment_status"] in ("paid", "unpaid") and session[
                "payment_intent"
            ]:
                # "unpaid" is expected here: a manual-capture intent is
                # authorised, not paid, until we capture it.
                services.mark_authorized(escrow, session["payment_intent"])
        except gateway.StripeNotConfigured as exc:
            flash.error(request, str(exc))
        except Exception:  # pragma: no cover - network/Stripe failure
            logger.exception("Could not confirm checkout session for job %s", job.pk)

    escrow = EscrowPayment.objects.filter(job=job).first()
    if escrow and escrow.is_held:
        flash.success(request, "Funds are held. The worker can start.")
    else:
        flash.info(request, "We're still confirming the payment with Stripe.")
    return redirect("payments:escrow", pk=job.pk)


@login_required
@require_POST
def cancel_and_refund(request, pk: int):
    """Client calls off a funded gig; the hold is released."""
    client = _client(request)
    job = get_object_or_404(Job, pk=pk, client=client)
    escrow = get_object_or_404(EscrowPayment, job=job)
    try:
        services.refund(escrow, actor=Actor.CLIENT)
    except (services.EscrowError, gateway.StripeNotConfigured) as exc:
        flash.error(request, str(exc))
        return redirect("payments:escrow", pk=job.pk)
    flash.success(request, "Cancelled. The hold on your card has been released.")
    return redirect("payments:escrow", pk=job.pk)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


@csrf_exempt
@require_POST
def webhook(request):
    """Stripe's report of what actually happened.

    CSRF-exempt because Stripe is not a browser and has no cookie; the
    signature check below is what authenticates it, and it is not optional.
    """
    try:
        event = gateway.construct_event(
            request.body, request.META.get("HTTP_STRIPE_SIGNATURE", "")
        )
    except gateway.StripeNotConfigured as exc:
        logger.error("Webhook received but not configured: %s", exc)
        return HttpResponse(status=503)
    except Exception as exc:
        logger.warning("Rejected webhook: %s", exc)
        return HttpResponseBadRequest("bad signature")

    # Stripe retries, and does not promise exactly-once delivery. Releasing a
    # payout twice is not recoverable, so a replayed id stops here.
    #
    # The row is written *before* the work, not after, and that ordering is the
    # whole point. Asking "does this exist?" and then doing the work is two
    # steps with a gap: two deliveries of the same event — a retry arriving
    # while the first is still running — both find nothing and both proceed.
    # The insert is a single atomic claim, so exactly one of them wins it.
    #
    # What saves this from the opposite failure, a claim held by a run that
    # then crashed, is the delete in the except below: the claim is released
    # and Stripe's next retry does the work for real.
    try:
        with transaction.atomic():
            claim = WebhookEvent.objects.create(
                event_id=event["id"], event_type=event["type"]
            )
    except IntegrityError:
        return HttpResponse(status=200)

    obj = event["data"]["object"]
    handled = ""

    try:
        if event["type"] == "checkout.session.completed":
            escrow = _escrow_from_metadata(obj.get("metadata") or {})
            if escrow and obj.get("payment_intent"):
                services.mark_authorized(escrow, obj["payment_intent"])
                handled = f"escrow {escrow.pk} authorized"

        elif event["type"] == "payment_intent.amount_capturable_updated":
            escrow = _escrow_from_metadata(obj.get("metadata") or {})
            if escrow:
                services.mark_authorized(escrow, obj["id"])
                handled = f"escrow {escrow.pk} authorized"

        elif event["type"] == "payment_intent.payment_failed":
            escrow = _escrow_from_metadata(obj.get("metadata") or {})
            if escrow:
                reason = (obj.get("last_payment_error") or {}).get(
                    "message", "Payment failed."
                )
                services.mark_failed(escrow, reason)
                handled = f"escrow {escrow.pk} failed"

        elif event["type"] == "account.updated":
            account = StripeAccount.objects.filter(account_id=obj["id"]).first()
            if account:
                account.details_submitted = bool(obj.get("details_submitted"))
                account.charges_enabled = bool(obj.get("charges_enabled"))
                account.payouts_enabled = bool(obj.get("payouts_enabled"))
                account.save(
                    update_fields=[
                        "details_submitted",
                        "charges_enabled",
                        "payouts_enabled",
                        "updated_at",
                    ]
                )
                handled = f"account {account.account_id} refreshed"
    except Exception:  # pragma: no cover - defensive
        # Give the claim back and return 500 so Stripe retries. Keeping it
        # would mean the event is on record as handled while nothing happened,
        # which is the one outcome worse than doing the work twice — the retry
        # that would have fixed it never comes.
        logger.exception("Webhook %s failed", event["id"])
        WebhookEvent.objects.filter(pk=claim.pk).delete()
        return HttpResponse(status=500)

    # Only the summary is written here now; the claim itself already exists.
    WebhookEvent.objects.filter(pk=claim.pk).update(payload_summary=handled)
    return HttpResponse(status=200)


def _escrow_from_metadata(metadata: dict) -> EscrowPayment | None:
    escrow_id = metadata.get("escrow_id")
    if not escrow_id:
        return None
    return EscrowPayment.objects.filter(pk=escrow_id).first()
