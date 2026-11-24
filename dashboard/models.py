"""Durable application audit records for synthetic reviews."""

from uuid import uuid4

from django.conf import settings
from django.db import models


class ReviewRun(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    patient_id = models.UUIDField()
    review_date = models.DateField()
    model = models.CharField(max_length=100)
    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    tool_calls = models.PositiveSmallIntegerField(default=0)
    readiness = models.CharField(max_length=20, blank=True)
    error = models.CharField(max_length=240, blank=True)
    report = models.JSONField(null=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["state", "created_at"])]

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 2)
        return None

    @property
    def terminal(self) -> bool:
        return self.state in {self.State.COMPLETED, self.State.FAILED}
