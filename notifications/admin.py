from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("kind", "recipient", "job", "sent_at", "attempts")
    list_filter = ("kind", "sent_at")
    search_fields = ("recipient__email", "last_error")
    readonly_fields = ("created_at", "updated_at")
