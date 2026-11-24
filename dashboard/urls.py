"""Dashboard pages and standard Django session authentication."""

from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import evaluation_views, views
from .forms import AdministratorLoginForm

urlpatterns = [
    path("login/", LoginView.as_view(authentication_form=AdministratorLoginForm), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", views.run_agent, name="run_agent"),
    path("results/", views.results, name="results"),
    path("runs/<uuid:run_id>/", views.run_detail, name="run_detail"),
    path("runs/<uuid:run_id>/progress/", views.progress, name="run_progress"),
    path("history/", views.history, name="history"),
    path("evaluations/", evaluation_views.history, name="evaluations"),
    path("evaluations/compare/", evaluation_views.comparison, name="evaluation_compare"),
    path("evaluations/<uuid:run_id>/", evaluation_views.detail, name="evaluation_detail"),
    path("evaluations/<uuid:run_id>/cases/<uuid:fixture_id>/", evaluation_views.case_detail,
         name="evaluation_case"),
]
