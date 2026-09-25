from django.urls import path

from . import views


urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("interviews/new/", views.interview_create, name="interview_create"),
    path(
        "interviews/<uuid:interview_id>/",
        views.interview_detail,
        name="interview_detail",
    ),
    path(
        "interviews/<uuid:interview_id>/room/",
        views.host_room,
        name="host_room",
    ),
    path(
        "interviews/<uuid:interview_id>/rotate-invite/",
        views.rotate_invite,
        name="rotate_invite",
    ),
    path(
        "interviews/<uuid:interview_id>/questions/",
        views.generate_questions,
        name="generate_questions",
    ),
    path(
        "interviews/<uuid:interview_id>/questions/<int:question_id>/state/",
        views.question_state,
        name="question_state",
    ),
    path(
        "interviews/<uuid:interview_id>/complete/",
        views.complete_interview,
        name="complete_interview",
    ),
    path("join/<uuid:token>/", views.candidate_join, name="candidate_join"),
    path(
        "room/<uuid:interview_id>/candidate/",
        views.candidate_room,
        name="candidate_room",
    ),
    path("health/", views.service_status, name="service_status"),
]
