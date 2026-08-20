# XTISE

Where a tradesperson finds a pair of hands, and where somebody with no
experience gets their first day on a site.

Most jobs on this board need nobody who already knows the work: a bricklayer
needs someone to carry and mix and learn, and the person who takes that day may
be nineteen and have never held a trowel. So *no experience needed* is a field
on the job rather than a line in a description, it leads every card, and it is
the one filter that answers "is this for me?" in a tap.

The arrangement runs from one day to a permanent place, and the platform
carries the whole of it: the post, the applications, the conversation, the
agreement, the days worked, the money, and what each side says about the other
afterwards.

Built as a Django monolith with server-rendered pages and no JavaScript
framework. Every page works without JS; the interactive parts progressively
enhance markup that already functions.

---

## What it does

**How much experience it needs, said out loud.** Every job answers one of three
things: no experience needed, some helps, or it wants somebody who knows the
trade. It defaults to the first, and that default is an opinion rather than a
shrug — the people this exists to reach have nothing to declare, and a board
that assumes skill turns them away before they have applied.

**Two kinds of post.** A *gig* is one dated shift at a fixed price for that day.
A *standing position* is ongoing work paid at a rate. They behave differently
enough — escrow, sign-off, expiry — that they are separate forms rather than one
form with a mode switch. Between them they cover the arc this market actually
has: one day, then a few, then a place.

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

**An in-app assistant.** Optional, and deliberately narrow: it answers
questions about how the platform works and does nothing else. Its grounding is
the platform's own live configuration plus the whitepaper, read from the file
it is published from, so a fee change or an edited argument reaches it without
anybody restating it. It is handed no tools, so "it cannot act on your behalf"
is a fact about the request rather than a rule the model is asked to keep.

It used to be able to fill a form in by chat as well. That went when posting
and profiles became one question per screen: a conversation collecting the same
values was a slower route to a form you had to look at anyway.

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

### Production

The public domain is **https://xtise.gr**, written down once as `SITE_DOMAIN`
in `config/settings.py`. Templates build their own absolute URLs from the
request, so the same code serves localhost, a phone tunnel and the live site
without knowing which it is on; the only runtime that has to know is email,
which cannot use a relative link.

Turning `DJANGO_DEBUG=False` is what switches on the production posture — the
host list stops defaulting to `*`, CSRF origins are derived from it, and the
HTTPS redirect, HSTS, secure cookies, the forwarded-proto header and the
clickjacking and referrer headers all come on together. That block is keyed on
`DEBUG` rather than living in a separate settings module, because a
production-only file is a file nobody runs until the day it matters.

**[docs/deploy.md](docs/deploy.md)** is the runbook: one VPS, Docker, nginx and
Postgres, from a bare Ubuntu box to a working site — including the three
consoles that have to be told about the domain (Google, Stripe, Brevo) and the
two settings that are cheap now and expensive later.

HSTS is not revocable in any useful sense — a browser that has seen the header
refuses plain HTTP for its duration whatever the server later says — so
`DJANGO_HSTS_SECONDS` exists to start it short on the first deploy.

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
templates/       every page, server-rendered
static/          one stylesheet, one script, no build step
docs/            the whitepaper, and how the interface is put together
```

The interface has a small named component system — JobCard, ExperienceBadge,
ExperienceChips, StepForm and the rest — written down in
[docs/ui.md](docs/ui.md) along with the rules that hold it together: what the
board must always say about experience, why the six-step posting flow keeps no
state on the server, and the tokens a new screen is allowed to use.

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

Around 780 tests, no network calls — Stripe and the assistant are both stubbed.

Stubbing the payment gateway is what makes the suite runnable without keys, and
it hides exactly one thing: a mock accepts any arguments, so a service calling
the gateway wrongly would pass every test and fail on the first real call. Two
things stop that now — the patches are autospec'd, and one test binds the calls
the services make against the signatures that will actually run.

One file is the exception, and it exists because of what stubbing costs. A
mock answers what it was told to answer, so the suite proves things about this
application and nothing about the thing it talks to — it cannot tell you that
Stripe caps an application fee at the captured amount, or that an unknown
session raises the error this code catches. `payments/test_live_stripe.py`
asks Stripe those questions directly. It skips unless `STRIPE_SECRET_KEY` is
set and refuses any key that is not `sk_test_`:

```bash
STRIPE_SECRET_KEY=sk_test_... python manage.py test payments.test_live_stripe
```

Stripe does not promise its events arrive in the order they happened, so the
handlers are written to survive arriving backwards — a `payment_failed` for a
superseded attempt cannot unfund a payment that is held, and an older
`account.updated` cannot overwrite a newer one. Both are tested by delivering
the events in the wrong order.

The concurrency tests do not use threads, which prove nothing on SQLite. Each
race is staged instead: a function is handed an instance that says one thing
while the database says another, which is precisely what a concurrent write
leaves behind and happens the same way every run. Two simultaneous fundings,
two deliveries of one webhook, a release meeting a dispute, a booking whose
third day fails after the first two have been captured.
The interesting assistant tests are the ones where the stubbed model misbehaves
on purpose — taking an instruction out of a user's message, or returning
nothing at all — and the one that checks it is handed no tools, which is what
makes the rest of that moot.

---

## Notes on the code

A few conventions worth knowing before changing anything:

- **Comments explain *why*, not *what*.** Most of them are load-bearing history
  — a note saying a step was removed and why is what stops it being helpfully
  reintroduced.
- **One source of truth per fact.** The assistant reads fees from
  `business_rules`, the lifecycle from the state machine, the experience levels
  from the field's own choices, and the product argument from `docs/whitepaper.md`.
  The posting flow's steps are declared on the form class, not in the template.
  Nothing is written down twice, so a change in one place shows up everywhere it
  should — and nothing quietly keeps quoting last year's number.
- **The server is the authority.** The model is never trusted with anything that
  matters, because it is not trustworthy with anything that matters. The
  strongest form of that is not a rule in a prompt: the assistant is handed no
  tools and its endpoint writes nothing.
- **Money is `Decimal`.** Never `float`. A float fee eventually produces a payout
  off by a cent, and cents here belong to real people.

## Licence

Not currently licensed for reuse.
