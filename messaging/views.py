"""Phase 3 views: inbox, thread, and the two ways a thread gets started."""

from __future__ import annotations

from django import forms
from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import WorkerProfile
from jobs.models import Job, JobType, collapse_rows

from notifications.models import Kind
from notifications.services import booking_key, notify

from .models import Conversation, Message, can_converse


class MessageForm(forms.ModelForm):
    class Meta:
        model = Message
        fields = ["body"]
        labels = {"body": ""}
        widgets = {
            # Two rows, not three. The composer is a pill at the foot of the
            # thread now; browsers that support `field-sizing: content` grow it
            # from one line as you type, and the rest get a two-line box, which
            # is the smallest thing that still lets you see a sentence you are
            # part-way through writing.
            "body": forms.Textarea(
                attrs={"rows": 2, "placeholder": "Message…"}
            )
        }


def _worker(request) -> WorkerProfile | None:
    return getattr(request.user, "worker_profile", None)


def _client(request):
    return getattr(request.user, "client_profile", None)


@login_required
def inbox(request):
    conversations = (
        Conversation.objects.for_user(request.user)
        .with_unread_for(request.user)
        .with_preview()
        .select_related("job__trade", "job__client__user", "worker__user")
        # Ordered here, not left to Meta.ordering, which does not survive this
        # chain: with_unread_for annotates a Count, that makes it a GROUP BY
        # query, and Django drops a model's default ordering on those. The
        # inbox came back in whatever order the database chose — so whoever
        # wrote to you most recently was not reliably at the top, which is the
        # one thing an inbox has to get right.
        #
        # nulls_last because a thread opened with no message yet has no
        # last_message_at, and "never used" belongs at the bottom rather than
        # sorted among today's.
        .order_by(models.F("last_message_at").desc(nulls_last=True), "-created_at")
    )

    # One row per booking, not per day. A four-day booking opens four threads,
    # because a thread belongs to a job and each day is its own job — but the
    # two people in them are the same two people talking about the same week,
    # and an inbox listing the same name and the same trade four times over
    # reads as the app being broken rather than as the work being four days.
    #
    # The same collapse the boards and the offer lists already do; see
    # jobs.models.collapse_rows. The row that survives is the one that sorted
    # highest, which is the most recently active — open the booking and you
    # land where the talking is.
    rows = collapse_rows(list(conversations), lambda c: c.job)

    # Unread has to be summed rather than inherited. The badge answers "is
    # there anything here for me", and a booking whose only unread message is
    # on Wednesday's thread would have shown a clean row while an unanswered
    # question sat inside it.
    unread = _unread_by_group(conversations)
    for row in rows:
        row.unread_count = unread.get(row.job.offer_group, row.unread_count)

    return render(request, "messaging/inbox.html", {"conversations": rows})


def _unread_by_group(conversations):
    """Total unread per booking, for the rows that stand for several threads."""
    totals: dict = {}
    for conversation in conversations:
        group = conversation.job.offer_group
        if group is None:
            continue
        totals[group] = totals.get(group, 0) + conversation.unread_count
    return totals


