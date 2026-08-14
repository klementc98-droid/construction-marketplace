"""Phase 5 views: the on-site actions, and the one page that shows the job."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from config import business_rules as rules
from core.state_machine import Actor, JobState, available_transitions
from jobs.models import Job
from payments.models import EscrowPayment
from payments.services import EscrowError

from . import services
from .models import payable_for


class EarlyEndForm(forms.Form):
    hours_worked = forms.DecimalField(
        label="Hours actually worked",
        max_digits=4,
        decimal_places=1,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.5", "min": "0"}),
    )
    note = forms.CharField(
        label="What happened?",
        required=False,
        max_length=300,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Optional, but it helps if this is queried later."}),
    )


class DisputeForm(forms.Form):
    reason = forms.CharField(
        label="What's the problem?",
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 5, "placeholder": "A person reads this. Be specific — hours, times, what was agreed."}
        ),
    )


def _roles(request, job):
    worker = getattr(request.user, "worker_profile", None)
    client = getattr(request.user, "client_profile", None)
    is_worker = worker is not None and job.assigned_worker_id == worker.pk
    is_client = client is not None and job.client_id == client.pk
    return worker, is_worker, is_client


@login_required
def workspace(request, pk: int):
    """One page showing where a gig stands and the single next thing to do.

    The available actions come from the state machine rather than a second
    hand-maintained list, so the buttons and the rules cannot drift apart.
    """
    job = get_object_or_404(
        Job.objects.select_related(
            "client__user", "assigned_worker__user", "trade", "region"
        ),
        pk=pk,
    )
    worker, is_worker, is_client = _roles(request, job)
    if not (is_worker or is_client):
        flash.error(request, "That job isn't yours.")
        return redirect("jobs:detail", pk=job.pk)

    actor = Actor.WORKER if is_worker else Actor.CLIENT
    completion = getattr(job, "completion", None)
    escrow = EscrowPayment.objects.filter(job=job).first()

    return render(
        request,
        "worklog/workspace.html",
        {
            "job": job,
            "escrow": escrow,
            "check_in": getattr(job, "check_in", None),
            "completion": completion,
            "dispute": getattr(job, "dispute", None),
            "is_worker": is_worker,
            "is_client": is_client,
            "moves": [m.to_state for m in available_transitions(job.state, actor)],
            "early_form": EarlyEndForm(),
            "dispute_form": DisputeForm(),
            "minimum_hours": rules.MINIMUM_GUARANTEED_HOURS,
            "approval_hours": int(rules.CLIENT_APPROVAL_WINDOW.total_seconds() // 3600),
            "dispute_hours": int(rules.EARLY_END_DISPUTE_WINDOW.total_seconds() // 3600),
            "now": timezone.now(),
        },
    )


@login_required
@require_POST
def check_in(request, pk: int):
    job = get_object_or_404(Job, pk=pk)
    worker, is_worker, _ = _roles(request, job)
    if not is_worker:
        flash.error(request, "Only the worker on this job can check in.")
        return redirect("worklog:workspace", pk=job.pk)

    def _decimal(name):
        raw = request.POST.get(name)
        try:
            return Decimal(raw) if raw else None
        except (InvalidOperation, TypeError):
            return None

    accuracy = request.POST.get("accuracy")
    try:
        accuracy = int(float(accuracy)) if accuracy else None
    except (TypeError, ValueError):
        accuracy = None

    try:
        record = services.check_in(
            job,
            worker,
            latitude=_decimal("latitude"),
            longitude=_decimal("longitude"),
            accuracy_m=accuracy,
        )
    except services.WorkflowError as exc:
        flash.error(request, str(exc))
        return redirect("worklog:workspace", pk=job.pk)

    if record.looks_on_site is False:
        # Recorded, not blocked. Saying so is honest; refusing would not be.
        flash.success(
            request,
            "Checked in. Your location looked further from the site than "
            "expected — that's noted but doesn't stop anything.",
        )
    else:
        flash.success(request, "Checked in. You're on the clock.")
    return redirect("worklog:workspace", pk=job.pk)


@login_required
@require_POST
def complete(request, pk: int):
    job = get_object_or_404(Job, pk=pk)
    worker, is_worker, _ = _roles(request, job)
    if not is_worker:
        flash.error(request, "Only the worker on this job can mark it done.")
        return redirect("worklog:workspace", pk=job.pk)
    try:
        services.complete(job, worker)
    except services.WorkflowError as exc:
        flash.error(request, str(exc))
        return redirect("worklog:workspace", pk=job.pk)
    flash.success(request, "Marked complete. The client has been notified.")
    return redirect("worklog:workspace", pk=job.pk)


@login_required
@require_POST
def finish(request, pk: int):
    """Worker: "the work is done" — on a gig with no escrow."""
    job = get_object_or_404(Job.objects.select_related("client__user"), pk=pk)
    worker, is_worker, _ = _roles(request, job)
    if not is_worker:
        flash.error(request, "Only the worker on this job can mark it done.")
        return redirect("worklog:workspace", pk=job.pk)
    try:
        services.mark_work_finished(job, worker)
    except services.WorkflowError as exc:
        flash.error(request, str(exc))
        return redirect("worklog:workspace", pk=job.pk)
    flash.success(
        request, "Marked as finished. Waiting for the client to confirm."
    )
    return redirect("worklog:workspace", pk=job.pk)


@login_required
@require_POST
def confirm(request, pk: int):
    """Client: "yes, it happened" — closes a gig with no escrow."""
    job = get_object_or_404(Job.objects.select_related("client__user"), pk=pk)
    try:
        services.confirm_closed(job, request.user)
    except services.WorkflowError as exc:
        flash.error(request, str(exc))
        return redirect("worklog:workspace", pk=job.pk)
    flash.success(request, "Closed. You can both leave a rating now.")
    return redirect("worklog:workspace", pk=job.pk)


@login_required
@require_POST
def end_early(request, pk: int):
    job = get_object_or_404(Job.objects.select_related("client__user"), pk=pk)
    _, is_worker, is_client = _roles(request, job)
    if not (is_worker or is_client):
        flash.error(request, "That job isn't yours.")
        return redirect("jobs:detail", pk=job.pk)

    form = EarlyEndForm(request.POST)
    if not form.is_valid():
        flash.error(request, "Enter how many hours were actually worked.")
        return redirect("worklog:workspace", pk=job.pk)

    try:
        completion = services.flag_early_end(
            job,
            request.user,
            hours_worked=form.cleaned_data["hours_worked"],
            note=form.cleaned_data.get("note", ""),
        )
    except services.WorkflowError as exc:
        flash.error(request, str(exc))
        return redirect("worklog:workspace", pk=job.pk)

    flash.success(
        request,
        f"Flagged as ended early. ${completion.payable_amount} releases in "
        f"{int(rules.EARLY_END_DISPUTE_WINDOW.total_seconds() // 3600)} hours "
        "unless someone disputes.",
    )
    return redirect("worklog:workspace", pk=job.pk)


@login_required
@require_POST
def approve(request, pk: int):
    job = get_object_or_404(Job.objects.select_related("client__user"), pk=pk)
    try:
        completion = services.approve(job, request.user)
    except (services.WorkflowError, EscrowError) as exc:
        flash.error(request, str(exc))
        return redirect("worklog:workspace", pk=job.pk)
    flash.success(request, f"Released ${completion.payable_amount} to the worker.")
    return redirect("worklog:workspace", pk=job.pk)


@login_required
@require_POST
def dispute(request, pk: int):
    job = get_object_or_404(Job.objects.select_related("client__user"), pk=pk)
    form = DisputeForm(request.POST)
    if not form.is_valid():
        flash.error(request, "Describe the problem so someone can review it.")
        return redirect("worklog:workspace", pk=job.pk)
    try:
        services.raise_dispute(job, request.user, reason=form.cleaned_data["reason"])
    except services.WorkflowError as exc:
        flash.error(request, str(exc))
        return redirect("worklog:workspace", pk=job.pk)
    flash.success(
        request,
        "Raised. The payment is frozen until someone reviews it — nothing "
        "releases automatically from here.",
    )
    return redirect("worklog:workspace", pk=job.pk)


def preview_payable(job: Job, hours) -> Decimal:
    """Exposed for the template's live estimate."""
    return payable_for(job, hours)
