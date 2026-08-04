from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("welcome/", views.select_role, name="select_role"),
    path("profile/details/", views.details, name="details"),
    path("switch-role/", views.switch_role, name="switch_role"),
    # Worker
    path("profile/worker/edit/", views.worker_edit, name="worker_edit"),
    path("worker/<int:pk>/", views.worker_detail, name="worker_detail"),
    path("profile/worker/photos/add/", views.portfolio_add, name="portfolio_add"),
    path(
        "profile/worker/photos/<int:pk>/delete/",
        views.portfolio_delete,
        name="portfolio_delete",
    ),
    # Client
    path("profile/client/edit/", views.client_edit, name="client_edit"),
    path("client/<int:pk>/", views.client_detail, name="client_detail"),
]
