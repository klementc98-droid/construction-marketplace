from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    # Worker
    path("payouts/", views.payouts, name="payouts"),
    path("payouts/start/", views.payouts_start, name="payouts_start"),
    path("payouts/return/", views.payouts_return, name="payouts_return"),
    # Client
    path("jobs/<int:pk>/payment/", views.escrow_detail, name="escrow"),
    path("jobs/<int:pk>/payment/fund/", views.fund, name="fund"),
    path("jobs/<int:pk>/payment/return/", views.fund_return, name="fund_return"),
    path(
        "jobs/<int:pk>/payment/cancel/",
        views.cancel_and_refund,
        name="cancel_and_refund",
    ),
    # Stripe
    path("stripe/webhook/", views.webhook, name="webhook"),
]
