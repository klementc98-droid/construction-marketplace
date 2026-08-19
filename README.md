# Construction's Finest

A hiring marketplace for the building trades. Clients post work, tradespeople
apply or receive direct offers, both sides agree the terms, and the money moves
when the day is signed off.

Built as a Django monolith with server-rendered pages and no JavaScript
framework. Every page works without JS; the interactive parts progressively
enhance markup that already functions.

---

## What it does

**Two kinds of post.** A *gig* is one dated shift at a fixed price for that day.
A *standing position* is ongoing work paid at a rate. They behave differently
enough — escrow, sign-off, expiry — that they are separate forms rather than one
form with a mode switch.

**Two ways to get hired.** Apply to something on the public board, or receive a
direct offer written for you by name. A direct offer never appears publicly, and
declining one needs no reason.

**Negotiation before commitment.** Either side can counter with different terms.
A counter is a proposal *about* the job and changes nothing until accepted — the
job keeps its posted terms throughout, which is what makes it safe to charge
against them.

**Escrow, optional.** Off by default. A gig can run through escrow if both sides
agree, in which case the client's card is authorised before the day and captured
at sign-off, minus the platform fee. Most jobs settle directly, and that is a
first-class path rather than a fallback.

**Multi-day bookings are one job.** Posting four days creates one booking that
reads as one entry everywhere — board, feed, offers, applications. Underneath
each day is its own row, because each day carries its own escrow, sign-off and
expiry, and Tuesday going to dispute must not freeze Wednesday's money. That
split is bookkeeping the reader never meets.

**An in-app assistant.** Optional. It answers questions about how the platform
works from the platform's own live configuration, and it can fill in a form by
asking one question at a time with tappable answers. It never writes to the
database — it opens the real form with the answers filled in and the person
presses save.

---

## Stack

| | |
|---|---|
| Framework | Django 6.0 |
| Auth | django-allauth, Google sign-in only |
| Database | SQLite locally, Postgres in production |
| Payments | Stripe Connect, manual-capture authorisations |
| Assistant | OpenAI, function calling |
| Frontend | Server-rendered templates, vanilla JS, one CSS file |
| i18n | English and Greek |

Google, Stripe and OpenAI are each optional at runtime. Leave the keys unset and
the rest of the app runs — the assistant widget hides itself, payments are
skipped, and sign-in tells you what is missing.

---

## Running it locally

Requires Python 3.12+.

```bash
git clone https://github.com/klementc98-droid/construction-marketplace.git
cd construction-marketplace

python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt

cp .env.example .env          # then fill in at least DJANGO_SECRET_KEY

python manage.py migrate
python manage.py seed_demo    # a dev admin and some demo profiles
python manage.py runserver
```

http://127.0.0.1:8000

Sign-in is Google-only, so you need `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` to get past the front door. `python manage.py
check_google` tells you whether the credentials and callback URL line up.

### On a phone, over a tunnel

An HTTPS tunnel in front of a plain-HTTP dev server breaks three things at once:
`ALLOWED_HOSTS`, CSRF origin checks, and `request.is_secure()` — which silently
builds `http://` OAuth callbacks that Google then rejects. One setting handles
all three:

```bash
# .env
DJANGO_TUNNEL_HOST=your-subdomain.ngrok-free.dev
```

```bash
python manage.py runserver 0.0.0.0:8000
ngrok http --url=your-subdomain.ngrok-free.dev 8000
```

The tunnel's callback URL has to be registered with Google as well. Leave the
setting blank for an ordinary localhost run and none of it applies.

---

## Configuration

Everything is environment variables; see `.env.example` for the annotated list.

Business rules — the platform fee, approval windows, minimum guaranteed hours,
the stats threshold — live in **`config/business_rules.py`**, not in settings and
not scattered through the code. Each has an environment override, so staging can
run a two-minute dispute window without a code change.

That separation is deliberate: changing the platform fee should never involve
opening the file that also controls `DEBUG`.

---

## Scheduled work

Two commands need to run on a timer in any real deployment. Both are idempotent
and safe to run often.

```bash
python manage.py settle_due_jobs      # release payments past their window
python manage.py expire_stale_gigs    # retire gigs whose day passed unfilled
python manage.py send_notifications   # post queued emails
python manage.py remind_tomorrow      # tell people about the day they have on
python manage.py reconcile_payments   # ask Stripe what actually happened
```

`settle_due_jobs` is the one that moves money. Without it, a client who says
nothing after a job is marked complete strands the worker's pay indefinitely —
silence is meant to be approval.

