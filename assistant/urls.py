from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("assistant/config/", views.config, name="config"),
    path("assistant/start/", views.start, name="start"),
    path("assistant/say/", views.say, name="say"),
    path("assistant/close/", views.close, name="close"),
]
