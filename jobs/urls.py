from django.urls import path

from . import views

app_name = "jobs"

urlpatterns = [
    # Browse / search
    path("jobs/", views.job_list, name="list"),
    path("workers/", views.worker_list, name="worker_list"),
    path("jobs/mine/", views.mine, name="mine"),
    # Posting. "post/" before "<int:pk>/" so the literal wins the match.
    path("jobs/post/", views.job_post_choose, name="post_choose"),
    path("jobs/post/<str:job_type>/", views.job_post, name="post"),
    path("jobs/<int:pk>/", views.job_detail, name="detail"),
    path("jobs/<int:pk>/edit/", views.job_edit, name="edit"),
    path("jobs/<int:pk>/cancel/", views.job_cancel, name="cancel"),
    # Applying
    path("jobs/<int:pk>/apply/", views.job_apply, name="apply"),
    path("jobs/<int:pk>/applicants/", views.job_applicants, name="applicants"),
    path(
        "applications/<int:pk>/withdraw/",
        views.application_withdraw,
        name="application_withdraw",
    ),
    path(
        "applications/<int:pk>/select/",
        views.application_select,
        name="application_select",
    ),
    # Direct offers — the mirror of applying, from the client's side.
    path("workers/<int:worker_pk>/offer/", views.offer_create, name="offer"),
    path("offers/<int:pk>/respond/", views.offer_respond, name="offer_respond"),
    path("offers/<int:pk>/withdraw/", views.offer_withdraw, name="offer_withdraw"),
    # Negotiation, on any open gig — not just direct offers. A worker proposes
    # for themselves; the client answers one named person.
    path("jobs/<int:pk>/counter/", views.counter_create, name="counter"),
    path(
        "jobs/<int:pk>/counter/<int:worker_pk>/",
        views.counter_create,
        name="counter_to",
    ),
    path("counters/<int:pk>/respond/", views.counter_respond, name="counter_respond"),
    path("jobs/<int:pk>/rate/", views.review_create, name="review"),
    path("jobs/<int:pk>/publish/", views.offer_publish, name="offer_publish"),
]
