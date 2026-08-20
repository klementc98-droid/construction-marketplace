# Deploying XTISE

Two arrangements, and the first is temporary on purpose.

**From a PC**, with a Cloudflare tunnel, while there is no server yet — see
[Interim](#interim-serving-xtisegr-from-a-pc) below. The site is up while the
machine is awake and nothing about it is migrated later; it is thrown away.

**On a VPS**, with Docker, nginx and Postgres — everything from step 1 onwards.
Assumes a fresh Ubuntu box.

Read the two warnings first either way. Both are cheap to set now and expensive
to change later, and the first one is written into the database the very first
time the app runs.

---

## ⚠ Set the region before the first migrate

The launch region is written into the database by a migration, from environment
variables, **the first time you migrate**. Changing them afterwards does
nothing — the row already exists.

Its country reaches Stripe: a worker's Connect account is opened in their
region's country, which decides what onboarding asks for and whether payouts
work at all. Moving a live Connect account to another country is not something
you do from a settings file.

So before anything else, in `.env`:

```
DEFAULT_REGION_NAME=Αθήνα
DEFAULT_REGION_SLUG=attica
DEFAULT_REGION_TIMEZONE=Europe/Athens
DEFAULT_REGION_COUNTRY=GR
CURRENCY=eur
```

## ⚠ Start HSTS short

`DJANGO_HSTS_SECONDS` defaults to a year. HSTS is not revocable in any useful
sense: a browser that has seen the header refuses plain HTTP for the whole
duration whatever the server later says. Deploy with `DJANGO_HSTS_SECONDS=300`,
confirm HTTPS works properly, then raise it.

---

## Interim: serving xtise.gr from a PC

Before there is a server, the domain can point at a machine on a desk. This is
a real arrangement with one honest limitation — the site is up only while that
machine is awake — and everything in it is thrown away, not migrated, when the
VPS arrives. Skip to step 1 below if you already have the server.

**Cloudflare Tunnel, not port forwarding.** A home connection in Greece is
usually behind CGNAT, which means there is no public address to forward a port
to; and even where there is, opening 443 on a home router to a Windows box is a
bad trade. The tunnel dials out, so nothing is exposed, no port is opened, and
the certificate is Cloudflare's problem rather than yours.

The cost is that the domain's nameservers move to Cloudflare. That is free, and
it is where they will want to stay afterwards anyway.

1. Add `xtise.gr` at [dash.cloudflare.com](https://dash.cloudflare.com), then
   change the nameservers at the registrar to the two it gives you. Minutes to
   a few hours.

2. On the machine:

   ```
   winget install Cloudflare.cloudflared
   cloudflared tunnel login
   cloudflared tunnel create xtise
   cloudflared tunnel route dns xtise xtise.gr
   cloudflared tunnel route dns xtise www.xtise.gr
   ```

3. `%USERPROFILE%\.cloudflared\config.yml`:

   ```yaml
   tunnel: xtise
   credentials-file: C:\Users\YOU\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: xtise.gr
       service: http://localhost:8000
     - hostname: www.xtise.gr
       service: http://localhost:8000
     - service: http_status:404
   ```

4. `.env`, and the three lines that differ from a real deployment:

   ```
   DJANGO_DEBUG=False
   DJANGO_ALLOWED_HOSTS=xtise.gr,www.xtise.gr,127.0.0.1
   SITE_URL=https://xtise.gr
   DJANGO_SSL_REDIRECT=False
   DJANGO_HSTS_SECONDS=0
   ```

   `DEBUG` must be off. On a public domain a traceback page is a settings dump,
   and this app's settings contain Stripe keys.

   The redirect is off because Cloudflare already serves the site over HTTPS and
   forwards plain HTTP to the tunnel; leaving it on risks a loop for no gain.
   HSTS is zero because this arrangement is temporary and HSTS is not — a
   browser that sees it refuses plain HTTP to xtise.gr for the whole duration,
   long after this machine has stopped being the server.

5. Static files, once per deploy of new code:

   ```
   python manage.py collectstatic --noinput
   ```

   They are served by WhiteNoise from inside the app, so there is no nginx and
   nothing to configure. With `DEBUG` off nothing else serves them, which is
   why this step is not optional.

6. Run it, in two terminals:

   ```
   pip install waitress
   waitress-serve --listen=127.0.0.1:8000 config.wsgi:application
   ```

   ```
   python manage.py run_timers
   ```

   `waitress`, not `gunicorn`: gunicorn is POSIX-only and will not start on
   Windows. `runserver` would also work and is a development server that says
   so — waitress is a real one and is one `pip install` away.

   The second terminal is what sends email, expires stale gigs and settles
   finished ones. Without it the app looks fine and quietly never sends
   anything, which is the exact failure `run_timers` was written for.

7. Turn off sleep. Settings → System → Power → Screen and sleep → *When plugged
   in, put my device to sleep*: **Never**. A sleeping machine is a domain that
   returns nothing, and Google will notice during OAuth verification.

Then do step 6 below — Google, and Brevo if you want email. Stripe can wait;
see the note there.

---

## 1. DNS

At your registrar, two records pointing at the VPS:

| Type | Name | Value |
| --- | --- | --- |
| A | `@` | your server's IPv4 |
| A | `www` | your server's IPv4 |

Add `AAAA` records too if the box has IPv6. Wait for them to resolve —
`dig +short xtise.gr` from anywhere — before asking for a certificate, or the
request fails and you burn a rate limit.

Brevo will also give you three or four records (DKIM, SPF, DMARC) for sending
mail as `no-reply@xtise.gr`. Add them at the same time; see step 6.

## 2. The box

```bash
ssh root@your-server
apt update && apt upgrade -y
apt install -y docker.io docker-compose-v2 git ufw

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable

git clone https://github.com/klementc98-droid/construction-marketplace.git xtise
cd xtise
```

Nothing else needs installing. Python, Postgres and nginx all live in
containers.

## 3. `.env`

```bash
cp .env.example .env
nano .env
```

The production shape, in full:

```
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<paste the output of the command below>
DJANGO_ALLOWED_HOSTS=xtise.gr,www.xtise.gr
SITE_URL=https://xtise.gr
DJANGO_HSTS_SECONDS=300

POSTGRES_DB=xtise
POSTGRES_USER=xtise
POSTGRES_PASSWORD=<a long random one>

DEFAULT_REGION_NAME=Αθήνα
DEFAULT_REGION_SLUG=attica
DEFAULT_REGION_TIMEZONE=Europe/Athens
DEFAULT_REGION_COUNTRY=GR

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...

EMAIL_HOST=smtp-relay.brevo.com
EMAIL_PORT=587
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=XTISE <no-reply@xtise.gr>

OPENAI_API_KEY=
```

A key nobody has ever seen:

```bash
docker run --rm python:3.14-slim python -c \
  "import secrets; print(secrets.token_urlsafe(64))"
```

`.env` is gitignored and must stay that way. `chmod 600 .env`.

## 4. The certificate, before the first start

nginx is configured for TLS, so it will not start until the certificate files
exist. Get them first, with certbot binding port 80 on its own:

```bash
docker compose run --rm -p 80:80 --entrypoint "" certbot \
  certbot certonly --standalone \
  -d xtise.gr -d www.xtise.gr \
  --email you@example.com --agree-tos --no-eff-email
```

Renewal after this is automatic — the `certbot` service wakes twice a day and
renews anything within thirty days of expiry, and nginx reloads every twelve
hours to pick it up.

## 5. Start it

```bash
docker compose up -d --build
docker compose logs -f web
```

The entrypoint migrates and creates the cache table before gunicorn starts. If
a migration fails the container stops rather than serving a half-migrated
database, which is deliberate — check the logs rather than restarting it.

Then an administrator, who is the only person who can resolve a dispute:

```bash
docker compose exec web python manage.py createsuperuser
```

## 6. The console work

Three services need to be told about the domain. None of this is in the repo.

**Google** — [console.cloud.google.com](https://console.cloud.google.com/apis/credentials),
on the OAuth client:

- Authorized JavaScript origin: `https://xtise.gr`
- Authorized redirect URI: `https://xtise.gr/accounts/google/login/callback/`

Then the **OAuth consent screen**: if it is still in *Testing*, only listed test
users can sign in and everybody else gets an error. Press **Publish app**.

**Stripe** — the account has to be activated for live payments (business
details, IBAN) and the **Connect platform profile** completed, or no worker can
open a payout account. Then add a webhook endpoint:

- URL: `https://xtise.gr/stripe/webhook/`
- Events: the four the app handles, and no others —
  `checkout.session.completed`, `payment_intent.amount_capturable_updated`,
  `payment_intent.payment_failed`, `account.updated`. Anything else is
  acknowledged and ignored, so subscribing to more only adds noise.
- Copy the signing secret into `STRIPE_WEBHOOK_SECRET`

**Brevo** — authenticate the domain (Senders → Domains) and add the DKIM/SPF
records it gives you, or mail from `no-reply@xtise.gr` lands in spam or bounces.

Brevo also keeps an **authorized IP list** — the same thing that produced
`525 Unauthorized IP` in development. Add the server's IP, or nothing sends.

## 7. Check it

```bash
curl -sI https://xtise.gr | head -5          # 200, and HSTS present
curl -sI http://xtise.gr | head -3           # 301 to https
docker compose exec web python manage.py check --deploy
```

`check --deploy` should be quiet. Then open the site, sign in with Google, and
post a job — the flow that touches auth, the database, static files and email
in one go.

---

## Updating

```bash
git pull
docker compose up -d --build
```

Migrations run on start. The image is rebuilt for both `web` and `ticker`, so
the two cannot end up on different versions of the code.

## Backups

Two things are not in the image and cannot be rebuilt: the database and the
uploads.

```bash
# Postgres
docker compose exec -T db pg_dump -U xtise xtise | gzip > db-$(date +%F).sql.gz

# CVs and portfolio photos
docker run --rm -v xtise_media:/m -v "$PWD":/out alpine \
  tar czf /out/media-$(date +%F).tar.gz -C /m .
```

Put both on a cron and copy them off the box. A backup on the same disk as the
thing it is backing up is not a backup.

## What runs where

| Service | What it is |
| --- | --- |
| `web` | gunicorn, 3 workers. Static served by WhiteNoise from inside the image. |
| `ticker` | The same image running `run_timers`: queued email every minute, reconciliation every five, expiry/settlement/reminders hourly. |
| `db` | Postgres 17, on a named volume. |
| `nginx` | TLS, the ACME challenge, `/media/`, and a proxy to `web`. |
| `certbot` | Renewal, twice a day. |

## Things that will bite

**A worker cannot get paid.** Almost always Stripe: the account is not live, or
the Connect profile is incomplete, or the region's country is wrong. Check
`docker compose exec web python manage.py reconcile_payments --dry-run` first —
it reports divergence between Stripe and the database without changing
anything.

**Email stops.** Brevo's authorized IPs, or the domain records. Queued mail is
not lost — `send_notifications` retries it — so fix the cause and it drains.

**The assistant answers "I'm not sure" about everything.** It reads
`docs/whitepaper.md` at runtime. If `docs/` is missing from the image it fails
softly, which is why this is worth knowing: `.dockerignore` deliberately keeps
`docs/`.

**A stylesheet 404s after a deploy.** Static files are hashed at build time and
served from inside the image, so this should not happen — if it does, the build
skipped `collectstatic`, which means the build failed and an old image is
running.
