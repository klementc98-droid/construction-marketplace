"""Back-office views.

Phase 7's dispute queue will live in this admin. Registering the profile
models now with the trust fields visible means that when a dispute does land,
the reviewer can already see who they are dealing with — and the
``flagged_for_review`` filter is here from day one so the fraud work that v1
defers has somewhere to land.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    AvailabilityDate,
    ClientProfile,
    PortfolioPhoto,
    TradeLicense,
    User,
    WorkerProfile,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("-date_joined",)
    list_display = ("email", "full_name", "roles_display", "is_staff", "date_joined")
    search_fields = ("email", "full_name")
    list_filter = ("is_staff", "is_superuser", "is_active")

    # Rebuilt from scratch because the stock UserAdmin fieldsets reference
    # `username`, which this model does not have.
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "phone", "google_picture_url", "last_active_role")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),
    )

    @admin.display(description="Roles")
    def roles_display(self, obj: User) -> str:
        return ", ".join(obj.roles) or "—"


class TradeLicenseInline(admin.TabularInline):
    model = TradeLicense
    extra = 0
    readonly_fields = ("verified_at", "verified_by")


class AvailabilityDateInline(admin.TabularInline):
    model = AvailabilityDate
    extra = 0


class PortfolioPhotoInline(admin.TabularInline):
    model = PortfolioPhoto
    extra = 0


@admin.register(WorkerProfile)
class WorkerProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "region",
        "trades_display",
        "average_rating",
        "jobs_completed",
        "flagged_for_review",
    )
    list_filter = ("region", "trades", "availability_status", "flagged_for_review")
    search_fields = ("user__email", "user__full_name", "service_area")
    filter_horizontal = ("trades",)
    inlines = [TradeLicenseInline, AvailabilityDateInline, PortfolioPhotoInline]
    readonly_fields = ("flagged_at",)

    @admin.display(description="Trades")
    def trades_display(self, obj: WorkerProfile) -> str:
        return ", ".join(t.name for t in obj.trades.all()) or "—"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user", "region").prefetch_related("trades")


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "company_name",
        "region",
        "average_rating",
        "jobs_completed",
        "flagged_for_review",
    )
    list_filter = ("region", "flagged_for_review")
    search_fields = ("user__email", "user__full_name", "company_name")
    readonly_fields = ("flagged_at",)


@admin.register(TradeLicense)
class TradeLicenseAdmin(admin.ModelAdmin):
    """Standalone view so licences can be worked through as a queue.

    This is the shape the deferred manual verification will take: filter to
    unverified, open one, check the state registry, stamp it.
    """

    list_display = ("worker", "trade", "number", "is_verified", "created_at")
    list_filter = ("trade", "verified_at")
    search_fields = ("number", "worker__user__email")
