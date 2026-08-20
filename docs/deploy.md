# Deploying XTISE

One VPS, Docker, nginx in front, Postgres beside it. Everything below assumes
a fresh Ubuntu box and the domain **xtise.gr** already bought.

Read the two warnings first. Both are things that are easy to set now and
expensive to change later.

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
