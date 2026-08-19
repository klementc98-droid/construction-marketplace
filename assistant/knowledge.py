"""What the Q&A branch is allowed to know, assembled from the running config.

Every number in here is read from :mod:`config.business_rules`, the lifecycle
in :mod:`core.state_machine`, or the database. None of it is written down
twice.

That is the whole point of the module. An assistant that answers "the platform
fee is 12%" from memory is stating a number it has no way to check, and the
day the fee moves it starts quietly misinforming people about their own pay.
Reading it from the same constant the payout code uses means the answer cannot
drift — and if someone changes the fee, the assistant changes with it on the
next request.

The text is deliberately dense and unpolished. It is context for a model, not
copy for a person; the model does the plain-language part.
"""

from __future__ import annotations

from config import business_rules as rules
from core.state_machine import TRANSITIONS, JobState


#: What the Q&A branch will answer about — the whitelist.
#:
#: Two jobs, and the second is the reason it is a list rather than a sentence.
#: It tells the model where its ground truth ends, so "can you look up my bank
#: balance" gets a boundary rather than an invention. And it is what the widget
#: shows a user who does not know what to ask.
#:
#: Kept beside the facts on purpose: a topic added here without the facts to
#: answer it invites exactly the confident wrong answer this module exists to
#: prevent, and the pairing makes that hard to miss. Whitelist and knowledge
#: are updated together or not at all.
TOPICS: tuple[tuple[str, str], ...] = (
    ("Pay and fees", "what you take home, the platform fee and when it applies"),
    (
        "Paying hand to hand or by escrow",
        "the two ways money moves, that direct is the default, who funds escrow and when",
    ),
    ("Check-in and sign-off", "checking in on site, marking a job done, approval"),
    ("Disputes", "how to raise one, what freezes, who decides"),
    ("Posting work", "gigs vs standing positions, multi-day bookings, expiry"),
    ("Applying and offers", "applying, direct offers, countering, being picked"),
    ("Ratings and profiles", "how ratings work, when stats appear, licences"),
    ("Accounts and navigation", "signing in, roles, where to find things"),
)


def topics() -> str:
    """The whitelist, as lines the model can both obey and read back."""
    return "\n".join(f"- {name}: {detail}" for name, detail in TOPICS)


