"""Django settings.

Business rules do NOT live here — see ``config/business_rules.py``. This file
is for infrastructure: what database, which apps, how auth is wired. Keeping
the two apart means changing the platform fee never involves opening the file
that also controls ``DEBUG``.
"""

from pathlib import Path

from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# override=True so the file wins over anything already in the environment. The
# default is the opposite, which means a stale shell variable — including one
# set to the empty string — silently shadows the real value in .env and the
# symptom shows up much later as a blank credential.
load_dotenv(BASE_DIR / ".env", override=True)


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-change-before-deploying"
)
DEBUG = _env_bool("DJANGO_DEBUG", True)
#: The production domain, and the only place it is written down. Templates build
#: their own absolute URLs from the request — see the Open Graph block in
#: base.html — so nothing but this file and an email have to know it.
#:
#: Overridable because a staging host is a real thing and a hardcoded domain is
#: how staging ends up sending people to production.
SITE_DOMAIN = os.getenv("SITE_DOMAIN", "xtise.gr").strip().lower()

ALLOWED_HOSTS = [h for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

# Tunnelling the dev server — ngrok, to open the site on a real phone — puts an
# HTTPS origin in front of a plain-HTTP runserver, and three things break:
#
#   1. ALLOWED_HOSTS. The request arrives with the tunnel's Host, not localhost.
#   2. CSRF. Django checks Origin against CSRF_TRUSTED_ORIGINS for any HTTPS
#      request, and a permissive ALLOWED_HOSTS does not cover it — every POST
#      fails with "does not match any trusted origins" until the scheme+host is
#      named here.
#   3. request.is_secure(). The tunnel terminates TLS and forwards HTTP, so
#      Django builds http:// absolute URLs — including the Google OAuth
#      callback, which Google then rejects for not matching the registered
#      redirect URI. The forwarded-proto header is what tells it otherwise.
#
# One switch, because these are never wanted individually. Unset — the normal
# case — none of it applies and a localhost run is untouched.
TUNNEL_HOST = os.getenv("DJANGO_TUNNEL_HOST", "").strip()

if TUNNEL_HOST:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, TUNNEL_HOST]
    CSRF_TRUSTED_ORIGINS = [f"https://{TUNNEL_HOST}"]
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True


# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------
# Keyed on DEBUG being off rather than on a separate settings module, which is
# how the two get to disagree: a production-only file is a file nobody runs
# until the day it matters. Everything below is inert on a developer's machine
# and is what the live site needs to be true.
#
# The host list stops defaulting to "*" as well. A permissive ALLOWED_HOSTS on
# a public site is how a request arrives with somebody else's Host header and
# leaves with a password-reset link pointing at their domain.

