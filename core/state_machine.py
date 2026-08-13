"""The job/payment lifecycle, as one explicit state machine.

The status of a job must never be inferable only by reading several booleans
and guessing. There is one field, it holds one of these states, and every
change goes through :func:`assert_transition`. An illegal move raises rather
than silently writing a nonsense status — money moves off the back of these
states, so "how did this job get to paid_out without escrow?" is not a
question anyone should have to answer from logs.

Job and payment state are deliberately ONE machine, not two. They are not
independent: a job cannot start before funds are held, and a payout cannot
happen before work is recorded. Two machines would allow the pair to disagree,
and reconciling "job says completed, payment says unfunded" is exactly the bug
class this design exists to prevent.

    posted ─→ accepted ─→ escrow_held ─→ in_progress ─┬─→ completed ─→ paid_out
                                                      └─→ ended_early ─┘
              (any of the above) ─→ disputed ─→ admin resolves ─→ paid_out
                                                               └→ refunded
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models
from django.utils.translation import gettext_lazy as _


class JobState(models.TextChoices):
    """Every state a gig can occupy. Stored directly on the job row."""

    # -- before work -------------------------------------------------------
    POSTED = "posted", _("Posted")
    """Open. Workers can see and apply. No worker committed, no money moved."""

    ACCEPTED = "accepted", _("Accepted")
    """A worker is confirmed, but the client has not funded escrow yet.

    Deliberately distinct from ESCROW_HELD. A worker must be able to tell the
    difference between "you have the job" and "the money is actually there",
    because only the second one is worth travelling across town for.
    """

    ESCROW_HELD = "escrow_held", _("Escrow held")
    """Client's funds are captured and held by the platform. Work may begin."""

    # -- during work -------------------------------------------------------
    IN_PROGRESS = "in_progress", _("In progress")
    """Worker has checked in on site."""

    ENDED_EARLY = "ended_early", _("Ended early")
    """Either party flagged an early finish; the short dispute window is open.

    Resolves to PAID_OUT with a prorated amount (floored at the guaranteed
    minimum hours) unless someone disputes first.
    """

    COMPLETED = "completed", _("Completed — awaiting approval")
    """Worker marked the job done. The client's approval window is running."""

    # -- resolution --------------------------------------------------------
    DISPUTED = "disputed", _("Disputed — under review")
    """Escrow is frozen and a human has to look at it. Never auto-resolves."""

    PAID_OUT = "paid_out", _("Paid out")
    """Funds released to the worker, less the platform fee. Terminal."""

    REFUNDED = "refunded", _("Refunded")
    """Funds returned to the client. Terminal."""

    CANCELLED = "cancelled", _("Cancelled")
    """Called off before any work happened. Terminal."""

    EXPIRED = "expired", _("Expired")
    """A gig whose date passed without a worker being confirmed. Terminal."""


#: How each state should read at a glance, as a badge modifier the stylesheet
#: knows how to colour. Purely presentational — it grants nothing and gates
#: nothing — but it lives here, beside the states themselves, so that adding a
#: state without deciding how it looks is a visible omission rather than a
#: badge that silently falls back to grey in six templates.
#:
#: Five tones, not eleven. A reader scanning a list needs "is this live, is it
#: waiting on me, has it gone wrong, is it over" — not a distinct colour per
#: state, which is a legend nobody reads.
STATE_TONES: dict[str, str] = {
    JobState.POSTED: "open",         # taking applications
    JobState.ACCEPTED: "booked",     # somebody has it; money not in yet
    JobState.ESCROW_HELD: "booked",
    JobState.IN_PROGRESS: "live",    # happening right now
    JobState.ENDED_EARLY: "review",  # a window is running
    JobState.COMPLETED: "review",
    JobState.DISPUTED: "alert",      # needs a human
    JobState.PAID_OUT: "done",
    JobState.REFUNDED: "over",       # finished, but nothing was earned
    JobState.CANCELLED: "over",
    JobState.EXPIRED: "over",
}


def state_tone(state: str) -> str:
    """The badge modifier for a state. Unknown states read as neutral."""
    return STATE_TONES.get(state, "over")


class Actor(models.TextChoices):
    """Who is permitted to make a given move.

    SYSTEM covers time-triggered transitions (auto-approve after the client's
    window lapses). ADMIN covers manual dispute resolution. Encoding this
    stops "the client can mark the job complete" from ever being written by
    accident — the transition table simply will not allow it.
    """

    WORKER = "worker", _("Worker")
    CLIENT = "client", _("Client")
    SYSTEM = "system", _("System")
    ADMIN = "admin", _("Admin")


@dataclass(frozen=True)
class Transition:
    to_state: str
    actors: frozenset[str]
    label: str

    def allows(self, actor: str) -> bool:
        return actor in self.actors


def _t(to_state: str, label: str, *actors: str) -> Transition:
    return Transition(to_state=to_state, actors=frozenset(actors), label=label)


