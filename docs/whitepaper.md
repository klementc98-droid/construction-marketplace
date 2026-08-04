# Construction's Finest

**A hiring market for the building trades, built around the payment rather than the listing.**

Version 1 · 31 July 2026

---

## 1. Summary

Construction's Finest is a two-sided marketplace connecting construction clients with
tradespeople, for both single dated shifts and ongoing positions. Its distinguishing
design choice is that **the money is the primary object, not an afterthought**: for a
dated gig, the client's funds are authorised and held before the worker travels, and
released to the worker on a timer that no party can stall by going quiet.

This document describes the model, the mechanism, and the deliberate limits of v1.

---

## 2. The problem

Two problems compound in day-rate construction work.

**Payment risk falls entirely on the worker.** The worker delivers first and invoices
after. For a single day's labour, the cost of chasing an unpaid invoice — phone calls,
travel, small-claims paperwork — routinely exceeds the value of the day itself. The
rational response is not to chase, which is precisely why non-payment persists: the
client who doesn't pay faces no consequence and simply hires someone new next week.
Existing job boards do not touch this. They match and withdraw, leaving the hardest
part of the transaction to the party least able to absorb it.

**The cold-start problem is asymmetric.** Workers will not join a board with no jobs;
clients will not post to a board with no workers. Most marketplaces attack this by
gating content behind sign-up, which makes it strictly worse — a tradesperson cannot
evaluate whether the board is worth an account without seeing whether there is work on
it.

## 3. Design response

**The board is public.** Jobs and worker profiles are readable without an account.
Creating an account is required only to *act* — to apply, to post, to be paid. The
shop window stays open.

**Money moves before work does.** On a dated gig, the client's card is authorised for
the full advertised amount before the worker is expected on site. The worker can see
that the funds are held. This converts "will I be paid" from a judgement about a
stranger into a fact visible on the job page.

**Time, not arbitration, is the default resolution mechanism.** Nearly every job ends
without conflict. Building a system that routes all of them through human review would
be slow and expensive. Instead the platform runs timers with defaults that favour the
party who has already performed, and reserves human judgement for the small number of
cases where someone actively objects.

---

## 4. The two products

| | **Gig** | **Standing position** |
|---|---|---|
| Shape | One dated shift | Ongoing role |
| Price | Fixed total for the day | A rate (hourly/daily/weekly), optionally a range |
| Escrow | Yes | No |
| Platform revenue | Percentage of the payout | None in v1 |

These are separate forms with separate fields, not one form that hides half its inputs.
A client posting a gig never sees a rate-range input at all — not disabled, not hidden,
absent. This is enforced structurally rather than by validation: each form can only
produce the shape its type allows.

A standing position carries no escrow because there is no single day that everyone can
agree has happened. The platform introduces the two sides and withdraws. Monetising
this is deferred rather than solved — see §10.

---

## 5. The escrow mechanism

### 5.1 Authorise, then capture

Funding a gig places a **manual-capture authorisation** on the client's card, not a
charge. The money is committed but not taken. It is captured and paid out only when the
work is signed off, and released back if the job is cancelled.

The client is charged exactly the advertised price. The platform fee is deducted from
the worker's payout at release time and never added on top at capture — the number on
the post is the number the client pays, and the worker knows what will land before
accepting.

Fee and payout are computed as a pair, with the payout defined as the remainder rather
than as an independently rounded number. Fee plus payout therefore always equals the
captured total exactly; no cent is created or destroyed by the split. All money is
handled as decimal, never floating point.

### 5.2 The funding window is a card-network constraint

Escrow can be funded at most a few days ahead of the gig date. This is not a product
preference. An uncaptured card authorisation expires — Stripe cancels it after roughly
seven days — so funding a gig three weeks out would leave the hold dead before anyone
turned up. The configured window is deliberately shorter than the expiry so that the
gig itself *plus* the client's approval window still fit inside the authorisation's
life.

This is the sharpest constraint in the v1 design and it shapes the product: the
platform cannot hold money for far-future work. §10 describes the intended escape.

### 5.3 Early finishes and the guaranteed minimum

Either party can flag that a day ended early. Pay is prorated, floored at a guaranteed
minimum number of hours that applies from the moment the worker checks in. A worker who
travelled to a site and was sent home after twenty minutes has still spent their
morning; the floor makes that non-negotiable rather than a matter of goodwill.

A prorated amount releases automatically after a short dispute window, or immediately
if the client approves it.

---

## 6. One state machine, not two

Job status and payment status are modelled as a **single** state machine.

```
posted ─→ accepted ─→ escrow_held ─→ in_progress ─┬─→ completed ─→ paid_out
                                                  └─→ ended_early ─┘
          (any of the above) ─→ disputed ─→ admin resolves ─→ paid_out
                                                           └→ refunded
```

They are not independent variables: a job cannot start before funds are held, and a
payout cannot happen before work is recorded. Two machines would permit the pair to
disagree, and reconciling *"job says completed, payment says unfunded"* is the exact bug
class this design exists to prevent.

Every transition names both the destination **and which actor may make it** — worker,
client, system (time-triggered), or admin. Encoding the actor means "the client can mark
the job complete" cannot be written by accident; the transition table simply will not
allow it. Illegal moves raise rather than writing a nonsense status.

