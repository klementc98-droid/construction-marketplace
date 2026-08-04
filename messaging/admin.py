from django.contrib import admin

from .models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    readonly_fields = ("sender", "body", "created_at", "read_at")
    can_delete = False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("job", "worker", "last_message_at", "created_at")
    search_fields = ("job__title", "worker__user__email")
    readonly_fields = ("created_at", "updated_at", "last_message_at")
    inlines = [MessageInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("sender", "conversation", "created_at", "read_at")
    list_filter = ("read_at",)
    search_fields = ("body", "sender__email")
    readonly_fields = ("created_at", "updated_at")
