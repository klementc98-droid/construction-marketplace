from django.contrib import admin

from .models import EscrowPayment, StripeAccount, WebhookEvent


@admin.register(StripeAccount)
class StripeAccountAdmin(admin.ModelAdmin):
    list_display = (
        "worker",
        "account_id",
        "details_submitted",
        "charges_enabled",
        "payouts_enabled",
    )
    list_filter = ("payouts_enabled", "charges_enabled")
    search_fields = ("worker__user__email", "account_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(EscrowPayment)
class EscrowPaymentAdmin(admin.ModelAdmin):
    list_display = ("job", "worker", "amount", "platform_fee", "status", "authorized_at")
    list_filter = ("status",)
    search_fields = ("job__title", "payment_intent_id", "checkout_session_id")
    # Money records are evidence. Hand-editing them would make our record
    # disagree with Stripe, which is the one thing this app must never do.
    readonly_fields = (
        "job",
        "worker",
        "amount",
        "platform_fee",
        "worker_payout",
        "captured_amount",
        "status",
        "checkout_session_id",
        "payment_intent_id",
        "authorized_at",
        "released_at",
        "refunded_at",
        "last_error",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_type", "received_at", "payload_summary")
    list_filter = ("event_type",)
    search_fields = ("event_id",)
    readonly_fields = ("event_id", "event_type", "received_at", "payload_summary")

    def has_add_permission(self, request):
        return False
