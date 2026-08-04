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
  the cent. Deducted from the WORKER'S PAYOUT at release, never added to the client's
  charge. A gig advertised at $400 costs the client exactly $400.
- Fee and payout are computed as a pair; payout is the remainder, so they always
  sum to the captured total exactly.
- Currency: {rules.CURRENCY.upper()}. Single currency.
- Standing positions carry NO escrow and NO fee. Only dated gigs run through escrow.

ESCROW TIMING
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
- Gig: one dated shift, fixed total price. Runs through escrow.
- Standing position: ongoing role paid at a rate (optionally a range). No escrow.

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
""".strip()