Two states that could easily have been collapsed are kept apart deliberately:

- **`accepted` vs `escrow_held`.** A worker must be able to distinguish "you have the
  job" from "the money is actually there", because only the second is worth crossing
  town for.
- **`cancelled` vs `refunded`.** Cancelling after funding cannot drop to `cancelled`,
  or the escrow would be orphaned. The lifecycle routes it through `refunded` instead.

The interface renders its buttons *from* this table, so the rules and the UI cannot
drift — there is no second list of "what can this person do right now" to forget to
update.

---

## 7. Trust mechanics

**Silence is approval.** When a worker marks a job complete, the client has a fixed
window to approve or dispute. If they do nothing, the money releases to the worker
anyway. The default deliberately favours the party who has already performed: an
unresponsive client must not be able to hold a worker's pay hostage by simply never
logging in again.

**Disputes never auto-resolve.** `disputed` is the one non-terminal state with no timer
out of it. Escrow freezes and a human decides. An automatic release from a dispute
would defeat the entire point of having one.

**Location is evidence, never a gate.** If a client supplies site coordinates, a
check-in records how far away it appeared to be. This is a soft signal on the record for
later review and is *never* a condition of checking in. GPS on a building site is
unreliable — steel, basements, cheap handsets — and a worker who is genuinely present
must never be locked out by their phone.

**Reputation is withheld until it means something.** Completion and dispute percentages
appear on a profile only after a threshold number of finished jobs; below it, profiles
read "New". One bad first day should not brand someone with a 0% completion rate
forever, and a single job at 100% is not evidence of anything either.

**Licences are self-reported and labelled as such.** The platform does not verify trade
licence numbers and says so, in both the place they are entered and the place they are
displayed. An unverified claim presented as verified would be worse than no claim at all.

**Identity is delegated.** Sign-in is Google-only. There is no password stored to phish,
reset, or leak, and no account-recovery flow to attack.

---

## 8. Matching: two symmetric paths

Hiring runs in both directions, and the two paths are deliberate mirrors.

**The worker applies.** Public post, open applications, client selects. Selection is a
single transaction: assigning the worker, closing the job, and passing over every other
applicant happen together or not at all. Unsuccessful applicants receive a definite
answer rather than silence — a fast no is worth more to a tradesperson than a maybe that
never resolves.

**The client offers directly.** A client can write a gig for one named worker. The job
is real from the moment it is sent — same table, same escrow, same lifecycle — but
flagged private, so it never reaches the public board and is not visible to anyone else.
Accepting is then an ordinary state transition on a job that already exists, rather than
a conversion step that copies fields across and could get one wrong.

A private offer opens a message thread at the moment it is sent, with the client's
covering note as the first message. Someone who wants to ask one question about an offer
should not have to decline it in order to get a reply channel.

If an offer is declined, the client can publish the same job to the public board rather
than retyping it — but only once nobody is holding an unanswered offer to it, or the post
would appear publicly while a worker still believed it was theirs to accept.

---

## 9. Economics

Revenue in v1 comes from a single percentage of each gig payout, deducted at release.
There are no listing fees, no subscriptions, and no charge to browse.

The platform takes nothing from standing positions, and nothing from a job that does not
complete. Revenue is therefore strictly proportional to work that actually got done and
paid for — the incentive is to close jobs successfully, not to maximise listings.

Every configurable rule — the fee, each timer, the guaranteed minimum, the reputation
threshold — lives in exactly one module and can be overridden per environment. No fee or
window is hardcoded anywhere else in the system, including in user-facing copy: the
public "How it works" page reads its numbers from that same module, so it is structurally
incapable of advertising a fee the code does not charge.

---

## 10. Deliberate limits of v1

Stated plainly, because a whitepaper that omits them is marketing.

- **Single region, single currency.** Regions are data rather than schema, so a second
  market is a data change — but v1 launches with one, and nothing is localised.
- **Escrow cannot span long lead times.** The card-authorisation expiry (§5.2) caps how
  far ahead a gig can be funded. The intended fix is to move to separate charges and
  transfers — capturing to a platform balance at funding time and transferring at release,
  which has no expiry. This is not merely an engineering change: it makes the platform
  merchant of record for the full amount, which is a materially larger compliance
  question than v1 should answer.
- **Standing positions are unmonetised.** They are a genuine part of the market and
  currently pay for nothing.
- **Licence verification is not implemented.** Disclosed, not solved.
- **Dispute resolution is manual.** It does not scale as-is, and is not intended to.
- **Development runs on SQLite; production must not.** Money and state machines both
  require real transactions and real constraints.

---

## 11. Direction

In rough order of dependency:

1. Separate charges and transfers, lifting the funding-window ceiling (§5.2).
2. Notifications outside the app — a decision waiting in a browser tab is a decision
   not made. An offer for Thursday read on Friday is worth nothing.
3. Ratings and structured dispute evidence, so manual resolution has something to
   stand on.
4. A second region, exercising the assumption that regions are data.
5. Monetising standing positions, most plausibly on placement rather than subscription.