#: The single source of truth for what may follow what, and who may do it.
TRANSITIONS: dict[str, tuple[Transition, ...]] = {
    JobState.POSTED: (
        _t(JobState.ACCEPTED, "Confirm worker", Actor.CLIENT),
        # The other direction: the client offered this job to one named worker
        # and the worker said yes. Recorded as the WORKER's move rather than
        # folded into "Confirm worker", because it is the worker's answer that
        # closes the job — writing it as the client's act would put a decision
        # in the audit trail at a moment the client did not make one.
        #
        # This does not let a worker take any open post. The transition table
        # says who may make a move, never to which row; the offer view checks
        # that this worker is the one the offer names, exactly as the check-in
        # view checks the worker is the one assigned.
        _t(JobState.ACCEPTED, "Worker accepts a direct offer", Actor.WORKER),
        _t(JobState.CANCELLED, "Cancel posting", Actor.CLIENT, Actor.ADMIN),
        _t(JobState.EXPIRED, "Gig date passed unfilled", Actor.SYSTEM),
    ),
    JobState.ACCEPTED: (
        _t(JobState.ESCROW_HELD, "Client funds escrow", Actor.CLIENT, Actor.SYSTEM),
        # Either side can still walk away before money is committed. This is a
        # feature: an unfunded acceptance is not yet a promise worth enforcing.
        _t(JobState.CANCELLED, "Cancel before funding", Actor.CLIENT, Actor.WORKER, Actor.ADMIN),
        _t(JobState.EXPIRED, "Never funded before gig date", Actor.SYSTEM),
    ),
    JobState.ESCROW_HELD: (
        _t(JobState.IN_PROGRESS, "Worker checks in on site", Actor.WORKER),
        # Cancelling after funding returns the client's money; it does not
        # simply drop to CANCELLED, or the escrow would be orphaned.
        _t(JobState.REFUNDED, "Cancelled after funding", Actor.CLIENT, Actor.ADMIN),
        _t(JobState.DISPUTED, "Raise a dispute", Actor.CLIENT, Actor.WORKER),
    ),
    JobState.IN_PROGRESS: (
        _t(JobState.COMPLETED, "Worker marks job complete", Actor.WORKER),
        _t(JobState.ENDED_EARLY, "Flag early finish", Actor.WORKER, Actor.CLIENT),
        _t(JobState.DISPUTED, "Raise a dispute", Actor.CLIENT, Actor.WORKER),
    ),
    JobState.ENDED_EARLY: (
        _t(JobState.PAID_OUT, "Dispute window lapsed — release prorated pay", Actor.SYSTEM),
        _t(JobState.DISPUTED, "Dispute the early finish", Actor.CLIENT, Actor.WORKER),
        # An early finish that both sides accept can still be closed out by the
        # client immediately rather than waiting out the window.
        _t(JobState.PAID_OUT, "Client approves early finish", Actor.CLIENT),
    ),
    JobState.COMPLETED: (
        _t(JobState.PAID_OUT, "Client approves release", Actor.CLIENT),
        # Silence is approval. A client who never logs in again must not be
        # able to hold a worker's pay hostage indefinitely.
        _t(JobState.PAID_OUT, "Approval window lapsed — auto-release", Actor.SYSTEM),
        _t(JobState.DISPUTED, "Client disputes completion", Actor.CLIENT),
    ),
    JobState.DISPUTED: (
        # Only a human ends a dispute. There is no timer out of this state by
        # design — an auto-release from DISPUTED would defeat the whole point.
        _t(JobState.PAID_OUT, "Admin resolves for worker", Actor.ADMIN),
        _t(JobState.REFUNDED, "Admin resolves for client", Actor.ADMIN),
    ),
    # Terminal states have no exits.
    JobState.PAID_OUT: (),
    JobState.REFUNDED: (),
    JobState.CANCELLED: (),
    JobState.EXPIRED: (),
}

TERMINAL_STATES: frozenset[str] = frozenset(
    state for state, moves in TRANSITIONS.items() if not moves
)

#: States in which the platform is holding the client's money. Anything that
#: reconciles Stripe against our records should start from this set.
ESCROW_FUNDED_STATES: frozenset[str] = frozenset(
    {
        JobState.ESCROW_HELD,
        JobState.IN_PROGRESS,
        JobState.ENDED_EARLY,
        JobState.COMPLETED,
        JobState.DISPUTED,
    }
)


class IllegalTransition(Exception):
    """Raised when code attempts a move the lifecycle does not permit."""


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def available_transitions(state: str, actor: str | None = None) -> tuple[Transition, ...]:
    """Legal moves out of ``state``, optionally filtered to one actor.

    Templates use this to decide which buttons to render, so the UI and the
    rules can never drift apart — there is no second list of "what the worker
    can do right now" to forget to update.
    """
    moves = TRANSITIONS.get(state, ())
    if actor is None:
        return moves
    return tuple(m for m in moves if m.allows(actor))


def can_transition(from_state: str, to_state: str, actor: str) -> bool:
    return any(
        m.to_state == to_state and m.allows(actor)
        for m in TRANSITIONS.get(from_state, ())
    )


def assert_transition(from_state: str, to_state: str, actor: str) -> None:
    """Raise :class:`IllegalTransition` unless the move is permitted.

    Call this before writing any state change. The error message names all
    three parts, because "invalid state transition" alone is useless at 2am.
    """
    # Callers pass either enum members or raw strings from the database. Both
    # compare correctly (TextChoices subclasses str), but only str() renders
    # readably — f"{Actor.WORKER!r}" is "Actor.WORKER", which is no help in a
    # log line that someone is reading at 2am.
    src, dst, who = str(from_state), str(to_state), str(actor)

    if src == dst:
        raise IllegalTransition(f"Job is already {src!r}.")

    if src in TERMINAL_STATES:
        raise IllegalTransition(
            f"{src!r} is terminal; nothing can follow it "
            f"(attempted {dst!r} by {who!r})."
        )

    if not can_transition(from_state, to_state, actor):
        permitted = sorted(
            {str(m.to_state) for m in available_transitions(from_state, actor)}
        )
        raise IllegalTransition(
            f"{who!r} may not move a job from {src!r} to {dst!r}. "
            f"Permitted for this actor: {permitted or 'nothing'}."
        )
