"""Dashboard pages and standard Django session authentication."""

from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views
from .forms import AdministratorLoginForm

urlpatterns = [
    path("login/", LoginView.as_view(authentication_form=AdministratorLoginForm), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", views.run_agent, name="run_agent"),
    path("results/", views.results, name="results"),
    path("runs/<uuid:run_id>/", views.run_detail, name="run_detail"),
    path("runs/<uuid:run_id>/progress/", views.progress, name="run_progress"),
    path("history/", views.history, name="history"),
]
