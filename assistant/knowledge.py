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

from functools import cache
from pathlib import Path

from django.conf import settings

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
    (
        "Who this is for",
        "that most jobs here need no experience, what a helper is expected to "
        "do, how somebody with no trade behind them starts",
    ),
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
    (
        "What the product is and why",
        "what problem it exists to solve, how it decided to solve it, what is "
        "deliberately not built — all of it from the whitepaper",
    ),
)


def topics() -> str:
    """The whitelist, as lines the model can both obey and read back."""
    return "\n".join(f"- {name}: {detail}" for name, detail in TOPICS)


#: Where the published whitepaper lives. The same file served at /whitepaper/.
WHITEPAPER = Path(settings.BASE_DIR) / "docs" / "whitepaper.md"


@cache
def whitepaper() -> str:
    """The whitepaper, as the model's reference on what this product is.

    Read from the file rather than summarised into this module, for the reason
    the rest of the module exists: a paraphrase is a second copy, and a second
    copy drifts. Somebody editing the argument at /whitepaper/ should not also
    have to remember that a chat assistant is quoting an older version of it.

    It is the argument and not the rulebook, and the prompt says so — where the
    whitepaper and the live configuration disagree about a number, the
    configuration wins and the whitepaper is simply out of date.

    English only, and deliberately. The Greek edition is a translation of the
    same argument; carrying both would double the prompt to say one thing
    twice, and the model is told separately which language to answer in.

    Cached for the life of the process. It is a file on disk that changes on
    deploy, and reading it on every message would be a disk read per chat turn
    for content that cannot have changed since the process started.
    """
    try:
        text = WHITEPAPER.read_text(encoding="utf-8")
    except OSError:
        # Deployed without docs/, or a bad path. The assistant loses the
        # product argument and keeps every fact that comes from config — which
        # is the half that must never be wrong. Better a narrower assistant
        # than a stack trace on somebody's first question.
        return ""
    return text.strip()


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


def _experience() -> str:
    """The three levels, from the field's own choices.

    Written out because it is the single most-asked thing on this board and the
    most costly to get wrong in either direction: telling somebody with no
    trade behind them that they need one turns away exactly the person the
    platform exists for, and telling them every job will take them sends them
    to a listing that wanted a time-served electrician.
    """
    from jobs.models import ExperienceWanted

    lines = [f"- {value}: \"{label}\"" for value, label in ExperienceWanted.choices]
    return "\n".join(lines)


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

WHO A JOB IS FOR — THE MOST IMPORTANT THING ON THIS BOARD
- This is not a board where qualified tradespeople bid for work. It is a tradesperson
  who needs a pair of hands, and the person answering may never have held a trowel.
- Every job states what it wants from whoever takes it. Three answers, and the field
  defaults to the first:
{_experience()}
- The board can be filtered to one level in a single tap, and every job card and job
  page shows its level as a badge. Somebody asking "is there anything here for me"
  should be sent to /jobs/?experience=none .
- So the honest answer to "can I work here with no experience" is YES, and it is the
  ordinary case rather than an exception. Say so plainly. What is expected is turning
  up, on time, and doing what the tradesperson shows you.
- A worker profile does not need experience, a licence or a CV either. Years of
  experience can be zero and the profile reads "New" rather than badly.

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
- Posting a job and writing a worker profile both ask ONE question per screen, with a
  progress bar, and nothing is saved until the last one. If somebody finds a form
  long, tell them that — it is a short sequence of single questions, not a wall.
- The chat assistant (you) answers questions and does nothing else. You cannot fill a
  form in for anyone. Say where the form is and that it goes one question at a time.

WHAT YOU CAN BE ASKED ABOUT
{topics()}
Anything outside that list is not something to answer from guesswork.
""".strip()
