"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # set_language, which the header switcher posts to. Not i18n_patterns: the
    # choice lives in the session, so every URL on the site stays the one URL
    # regardless of language and a link sent between two people still works.
    path("i18n/", include("django.conf.urls.i18n")),
    # allauth owns /accounts/ (Google redirect, callback, logout).
    path("accounts/", include("allauth.urls")),
    path("", include("core.urls")),
    path("", include("assistant.urls")),
    path("", include("jobs.urls")),
    path("", include("messaging.urls")),
    path("", include("payments.urls")),
    path("", include("worklog.urls")),
    path("", include("accounts.urls")),
]

# Development only. Deployed environments serve uploads from object storage —
# see the MEDIA_ROOT note in settings.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
