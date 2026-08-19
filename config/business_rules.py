"""Every configurable business rule on the platform, in exactly one place.

Nothing else in the codebase may hardcode a fee, a window length, or a
threshold. If you find yourself typing a number that a non-engineer might one
day want changed, it belongs here.

Each value can be overridden by an environment variable, so staging can run a
2-minute dispute window without a code change and production can move the
platform fee without a deploy.

Money is ``Decimal`` throughout, never ``float``. A float platform fee will
eventually produce a payout that is off by a cent, and cents belong to real
people here.
"""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _hours(name: str, default_hours: float) -> timedelta:
    return timedelta(hours=float(os.getenv(name, str(default_hours))))


# ---------------------------------------------------------------------------
# Platform economics
# ---------------------------------------------------------------------------

#: Platform's cut, deducted from the worker's payout at release time.
#: 0.12 == 12%. Applied once, at payout — never at capture, or the client
#: would be charged the fee on top of the advertised gig price.
PLATFORM_FEE_PCT: Decimal = _decimal("PLATFORM_FEE_PCT", "0.12")

#: Currency for all amounts. Single-currency in v1; kept here so the
#: assumption is visible rather than scattered through Stripe calls.
CURRENCY: str = os.getenv("CURRENCY", "eur")

#: What the reader sees in front of a number. Derived from CURRENCY rather than
#: written out again, so the symbol on a page and the code sent to Stripe cannot
#: disagree — which is the one mistake here nobody would notice until a
#: statement arrived. An unknown code falls back to the code itself, which is
#: ugly and correct; a wrong symbol is neither.
_SYMBOLS = {"eur": "€", "usd": "$", "gbp": "£"}
CURRENCY_SYMBOL: str = _SYMBOLS.get(CURRENCY.lower(), CURRENCY.upper() + " ")


# ---------------------------------------------------------------------------
# Escrow and dispute timing
# ---------------------------------------------------------------------------

#: After a worker or client flags "ended early", how long the other party has
#: to dispute before the prorated payment is released automatically.
EARLY_END_DISPUTE_WINDOW: timedelta = _hours("EARLY_END_DISPUTE_WINDOW_HOURS", 2)

#: After a job is marked complete, how long the client has to approve or
#: dispute before funds auto-release to the worker. Silence means approval:
#: an unresponsive client must not be able to strand a worker's pay.
CLIENT_APPROVAL_WINDOW: timedelta = _hours("CLIENT_APPROVAL_WINDOW_HOURS", 48)

#: Minimum hours a worker is paid for once they have checked in, regardless of
#: how early the job ends. Protects a worker who travelled to site and was sent
#: home after twenty minutes. Applies only after check-in.
MINIMUM_GUARANTEED_HOURS: Decimal = _decimal("MINIMUM_GUARANTEED_HOURS", "2")

#: How far ahead of a gig the client may fund escrow.
#:
#: This is not a product preference — it is a card-network fact. An uncaptured
#: authorisation expires (Stripe cancels it after roughly 7 days), so funding a
#: gig three weeks out would leave the hold dead before anyone turned up. The
#: window is deliberately shorter than 7 days so that the gig itself plus the
#: client's approval window still fit inside the authorisation.
#:
#: TODO(v2): lift this by moving to separate charges and transfers — capture to
#: the platform balance at funding time and transfer to the worker at release.
#: That has no expiry, but it makes the platform the merchant of record for the
#: full amount, which is a bigger compliance question than v1 should answer.
ESCROW_AUTHORIZATION_MAX_DAYS: int = _int("ESCROW_AUTHORIZATION_MAX_DAYS", 5)


# ---------------------------------------------------------------------------
# Check-in verification
# ---------------------------------------------------------------------------

#: How far from the job site a check-in can be and still look normal, in
#: metres. This is a SOFT signal recorded on the check-in for later review —
#: never a gate. GPS is unreliable on a job site (steel, basements, cheap
#: handsets), and a worker who is genuinely present must never be blocked from
#: checking in because their phone put them across the street.
CHECKIN_GEOFENCE_RADIUS_M: int = _int("CHECKIN_GEOFENCE_RADIUS_M", 500)


# ---------------------------------------------------------------------------
# Trust and reputation
# ---------------------------------------------------------------------------

RATING_MIN: int = 1
RATING_MAX: int = 5

#: Completed jobs required before percentage stats (completion rate, dispute
#: rate) are shown as numbers. Below this, profiles display "New" instead.
#: One bad first job should not brand someone with a 0% completion rate
#: forever, and a single job at 100% is not evidence of anything either.
MIN_JOBS_FOR_PUBLIC_STATS: int = _int("MIN_JOBS_FOR_PUBLIC_STATS", 3)


# ---------------------------------------------------------------------------
# Region
# ---------------------------------------------------------------------------

#: v1 launches in a single region. This names the default rather than
#: hardcoding it: regions are rows in ``core.Region``, so adding a second city
#: is a data change, not a schema change. See core/models.py.
DEFAULT_REGION_SLUG: str = os.getenv("DEFAULT_REGION_SLUG", "default-region")
DEFAULT_REGION_NAME: str = os.getenv("DEFAULT_REGION_NAME", "Launch City")
DEFAULT_REGION_TIMEZONE: str = os.getenv("DEFAULT_REGION_TIMEZONE", "America/New_York")
#: ISO 3166-1 alpha-2, and it reaches Stripe: a worker's Connect account is
#: opened in the country of the region they work in. Country decides which
#: capabilities exist, what onboarding asks for and whether payouts are
#: possible at all, so it is not a field anyone can default their way through
#: — ``create_express_account`` used to assume "US" for everybody.
DEFAULT_REGION_COUNTRY: str = os.getenv("DEFAULT_REGION_COUNTRY", "US")


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

MAX_CV_SIZE_MB: int = _int("MAX_CV_SIZE_MB", 10)
MAX_AVATAR_SIZE_MB: int = _int("MAX_AVATAR_SIZE_MB", 5)
MAX_PORTFOLIO_PHOTOS: int = _int("MAX_PORTFOLIO_PHOTOS", 12)
ALLOWED_CV_EXTENSIONS: tuple[str, ...] = ("pdf",)


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

#: Youngest age allowed on the platform. Construction sites are regulated
#: workplaces and this is the floor in most jurisdictions; a market that lets a
#: 15-year-old take a demolition gig has a much larger problem than a bug.
MINIMUM_WORKING_AGE: int = _int("MINIMUM_WORKING_AGE", 18)


def platform_fee_for(amount: Decimal) -> Decimal:
    """The platform's cut of ``amount``, rounded to whole cents.

    Rounds half-up so the split is deterministic and auditable. Pair with
    :func:`worker_payout_for` — never compute one by subtracting an
    independently rounded other, or the two can fail to sum to the total.
    """
    from decimal import ROUND_HALF_UP

    return (amount * PLATFORM_FEE_PCT).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def worker_payout_for(amount: Decimal) -> Decimal:
    """What the worker receives from ``amount`` after the platform fee.

    Defined as the remainder, so fee + payout always equals the captured
    total exactly.
    """
    return amount - platform_fee_for(amount)