`send_notifications` drains the notification table. Emails are written to it
inside the transaction that caused them and never sent from a request, so a
slow or unreachable SMTP host cannot make somebody's job application hang and
nothing is lost while it is being fixed. Run it every few minutes.

`reconcile_payments` is the one that admits Stripe is a separate system. A
capture can succeed and the commit that records it can fail; no ordering inside
a transaction prevents that, because the two halves live in different
databases. So this asks Stripe about every hold that could be behind and moves
the local record to match — Stripe is the authority, and where they disagree
about money the money is right. It also gives back holds left on jobs that
expired or were called off, which is somebody's credit limit frozen for work
that is not going to happen. `--dry-run` reports without writing. Run it every
few minutes; every repair is a conditional claim, so running it often is free.

`remind_tomorrow` tells a worker the night before about the day they have on.
Run it once each evening; running it more often is harmless, because the day is
part of the deduplication key rather than a property of the schedule.

On a development machine there is no cron, and the failure that causes is a
quiet one: every request writes its notification correctly, the table fills up,
and not one email is ever sent. `run_timers` is the stand-in — it runs all four
on their own intervals in one process.

```bash
python manage.py run_timers          # loop until Ctrl-C; leave it beside runserver
python manage.py run_timers --once   # one pass of each, then exit
```

It is a convenience, not a deployment story: it keeps no record of when it last
ran and stops when the terminal closes. Use cron in anything real.

Two things are emailed and nothing else: a direct offer, and that night-before
reminder. Every other event is wired, worded and translated but switched off in
`notifications.services.ENABLED` — an inbox that fills with mail nobody asked
for gets the sender filtered, and after that the important one does not arrive
either. Turning one back on is adding a line to that set.

Leave the mail settings unset and email goes to the console — which is the
right default for a machine with no business talking to an SMTP server, and
lets you read exactly what a user would have received.

---

## Layout

```
config/          settings, URLs, and business_rules.py
core/            regions, trades, the job/payment state machine
accounts/        users, worker and client profiles, the home feed
jobs/            posts, applications, offers, counters, reviews
messaging/       conversations between the two sides of a job
payments/        Stripe Connect, escrow, webhooks
worklog/         check-in, sign-off, settlement
assistant/       the in-app chat helper
```

One state machine in `core/state_machine.py` covers the job and its payment
together. A job's state and its money's state are the same fact, and modelling
them separately is how you end up with a completed job holding an uncaptured
authorisation.

One machine, but still two rows — the job and the escrow — and they are kept in
step by claiming each with a conditional UPDATE and *checking both answers*.
That second half was missing, and the gap was exactly the size of the sentence
above: a release could capture the money into a job somebody had just moved to
disputed. The job is claimed first now, before the escrow and long before
Stripe, so losing that race costs nothing.

Stripe is a third system and no amount of this makes it transactional with the
database. A capture that succeeds followed by a commit that fails leaves the
two disagreeing, and the answer to that is reconciliation rather than a bigger
transaction. It is not written yet.

---

## Tests

```bash
python manage.py test
```

Around 650 tests, no network calls — Stripe and the assistant are both stubbed.

Stubbing the payment gateway is what makes the suite runnable without keys, and
it hides exactly one thing: a mock accepts any arguments, so a service calling
the gateway wrongly would pass every test and fail on the first real call. Two
things stop that now — the patches are autospec'd, and one test binds the calls
the services make against the signatures that will actually run.

The concurrency tests do not use threads, which prove nothing on SQLite. Each
race is staged instead: a function is handed an instance that says one thing
while the database says another, which is precisely what a concurrent write
leaves behind and happens the same way every run. Two simultaneous fundings,
two deliveries of one webhook, a release meeting a dispute, a booking whose
third day fails after the first two have been captured.
The interesting assistant tests are the ones where the stubbed model misbehaves
on purpose: declaring a form finished halfway through, claiming values it never
heard, or taking an instruction out of a user's message.

---

## Notes on the code

A few conventions worth knowing before changing anything:

- **Comments explain *why*, not *what*.** Most of them are load-bearing history
  — a note saying a step was removed and why is what stops it being helpfully
  reintroduced.
- **One source of truth per fact.** The assistant's schema is derived from the
  real Django forms; its answer buttons come from the same fields; its knowledge
  of fees is read from `business_rules`. Nothing about a form is written down
  twice, so a field added in one place appears everywhere it should.
- **The server is the authority.** The assistant's conversation state, the branch
  it is in, and whether a form is complete are all decided server-side. The model
  is never trusted with anything that matters, because it is not trustworthy with
  anything that matters.
- **Money is `Decimal`.** Never `float`. A float fee eventually produces a payout
  off by a cent, and cents here belong to real people.

## Licence

Not currently licensed for reuse.
