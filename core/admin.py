from django.contrib import admin

from .models import Region, Trade


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active")
    list_filter = ("is_active",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    """Toggling ``requires_license`` here is how a newly regulated trade starts
    prompting for a licence number — no code change, no deploy."""

    list_display = ("name", "slug", "requires_license", "display_order")
    list_filter = ("requires_license",)
    list_editable = ("requires_license", "display_order")
    prepopulated_fields = {"slug": ("name",)}
