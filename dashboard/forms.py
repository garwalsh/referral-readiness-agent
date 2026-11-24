"""Authentication and run configuration forms."""

import json
from datetime import date
from uuid import UUID

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User

REVIEW_DATE = date(2026, 11, 25)
MODEL_LABELS = {
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
}


class AdministratorLoginForm(AuthenticationForm):
    def confirm_login_allowed(self, user: User) -> None:
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise ValidationError("Sign in with the dashboard administrator account.", code="administrator_required")


def synthetic_patient_ids() -> list[str]:
    """Use the deployment's ID map without loading the evaluation answer key."""
    payload = json.loads(settings.RRR_FIXTURE_MAP.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Expected a nonempty fixture ID map.")
    return list(dict.fromkeys(str(UUID(record["patient"])) for record in payload.values()))


class ReviewForm(forms.Form):
    patient_id = forms.ChoiceField(label="Synthetic patient")
    model = forms.ChoiceField(label="Claude model")
    review_date = forms.DateField(
        label="Review date",
        initial=REVIEW_DATE,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )

    def __init__(self, patients: list[str], data=None) -> None:
        super().__init__(data)
        self.fields["patient_id"].choices = [
            ("", "Select a FHIR patient ID"),
            *((patient_id, patient_id) for patient_id in patients),
        ]
        models = [(model, MODEL_LABELS.get(model, model)) for model in settings.RRR_CLAUDE_MODELS]
        self.fields["model"].choices = models
        if models:
            self.initial["model"] = models[0][0]
