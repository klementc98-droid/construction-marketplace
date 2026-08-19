"""Phase 1 views: sign-in landing, role selection, profiles."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from assistant.conversation import take_handoff
from config import business_rules as rules
from core.models import Region, Trade
from core.state_machine import JobState
from jobs.models import Job, collapse_groups
from core.dates import month_grids
from jobs.services import reviews_of
from jobs.waiting import waiting_for

from .forms import (
    AccountDetailsForm,
    ClientProfileForm,
    PortfolioPhotoForm,
    RoleSelectionForm,
    WorkerProfileForm,
)
from .models import ClientProfile, PortfolioPhoto, TradeLicense, WorkerProfile


#: Feed page size. Small enough that the first screen paints fast, large
#: enough that a fast scroll does not out-run the loader.
FEED_PAGE_SIZE = 8

#: Workers shown under the home page's "Find workers" tab. The tab links on to
#: the full list, which is where someone actually shopping goes.
PREVIEW_WORKERS = 8

#: The two halves of the home page. Held in the query string rather than in the
#: session so the choice is in the URL: a link to the side you are looking at
#: still shows that side when you send it to somebody.
SHOW_WORK = "work"
SHOW_WORKERS = "workers"


def home(request):
    """The feed. Open work, newest first, for everyone.

    Signed out, this is the shop window — someone deciding whether to make an
    account should be able to see whether there is any work worth making one
    for. Signed in, it is the first thing you check in the morning.

    Paginated rather than infinite in the database sense: ``?partial=1``
    returns only the rows, which is what the scroll loader appends.
    """
    if request.user.is_authenticated and request.user.needs_role_selection:
        return redirect("accounts:select_role")

    jobs = (
        Job.objects.public()
        .select_related("trade", "region", "client__user")
        .order_by("-created_at", "gig_date")
    )
    # Collapsed before paging, not after: a booking of five days is one entry,
    # and paging the rows first would put two days of the same booking on
    # different pages and still show five cards.
    rows = collapse_groups(jobs)
    page = Paginator(rows, FEED_PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "page": page,
        "total": len(rows),
        # Same panel as "Mine", from the same counts — the answer to "is
        # anything waiting on me?" must not depend on which page you opened.
        "waiting": waiting_for(request.user),
    }

    # Finished work is not shown here at all. It used to pad the end of the
    # feed so an empty board did not read as a blank screen, but a board full
    # of jobs nobody can apply to is worse than a short one — the reader has to
    # check each card before finding out it is over. The workers tab is what
    # fills the page now when there is little work on it.
    #
    # Nothing is deleted: a finished job stays on both parties' "Mine" and in
    # the track record on their profiles, which is the whole basis of the trust
    # display. It simply stops being browsable.

    if request.GET.get("partial"):
        return render(request, "accounts/_feed_items.html", context)

    # Which half of the page is showing. Anything that is not the workers tab
    # is the work tab, so a hand-typed ?show=nonsense lands on the feed rather
    # than on a blank page.
    showing = SHOW_WORKERS if request.GET.get("show") == SHOW_WORKERS else SHOW_WORK
    context["showing"] = showing

    # Only the visible half is queried. The tabs are links, so the other side
    # costs a request when it is asked for and nothing at all until then.
    #
    # Outside the partial branch on purpose: the scroll loader appends
    # _feed_items.html once per page, so a worker card left in there would
    # arrive again every time somebody scrolled.
    if showing == SHOW_WORKERS:
        context["workers"] = (
            WorkerProfile.objects.select_related("user", "region")
            .prefetch_related("trades")
            .exclude(user=request.user.pk if request.user.is_authenticated else None)
            .order_by("-created_at")[:PREVIEW_WORKERS]
        )

    return render(request, "accounts/feed.html", context)


@login_required
def dashboard(request):
    """Your account: roles, profiles, and the things only you act on."""
    if request.user.needs_role_selection:
        return redirect("accounts:select_role")
    return render(request, "accounts/dashboard.html")


@login_required
def details(request):
    """Name, face, age — the things that belong to the person, not the role."""
    if request.method == "POST":
        form = AccountDetailsForm(
            request.POST, request.FILES, instance=request.user
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect("accounts:details")
    else:
        form = AccountDetailsForm(instance=request.user)
    return render(request, "accounts/details.html", {"form": form})


@login_required
def select_role(request):
    """Asked once after the first Google sign-in.

    Creating a profile is what grants a role, so this view creates the
    profile(s) the user asked for and leaves them to fill in the details.
    Re-visiting is safe: it only ever adds roles the user does not yet hold.
    """
    if request.method == "POST":
        form = RoleSelectionForm(request.POST)
        if form.is_valid():
            roles = form.cleaned_data["roles"]
            region = Region.objects.filter(is_active=True).first()
            if region is None:
                # Reference data missing means migrations have not been run.
                # Fail loudly rather than creating half a profile.
                messages.error(
                    request,
                    "No active region is configured. Run migrations before signing up.",
                )
                return redirect("accounts:select_role")

            with transaction.atomic():
                if "worker" in roles and not request.user.is_worker:
                    WorkerProfile.objects.create(user=request.user, region=region)
                if "client" in roles and not request.user.is_client:
                    ClientProfile.objects.create(user=request.user, region=region)
                request.user.last_active_role = roles[0]
                request.user.save(update_fields=["last_active_role"])

            # Send them straight to the profile that needs filling in.
            if "worker" in roles:
                return redirect("accounts:worker_edit")
            return redirect("accounts:client_edit")
    else:
        form = RoleSelectionForm(initial={"roles": request.user.roles})

    return render(request, "accounts/select_role.html", {"form": form})


# ---------------------------------------------------------------------------
# Worker profile
# ---------------------------------------------------------------------------


@login_required
def worker_edit(request):
    profile = getattr(request.user, "worker_profile", None)
    if profile is None:
        messages.info(request, "Add a worker profile to look for work.")
        return redirect("accounts:select_role")

    if request.method == "POST":
        form = WorkerProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            _save_licenses(request, profile)
            messages.success(request, "Profile saved.")
            return redirect("accounts:worker_detail", pk=profile.pk)
    else:
        # Arriving from the chat assistant: the same form, pre-filled with what
        # was confirmed in the conversation. It is still an ordinary unbound
        # form — nothing has been written, corrections happen by typing into
        # the fields, and saving goes through the usual validation.
        prefill = take_handoff(request, "worker_profile")
        if prefill:
            messages.info(
                request,
                "Here's what we filled in together. Check it over, change "
                "anything that isn't right, then save.",
            )
        form = WorkerProfileForm(instance=profile, initial=prefill or None)

    return render(
        request,
        "accounts/worker_edit.html",
        {
            "form": form,
            "profile": profile,
            "photo_form": PortfolioPhotoForm(),
            "regulated_trades": Trade.objects.filter(requires_license=True),
            "licenses": {lic.trade_id: lic.number for lic in profile.licenses.all()},
        },
    )


def _save_licenses(request, profile: WorkerProfile) -> None:
    """Persist one licence number per regulated trade the worker claims.

    Self-reported and unverified — see TradeLicense's docstring. Licences for
    trades the worker no longer lists are deleted rather than orphaned, so a
    stale electrician's number cannot linger on a profile that has dropped the
    trade.
    """
    claimed = set(profile.trades.filter(requires_license=True).values_list("id", flat=True))

    for trade_id in claimed:
        number = (request.POST.get(f"license_{trade_id}") or "").strip()
        if number:
            TradeLicense.objects.update_or_create(
                worker=profile, trade_id=trade_id, defaults={"number": number}
            )
        else:
            profile.licenses.filter(trade_id=trade_id).delete()

    profile.licenses.exclude(trade_id__in=claimed).delete()


@login_required
@require_POST
def portfolio_add(request):
    profile = get_object_or_404(WorkerProfile, user=request.user)

    if profile.portfolio_photos.count() >= rules.MAX_PORTFOLIO_PHOTOS:
        messages.error(request, f"You can show up to {rules.MAX_PORTFOLIO_PHOTOS} photos.")
        return redirect("accounts:worker_edit")

    form = PortfolioPhotoForm(request.POST, request.FILES)
    if form.is_valid():
        photo = form.save(commit=False)
        photo.worker = profile
        photo.save()
        messages.success(request, "Photo added.")
    else:
        messages.error(request, "That file didn't look like an image.")
    return redirect("accounts:worker_edit")


@login_required
@require_POST
def portfolio_delete(request, pk: int):
    photo = get_object_or_404(PortfolioPhoto, pk=pk, worker__user=request.user)
    photo.delete()
    messages.success(request, "Photo removed.")
    return redirect("accounts:worker_edit")


def worker_detail(request, pk: int):
    """Public worker profile."""
    profile = get_object_or_404(
        WorkerProfile.objects.select_related("user", "region").prefetch_related(
            "trades", "licenses__trade", "portfolio_photos", "availability_dates"
        ),
        pk=pk,
    )
    return render(
        request,
        "accounts/worker_detail.html",
        {
            "profile": profile,
            "is_own": profile.user_id == request.user.pk,
            "reviews": reviews_of(profile),
            # The same days as the badges above them, drawn as months. A client
            # deciding whether to offer next Thursday reads a calendar faster
            # than a list, and a calendar is the only shape that shows the free
            # days between the booked ones.
            "booked_months": month_grids(profile.booked_dates),
        },
    )


# ---------------------------------------------------------------------------
# Client profile
# ---------------------------------------------------------------------------


@login_required
def client_edit(request):
    profile = getattr(request.user, "client_profile", None)
    if profile is None:
        messages.info(request, "Add a client profile to hire.")
        return redirect("accounts:select_role")

    if request.method == "POST":
        form = ClientProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile saved.")
            return redirect("accounts:client_detail", pk=profile.pk)
    else:
        form = ClientProfileForm(instance=profile)

    return render(request, "accounts/client_edit.html", {"form": form, "profile": profile})


def client_detail(request, pk: int):
    profile = get_object_or_404(
        ClientProfile.objects.select_related("user", "region"), pk=pk
    )
    return render(
        request,
        "accounts/client_detail.html",
        {
            "profile": profile,
            "is_own": profile.user_id == request.user.pk,
            "reviews": reviews_of(profile),
            # What they have open right now says more about a client than any
            # of the counters do.
            #
            # Collapsed, and sliced after rather than before: a booking is one
            # thing this client is hiring for, and five days of it took the
            # whole list while looking like five separate jobs going spare. The
            # slice counts bookings now, which is what "five open jobs" was
            # always meant to mean.
            "open_jobs": collapse_groups(
                list(profile.jobs.public().select_related("trade", "region"))
            )[:5],
            # And what is under way. Without this a job disappeared from its own
            # client's profile the moment they confirmed somebody — the page
            # listed only open posts, so accepting an applicant looked like
            # losing the job. Work in hand is also the more useful signal: a
            # client with three jobs running is plainly hiring.
            "current_jobs": collapse_groups(list(
                profile.jobs.filter(
                    state__in=[
                        JobState.ACCEPTED,
                        JobState.ESCROW_HELD,
                        JobState.IN_PROGRESS,
                        JobState.COMPLETED,
                        JobState.ENDED_EARLY,
                    ]
                )
                .select_related("trade", "region", "assigned_worker__user")
                .order_by("gig_date")
            ))[:5],
        },
    )


@login_required
@require_POST
def switch_role(request):
    """Flip which dashboard a dual-role user lands on."""
    role = request.POST.get("role")
    if role in ("worker", "client") and role in request.user.roles:
        request.user.last_active_role = role
        request.user.save(update_fields=["last_active_role"])
    return redirect(request.POST.get("next") or reverse("accounts:home"))
