from django.contrib import admin

from .models import CheckIn, Completion, Dispute


@admin.register(CheckIn)
class CheckInAdmin(admin.ModelAdmin):
    list_display = ("job", "worker", "arrived_at", "distance_m", "looks_on_site")
    list_filter = ("looks_on_site",)
    search_fields = ("job__title", "worker__user__email")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Completion)
class CompletionAdmin(admin.ModelAdmin):
    list_display = (
        "job", "hours_worked", "ended_early", "payable_amount",
        "settles_at", "settled_at",
    )
    list_filter = ("ended_early", "ended_early_by")
    search_fields = ("job__title",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    """The bones of the phase 7 review queue.

    Open disputes first, because that is the only ordering an admin working
    through them wants.
    """

    list_display = ("job", "status", "raised_by", "created_at", "resolved_at")
    list_filter = ("status",)
    search_fields = ("job__title", "reason")
    readonly_fields = ("job", "raised_by", "reason", "created_at", "updated_at")
    ordering = ("status", "created_at")
