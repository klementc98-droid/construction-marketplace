"""Verify the Google OAuth wiring and print the exact values Google needs.

Most OAuth setup failures are a redirect URI that differs from the registered
one by a trailing slash or by localhost-vs-127.0.0.1. Google's error for that
is `redirect_uri_mismatch`, which tells you there is a mismatch but not what
the app actually sent. This prints what we will send, so it can be pasted
rather than retyped.
"""

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand

CALLBACK_PATH = "/accounts/google/login/callback/"


class Command(BaseCommand):
    help = "Check Google OAuth configuration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--host",
            default="http://127.0.0.1:8000",
            help="Origin the app is served from (default: http://127.0.0.1:8000)",
        )

    def handle(self, *args, **options):
        host = options["host"].rstrip("/")
        out = self.stdout
        ok = self.style.SUCCESS
        bad = self.style.ERROR
        warn = self.style.WARNING

        app = (settings.SOCIALACCOUNT_PROVIDERS or {}).get("google", {}).get("APP", {})
        client_id = app.get("client_id") or ""
        secret = app.get("secret") or ""

        out.write("\nPaste these into the Google Cloud console")
        out.write("  (APIs & Services -> Credentials -> your OAuth client):\n")
        out.write(f"  Authorized JavaScript origin : {host}")
        out.write(f"  Authorized redirect URI      : {host}{CALLBACK_PATH}\n")

        out.write("Local configuration:")

        if client_id:
            masked = client_id[:14] + "…" + client_id[-14:] if len(client_id) > 30 else client_id
            out.write(ok(f"  GOOGLE_CLIENT_ID     set  ({masked})"))
            if not client_id.endswith(".apps.googleusercontent.com"):
                out.write(
                    warn(
                        "    ...but it doesn't end in .apps.googleusercontent.com — "
                        "that's usually a copied project id, not the client id."
                    )
                )
        else:
            out.write(bad("  GOOGLE_CLIENT_ID     MISSING — add it to .env"))

        out.write(
            ok("  GOOGLE_CLIENT_SECRET set")
            if secret
            else bad("  GOOGLE_CLIENT_SECRET MISSING — add it to .env")
        )

        # allauth resolves the app from settings OR from a SocialApp row. Having
        # both is the classic "MultipleObjectsReturned" at login time, so warn.
        try:
            from allauth.socialaccount.models import SocialApp

            db_apps = SocialApp.objects.filter(provider="google").count()
            if db_apps and client_id:
                out.write(
                    warn(
                        f"\n  {db_apps} Google SocialApp row(s) exist in the database AND "
                        "credentials are set in .env.\n  allauth may raise "
                        "MultipleObjectsReturned at login. Delete the DB rows in "
                        "/admin/socialaccount/socialapp/ — .env is the source of truth here."
                    )
                )
        except Exception:  # pragma: no cover - table may not exist yet
            pass

        site = Site.objects.filter(pk=settings.SITE_ID).first()
        if site:
            out.write(f"\n  SITE_ID {settings.SITE_ID} -> {site.domain}")

        if client_id and secret:
            out.write(ok(f"\nReady. Restart the server, then open {host}/ and click Continue with Google.\n"))
        else:
            out.write(warn("\nNot ready yet — fill in .env, then run this again.\n"))