if not DEBUG:
    if ALLOWED_HOSTS == ["*"]:
        ALLOWED_HOSTS = [SITE_DOMAIN, f"www.{SITE_DOMAIN}"]

    # Django checks Origin against this for every HTTPS POST, and a permissive
    # ALLOWED_HOSTS does not cover it. Built from the hosts rather than written
    # out, so the two lists cannot drift.
    CSRF_TRUSTED_ORIGINS = [
        f"https://{host.lstrip('.')}"
        for host in ALLOWED_HOSTS
        if host not in ("*", "localhost", "127.0.0.1")
    ]

    # TLS terminates at the proxy and HTTP is forwarded on, which is what tells
    # Django to build https:// URLs — including the OAuth callback, which Google
    # rejects if the scheme is wrong.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SSL_REDIRECT", True)

    # A year, with subdomains, and preload-ready. Set deliberately: HSTS is not
    # revocable in any useful sense — a browser that has seen this header will
    # refuse plain HTTP for the duration whatever the server later says — so
    # DJANGO_HSTS_SECONDS exists to start it short on the first deploy.
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", str(60 * 60 * 24 * 365)))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Lax rather than Strict: the OAuth callback is a cross-site GET landing
    # back here, and Strict would drop the session cookie on exactly that hop.
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"

    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
    X_FRAME_OPTIONS = "DENY"


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # allauth needs the sites framework.
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    # Local
    "core",
    "accounts",
    "jobs",
    "messaging",
    "payments",
    "worklog",
    "assistant",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Directly after security and before everything else, which is where
    # WhiteNoise has to sit: it answers static requests itself and returns
    # without waking sessions, locale or auth for a stylesheet.
    #
    # Serving static from the app rather than from nginx is a deliberate
    # trade. nginx would be marginally faster; it would also mean a static file
    # can 404 because a volume was not mounted or a path drifted, which is a
    # failure that looks like a broken site and reads like a CSS bug.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Between session and common, which is where Django requires it: it reads
    # the language out of the session (set by the switcher in the header) and
    # falls back to the browser's Accept-Language before anything renders.
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Below both of the things it needs: the language locale resolved for this
    # request, and the user auth attached to it.
    "accounts.middleware.RememberLanguage",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Required by allauth.
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                # LANGUAGE_CODE and LANGUAGES, for the switcher and <html lang>.
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Unread-message badge in the header. Cheap enough to run on
                # every page: one COUNT against an indexed FK.
                "messaging.views.unread_count",
                # Which navigation tab is current.
                "core.context.nav",
                # Cache-busting stamp for the stylesheet and script.
                "core.context.assets",
                # Whether to render the assistant launcher at all.
                "assistant.context.assistant",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# SQLite for local development; set DATABASE_URL-style vars for Postgres in
# any deployed environment. Money and state machines both want real
# transactions and real constraints — do not ship escrow on SQLite.

if os.getenv("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB"),
            "USER": os.getenv("POSTGRES_USER", ""),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
# The assistant's rate limit is counted here, and counted with `add` then
# `incr` so that checking and counting are one operation. That only holds if
# every process shares the store: Django's default is per-process memory, so
# under gunicorn with four workers one caller quietly gets four allowances.
#
# The database rather than Redis, and that is a considered choice for an app
# this size. It is one table on a Postgres that already exists, backed up with
# everything else, with no fourth service to run out of memory at 3am. The cost
# is a round trip per check, on a path that already talks to the database.
#
# `createcachetable` is idempotent and runs on every deploy — see the
# entrypoint. Local memory stays the default in development, where there is one
# process and no table.

if os.getenv("POSTGRES_DB"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "app_cache",
        }
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SITE_ID = 1

# Google is the only way in. SOCIALACCOUNT_ONLY disables password login
# entirely, which is the point: there is no password to phish, reset, or leak,
# and no "forgot password" flow to build.
SOCIALACCOUNT_ONLY = True
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]

# Our User sets ``username = None`` — email is the identifier. allauth defaults
# this to "username" and then reaches for that field on the model (to pull its
# validators, to clean it during signup), which raises FieldDoesNotExist. None
# tells allauth the concept does not exist here.
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "none"  # Google has already verified it.
ACCOUNT_UNIQUE_EMAIL = True

# Straight from Google into the app — no interstitial signup form. The next
# thing the user sees is the role picker, which is the only question we
# actually need answered.
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = True

# An account created before Google was wired up (a `createsuperuser` account,
# say) owns its email but has no social account attached. Without these, that
# email is simply "taken": auto-signup is refused, and allauth sends the user
# to a signup form telling them to log in with a password first — impossible
# under SOCIALACCOUNT_ONLY. These treat Google's `email_verified` as proof of
# ownership and log the user into the existing account.
#
# The safety of this rests entirely on the provider being trustworthy, because
# a provider that fabricates a verified email can log into any local account.
# Google is our only provider and is the sole identity source here.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
# ...and attach the social account on that first match, so the link survives a
# later change of email address on either side.
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

SOCIALACCOUNT_ADAPTER = "accounts.adapters.SocialAccountAdapter"

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    }
}

# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------
# Test-mode keys in development. Absent keys are not an error at import time —
# the payment views detect it and say so, so the rest of the app stays usable
# for anyone working on jobs or messaging without Stripe set up.

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
#: Verifies that a webhook really came from Stripe. Without it we would be
#: taking an unauthenticated POST's word for it that money moved.
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Assistant
# ---------------------------------------------------------------------------
# Same posture as Stripe above: an absent key is not an error at import time.
# The widget checks and hides itself, so the rest of the app stays usable for
# anyone working without an OpenAI account.

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

#: A small model on purpose. The assistant asks one short question at a time
#: and returns structured field values through function calling — neither task
#: rewards a frontier model, and usage is a handful of messages per user. Kept
#: as a setting so the choice can be revisited without touching code.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

#: Ceiling on a single reply. The assistant is meant to be terse; a long answer
#: means it has lost the thread, and this bounds what that costs.
OPENAI_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "600"))

#: Seconds before we give up on the API. A chat widget that hangs is worse
#: than one that says it is having trouble.
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))

#: Per-user hourly cap. Not a billing control — a stuck client retrying in a
#: loop is the realistic failure here, and this bounds it.
ASSISTANT_RATE_LIMIT_PER_HOUR = int(os.getenv("ASSISTANT_RATE_LIMIT_PER_HOUR", "60"))

LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_ON_GET = True

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
#
# Nothing here is required to run the app. Unset, mail goes to the console —
# which is the right default for a machine that has no business talking to an
# SMTP server, and means a developer can read exactly what a user would have
# received without configuring anything.
#
# Notifications are never sent from a request. They are written to a table and
# posted by ``manage.py send_notifications`` on a timer, so an SMTP host that
# is slow, down or wrong cannot make somebody's job application hang, and
# nothing is lost while it is being fixed.

EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend"
)
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", True)
#: Seconds. Without it a wedged SMTP connection hangs the sending command
#: forever, and the next cron run stacks up behind it.
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "20"))

DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL", f"XTISE <no-reply@{SITE_DOMAIN}>"
)

#: Where links in an email point. An email is read outside the browser session
#: that caused it, so a relative URL is useless — and this is the only place at
#: runtime where the app has to know its own public address.
#:
#: Defaults to the dev server while DEBUG is on and to the production domain
#: otherwise, so a deploy that forgets to set it sends working links rather
#: than links to 127.0.0.1.
SITE_URL = os.getenv(
    "SITE_URL",
    "http://127.0.0.1:8000" if DEBUG else f"https://{SITE_DOMAIN}",
).rstrip("/")


# ---------------------------------------------------------------------------
# I18n / time
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en"

#: The languages offered in the header switcher. Each needs a catalogue under
#: LOCALE_PATHS; a language listed here without one silently falls back to
#: English, which looks like a broken switcher rather than a missing file.
LANGUAGES = [
    ("en", "English"),
    ("el", "Ελληνικά"),
]

LOCALE_PATHS = [BASE_DIR / "locale"]

# Store everything in UTC; render in the region's local timezone. Dispute and
# approval windows are measured in real elapsed time, so the storage timezone
# must never be the market's.
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static and media
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STATIC_ROOT = BASE_DIR / "staticfiles"

# Hashed filenames and a gzip/brotli copy of each, built by collectstatic.
#
# The hash is what lets these be cached for a year, which matters here more
# than usual: the stylesheet is one 4,000-line file that every page loads. The
# ?v= stamp in the templates stays anyway — it costs nothing and it is what
# makes a file correct in development, where this storage is not used.
#
# Manifest storage fails the build if a file references one that is missing.
# That is the point of choosing it: better a deploy that stops than a site
# that silently loses an asset.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Local disk in development. Deployed environments should swap in object
# storage — CVs and portfolio photos are user uploads and must not live on an
# ephemeral app filesystem.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DATA_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
