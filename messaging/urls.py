from django.urls import path

from . import views

app_name = "messaging"

urlpatterns = [
    path("messages/", views.inbox, name="inbox"),
    path("messages/<int:pk>/", views.thread, name="thread"),
    path(
        "messages/start/<int:job_pk>/<int:worker_pk>/", views.start, name="start"
    ),
    path(
        "messages/contact/<int:worker_pk>/", views.start_direct, name="start_direct"
    ),
]