def _hours(delta) -> int:
    return int(delta.total_seconds() // 3600)


def _lifecycle() -> str:
    """The state machine, flattened into lines a model can quote from."""
    lines = []
    for state, moves in TRANSITIONS.items():
        label = JobState(state).label
        if not moves:
            lines.append(f"- {state} ({label}): final state, nothing follows it.")
            continue
        exits = "; ".join(
            f"{m.label} -> {m.to_state} [by {'/'.join(sorted(m.actors))}]"
            for m in moves
        )
        lines.append(f"- {state} ({label}): {exits}")
    return "\n".join(lines)


def _trades() -> str:
    from core.models import Trade

    rows = []
    for trade in Trade.objects.all():
        note = " (regulated — licence expected)" if trade.requires_license else ""
        rows.append(f"{trade.name}{note}")
    return ", ".join(rows) if rows else "none configured yet"


def facts() -> str:
    """The grounding block for the Q&A branch.

    Rebuilt per request. It is a few hundred tokens against a cheap model and
    involves one small query; caching it would mean a fee change needing a
    restart to reach the assistant, which is exactly the drift this avoids.
    """
    return f"""
MONEY
- Platform fee: {rules.PLATFORM_FEE_PCT * 100:.2f}% of the gig payout, rounded half-up to
  the cent. It applies ONLY to a gig running through escrow. Deducted from the
  WORKER'S PAYOUT at release, never added to the client's charge. A gig advertised at
  $400 costs the client exactly $400.
- On a gig settled hand to hand the platform takes NOTHING — no fee, no cut, no
  deduction. The advertised price is what the client hands over and what the worker
  keeps. Never quote the fee at someone who is not using escrow.
- Fee and payout are computed as a pair; payout is the remainder, so they always
  sum to the captured total exactly.
- Currency: {rules.CURRENCY.upper()} ({rules.CURRENCY_SYMBOL}). Single currency.
- Standing positions carry NO escrow. Only dated gigs can use escrow.

THE TWO WAYS MONEY MOVES — DIRECT IS THE DEFAULT, ESCROW IS OPTIONAL
- Paying hand to hand — cash, bank transfer, whatever the two of them arrange between
  themselves — is the DEFAULT and the ordinary case. The platform is not in the middle
  of it: it does not hold the money, does not move it, and takes nothing from it.
- Escrow is OFF by default. A gig runs without it unless both sides agree to use it.
  Most jobs on this board settle directly between the two people, and that is normal
  and expected — not a worse or riskier way to work.
- On a direct gig the worker marks the day done and the client confirms it. There is
  no hold to release, so NOTHING happens on a timer: no automatic release, no approval
  countdown. The two of them settle up between themselves.
- Escrow is agreed as part of the terms, the same way the price is: it can be proposed in
  a counter-offer and it takes effect when the other side accepts. Nobody is forced
  into it and nobody is charged for declining it.
- Never tell someone their money is "held safely" or "protected" unless escrow is
  actually on for that job. On a direct-settlement gig the platform is not holding
  anything, and saying otherwise is the most damaging wrong answer available here.
- Everything below under ESCROW TIMING applies ONLY when escrow is on.

ESCROW TIMING (only when escrow is on)
- A client may fund a gig at most {rules.ESCROW_AUTHORIZATION_MAX_DAYS} days before the
  gig date. Reason: an uncaptured card authorisation expires (~7 days), so a hold
  placed earlier would be dead before the day arrives. This is a card-network limit,
  not a preference.
- Funding places a manual-capture AUTHORISATION — money committed, not yet taken.
- Client approval window after a job is marked complete: {_hours(rules.CLIENT_APPROVAL_WINDOW)} hours.
  If the client says nothing, funds release to the worker automatically. Silence is
  approval, deliberately: an unresponsive client must not be able to strand a worker's pay.
- Early-finish dispute window: {_hours(rules.EARLY_END_DISPUTE_WINDOW)} hours. Either side
  can flag an early end; the prorated amount releases when the window lapses, or
  immediately if the client approves.
- Minimum guaranteed hours once a worker has CHECKED IN: {rules.MINIMUM_GUARANTEED_HOURS}.
  They are paid at least this much however early the day ends. Applies only after check-in.

CHECK-IN
- The worker checks in on site; that is what moves a job to in_progress.
- If the client supplied site coordinates, the check-in records how far away it looked.
  Tolerance {rules.CHECKIN_GEOFENCE_RADIUS_M} metres. This is a SOFT note on the record
  for later review and is NEVER a condition of checking in — GPS on a site is unreliable
  and a worker who is genuinely present must never be blocked.

DISPUTES
- Raising a dispute is open to either side on any gig, but what it can do depends on
  how the gig pays. With no escrow there is no hold to freeze and nothing for an admin
  to move — the record is marked and the money stays wherever it already is.
- A dispute freezes escrow and requires a human to resolve. There is no timer out of a
  dispute, by design — an automatic release would defeat the point of raising one.
- An admin resolves it either to paid_out (worker) or refunded (client).

RATINGS AND PROFILE STATS
- Ratings run {rules.RATING_MIN}-{rules.RATING_MAX}.
- Completion and dispute percentages appear on a profile only after
  {rules.MIN_JOBS_FOR_PUBLIC_STATS} completed jobs. Below that the profile reads "New".
  Rationale: one bad first job should not brand someone forever, and a single job at
  100% is not evidence either.
- Trade licence numbers are SELF-REPORTED. The platform does NOT verify them and says so.

JOB LIFECYCLE (single state machine covering job and payment together)
{_lifecycle()}

THE TWO POST TYPES
- Gig: one dated shift at a fixed price for that day. Can use escrow.
- Standing position: ongoing role paid at a rate (optionally a range). No escrow, no
  single day to sign off.

MULTI-DAY BOOKINGS — ONE JOB, NOT SEVERAL
- A client posting four days creates ONE booking. Everywhere a person looks — the
  board, the feed, their own lists, offers, applications — it is a single entry
  reading "4 days" with the date span, never one card per day.
- Underneath, each day is its own row, because each day carries its own escrow, its
  own sign-off and its own expiry: Tuesday going to dispute must not freeze
  Wednesday's money. That is bookkeeping and NOT something to explain to a user
  unless they ask why a day can be cancelled on its own.
- Applying once applies to the whole booking. Nobody applies for Tuesday and is left
  wondering about Wednesday. Confirming somebody books every day of it.
- The advertised price is PER DAY. A 4-day booking at 150 is 600 in all; lists show
  both the per-day figure and the total, so quote whichever they asked for and do not
  multiply a total by the days again.

GIGS THAT NOBODY TAKES
- A dated gig whose day passes with no worker committed moves to "expired" on its
  own. It is not deleted and it is not a black mark on anyone — it simply stops being
  something people can apply to.
- Expired and cancelled jobs stay on both parties' own lists and in their track
  record. Nothing is erased.

NEGOTIATION
- Either side can counter: a worker proposes terms for themselves, a client answers
  one named person. A counter is a PROPOSAL about the job — it changes nothing until
  it is accepted. The job keeps its posted terms until then.
- A counter can carry a different price, and can also propose using escrow or not.
- Accepting is what writes the new terms. On a multi-day booking, accepting applies
  across the booking rather than to one day.

MESSAGING
- Every offer opens a message thread from the start, so a question can be asked
  without having to decline first. Messages live at /messages/.

TRADES CONFIGURED: {_trades()}

ACCOUNT AND NAVIGATION
- Sign-in is Google only. There is no password to reset.
- One account can hold both roles. Creating a profile is what grants a role.
- Jobs board: /jobs/ . Workers board: /workers/ . Your own jobs and applications:
  /jobs/mine/ . Messages: /messages/ . Payout setup for workers: /payouts/ .
  How-it-works page: /about/ .
- Two ways to get hired: apply to a public post, or receive a direct offer written for
  you by name. A direct offer never appears on the public board. Declining needs no reason.
- Minimum age to use the platform: {rules.MINIMUM_WORKING_AGE}.
- The chat assistant can help fill in a worker profile, a gig or a standing position by
  asking one question at a time. It never saves anything: it opens the real form with
  the answers filled in, and the person presses save themselves.

WHAT YOU CAN BE ASKED ABOUT
{topics()}
Anything outside that list is not something to answer from guesswork.
""".strip()
