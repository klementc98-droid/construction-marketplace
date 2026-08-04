from django.contrib import admin

from .models import Application, Job, Offer


class ApplicationInline(admin.TabularInline):
    model = Application
    extra = 0
    readonly_fields = ("created_at", "responded_at")


class OfferInline(admin.TabularInline):
    model = Offer
    extra = 0
    readonly_fields = ("created_at", "responded_at")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "title", "job_type", "trade", "state", "client", "gig_date",
        "is_private", "created_at",
    )
    list_filter = ("job_type", "state", "is_private", "trade", "region")
    search_fields = ("title", "description", "location")
    autocomplete_fields = ()
    readonly_fields = ("created_at", "updated_at", "filled_at")
    inlines = [ApplicationInline, OfferInline]


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("worker", "job", "status", "created_at", "responded_at")
    list_filter = ("status",)
    search_fields = ("worker__user__email", "job__title")
    readonly_fields = ("created_at", "updated_at", "responded_at")


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ("worker", "job", "status", "created_at", "responded_at")
    list_filter = ("status",)
    search_fields = ("worker__user__email", "job__title")
    readonly_fields = ("created_at", "updated_at", "responded_at")
