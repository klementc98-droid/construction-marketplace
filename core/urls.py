from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("about/", views.about, name="about"),
    path("whitepaper/", views.whitepaper, name="whitepaper"),
]
