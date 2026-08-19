from django.urls import path

from . import views

app_name = "worklog"

urlpatterns = [
    path("jobs/<int:pk>/work/", views.workspace, name="workspace"),
    path("jobs/<int:pk>/work/check-in/", views.check_in, name="check_in"),
    path("jobs/<int:pk>/work/complete/", views.complete, name="complete"),
    path("jobs/<int:pk>/work/finish/", views.finish, name="finish"),
    path("jobs/<int:pk>/work/confirm/", views.confirm, name="confirm"),
    path("jobs/<int:pk>/work/ended-early/", views.end_early, name="end_early"),
    path("jobs/<int:pk>/work/approve/", views.approve, name="approve"),
    path("jobs/<int:pk>/work/dispute/", views.dispute, name="dispute"),
]
