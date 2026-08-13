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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Between session and common, which is where Django requires it: it reads
    # the language out of the session (set by the switcher in the header) and
    # falls back to the browser's Accept-Language before anything renders.
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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

# Local disk in development. Deployed environments should swap in object
# storage — CVs and portfolio photos are user uploads and must not live on an
# ephemeral app filesystem.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DATA_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
