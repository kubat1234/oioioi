from django.urls import path

from oioioi.czeciecamp import views

app_name = "czeciecamp"

contest_patterns = [
    path("camp/", views.panel_view, name="czeciecamp_panel"),
    path("camp/round/<int:round_id>/visibility/<str:action>/", views.round_action_view, name="czeciecamp_round_action"),
    path("camp/freeze/", views.freeze_view, {"frozen": True}, name="czeciecamp_freeze"),
    path("camp/unfreeze/", views.freeze_view, {"frozen": False}, name="czeciecamp_unfreeze"),
    path("camp/add_round/", views.add_round_view, name="czeciecamp_add_round"),
    path("camp/rounds/save/", views.save_rounds_view, name="czeciecamp_save_rounds"),
    path("camp/round/<int:round_id>/delete/", views.delete_round_view, name="czeciecamp_delete_round"),
]

noncontest_patterns = [
    path("czeciecamp/create/", views.create_camp_view, name="czeciecamp_create"),
]
