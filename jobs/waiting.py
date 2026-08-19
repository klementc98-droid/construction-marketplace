"""What is waiting on this person, counted in one place.

Three things can sit unanswered on this board, and each one costs somebody
real time when it goes unseen: an offer nobody replies to, a finished job the
other side never agreed to close, and a rating never left. None of them
announce themselves — they sit inside a list you have to think to open.

Counted here rather than in each view because the same answer is rendered in
two places (the home page and "Mine") and a second copy would drift. Returned
as a small object rather than a dict so a template typo is an empty render
rather than a silently missing badge.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from core.state_machine import JobState

from .models import collapse_groups, count_bookings, Job, Offer, OfferStatus, Review


@dataclass(frozen=True)
class Waiting:
    """Counts of things this user owes an answer to."""

    offers: int = 0
    confirmations: int = 0
    ratings: int = 0

    @property
    def total(self) -> int:
        return self.offers + self.confirmations + self.ratings

    def __bool__(self) -> bool:
        return self.total > 0


def waiting_for(user) -> Waiting:
    """Everything this user is holding up, as counts.

    Cheap enough to run on every page: three counted queries against indexed
    columns, and it is skipped entirely for signed-out visitors.
    """
    if not getattr(user, "is_authenticated", False):
        return Waiting()

    worker = getattr(user, "worker_profile", None)
    client = getattr(user, "client_profile", None)

    # Counted in bookings, not days, because the list this number sends people
    # to is collapsed the same way. "4 offers waiting" over a single four-day
    # booking is a badge that no page can account for.
    offers = (
        count_bookings(
            Offer.objects.filter(
                worker=worker, status=OfferStatus.PENDING, job__state=JobState.POSTED
            ).values_list("job__offer_group", flat=True)
        )
        if worker is not None
        else 0
    )

    # Jobs where the worker has said the work is done and the client has not
    # yet agreed. Only the direct-settlement route: with escrow, the release is
    # the client's move and the workspace already chases it.
    confirmations = (
        count_bookings(
            Job.objects.filter(
                client=client, state=JobState.COMPLETED, use_escrow=False
            ).values_list("offer_group", flat=True)
        )
        if client is not None
        else 0
    )

    # Finished jobs this person has not rated. Counted by walking the finished
    # rows rather than by an anti-join on Review, because "have I rated this?"
    # depends on which side of the job they are — which is a property of the
    # row, not something a single filter expresses.
    #
    # Collapsed to one day per booking *before* the question is asked, not
    # after. A week worked for one client is one rating to leave, and the
    # rating is written against the booking rather than the day — so asking
    # all nine days is nine queries to answer one question, eight of them
    # about rows that never hold the answer.
    ratings = 0
    finished = Job.objects.filter(
        Q(client=client) | Q(assigned_worker=worker),
        state__in=[JobState.PAID_OUT, JobState.CLOSED],
    ).select_related("client", "assigned_worker")
    if client is not None or worker is not None:
        ratings = sum(
            1 for job in collapse_groups(finished) if job.can_be_reviewed_by(user)
        )

    return Waiting(offers=offers, confirmations=confirmations, ratings=ratings)