@login_required
def thread(request, pk: int):
    conversation = get_object_or_404(
        Conversation.objects.select_related(
            "job__trade", "job__client__user", "worker__user"
        ),
        pk=pk,
    )
    # Membership, not merely authentication. A signed-in stranger is still a
    # stranger to this thread.
    if not conversation.includes(request.user):
        flash.error(request, "That conversation isn't yours.")
        return redirect("messaging:inbox")

    if request.method == "POST":
        form = MessageForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                message = form.save(commit=False)
                message.conversation = conversation
                message.sender = request.user
                message.save()
                conversation.last_message_at = message.created_at
                conversation.save(update_fields=["last_message_at", "updated_at"])

                # The one notification that is worth more than the badge. A
                # question asked on site is useless if it sits unseen until
                # tonight, and the person it is for is on a roof, not on this
                # page.
                #
                # Keyed on the booking and the sender rather than the message,
                # so a run of five messages while somebody is not looking is
                # one email rather than five. Whatever is still unsent when the
                # command next runs is the one they get, which is the newest.
                notify(
                    conversation.other_party(request.user),
                    Kind.MESSAGE,
                    job=conversation.job,
                    actor=request.user,
                    dedupe=booking_key("message", conversation.job, request.user.pk),
                    sender=str(request.user),
                    job_title=conversation.job.title,
                    preview=message.body[:300],
                    path=reverse("messaging:thread", args=[conversation.pk]),
                )
            # Land on what you just sent, not on the top of the thread. The
            # anchor is an empty element after the last bubble; without it a
            # long thread reloads scrolled to its oldest message, which is the
            # one place nobody wants to be after pressing Send.
            return redirect(
                reverse("messaging:thread", args=[conversation.pk]) + "#dm-end"
            )
    else:
        form = MessageForm()
        # Opening the thread is what marks the other side's messages read.
        # Only on GET: a POST is you talking, not you reading.
        conversation.messages.filter(read_at__isnull=True).exclude(
            sender=request.user
        ).update(read_at=timezone.now())

    return render(
        request,
        "messaging/thread.html",
        {
            "conversation": conversation,
            "job": conversation.job,
            "other": conversation.other_party(request.user),
            "messages_list": conversation.messages.select_related("sender"),
            "form": form,
        },
    )


@login_required
@require_POST
def start(request, job_pk: int, worker_pk: int):
    """Open (or re-open) the thread for a job/worker pair.

    Reached from the applicants list, from a job the worker applied to, and
    from the assigned worker on a filled job. Idempotent: a second press lands
    on the same thread rather than making another one.
    """
    job = get_object_or_404(Job.objects.select_related("client__user"), pk=job_pk)
    worker = get_object_or_404(WorkerProfile, pk=worker_pk)

    is_client = job.client.user_id == request.user.pk
    is_this_worker = worker.user_id == request.user.pk
    if not (is_client or is_this_worker):
        flash.error(request, "You're not part of that job.")
        return redirect("jobs:detail", pk=job.pk)

    if not can_converse(job, worker):
        flash.error(
            request,
            "You can message once there's an application on this job.",
        )
        return redirect("jobs:detail", pk=job.pk)

    conversation, _ = Conversation.objects.get_or_create(job=job, worker=worker)
    return redirect("messaging:thread", pk=conversation.pk)


@login_required
def start_direct(request, worker_pk: int):
    """A client reaching a worker who has not applied.

    The spec allows direct contact for gig posts, so the client must pick one
    of their own open gigs to attach it to — which is also what stops this
    from becoming an unsolicited-DM channel. Creating the conversation is what
    grants the pair a channel; see the note in ``can_converse``.
    """
    worker = get_object_or_404(
        WorkerProfile.objects.select_related("user"), pk=worker_pk
    )
    client = _client(request)
    if client is None:
        flash.info(request, "Add a client profile to contact workers.")
        return redirect("accounts:select_role")
    if worker.user_id == request.user.pk:
        flash.error(request, "That's you.")
        return redirect("accounts:worker_detail", pk=worker.pk)

    # public(), not open(): a gig written as a direct offer to someone else is
    # not a thread this client may open with a third party. Doing so would put
    # another worker's offer — price, date, site — in front of a stranger.
    gigs = Job.objects.filter(
        client=client, job_type=JobType.GIG
    ).public().select_related("trade")

    if request.method == "POST":
        job = get_object_or_404(gigs, pk=request.POST.get("job"))
        conversation, created = Conversation.objects.get_or_create(
            job=job, worker=worker
        )
        if created:
            flash.success(request, f"Started a thread with {worker.user}.")
        return redirect("messaging:thread", pk=conversation.pk)

    return render(
        request, "messaging/start_direct.html", {"worker": worker, "gigs": gigs}
    )


def unread_count(request):
    """Context processor: the badge in the header.

    Returns 0 for anonymous visitors rather than omitting the key, so the
    template never has to guard on it.
    """
    if not request.user.is_authenticated:
        return {"unread_messages": 0}
    return {
        "unread_messages": Message.objects.filter(
            Q(conversation__job__client__user=request.user)
            | Q(conversation__worker__user=request.user),
            read_at__isnull=True,
        )
        .exclude(sender=request.user)
        .count()
    }
