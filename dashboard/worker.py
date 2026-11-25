"""Claim a queued run and execute the unchanged CLI workflow outside HTTP requests."""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from scripts.reporting import Report

from .forms import synthetic_patient_ids
from .models import ReviewRun

WORKER_TIMEOUT_ERROR = "The agent exceeded the worker time limit."


def trace_path(run: ReviewRun) -> Path:
    return settings.RRR_RUN_DIR / f"{run.pk}.jsonl"


def read_events(run: ReviewRun) -> list[dict]:
    try:
        text = trace_path(run).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    # Concurrent readers see only whole JSONL events. The runner flushes each line.
    events = [json.loads(line) for line in text.split("\n")[:-1]]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError("Invalid trace event.")
    return events


def count_tool_calls(events: list[dict]) -> int:
    return sum(event.get("event") == "tool" and event.get("parameters") is not None for event in events)


def claim_next() -> ReviewRun | None:
    now = timezone.now()
    deadline = now - timedelta(seconds=settings.RRR_RUN_TIMEOUT + 30)
    # A killed worker leaves a visible failure after its lease. It is never re-run implicitly.
    stale = ReviewRun.objects.filter(state=ReviewRun.State.RUNNING, started_at__lt=deadline)
    for run in stale:
        ReviewRun.objects.filter(pk=run.pk, state=ReviewRun.State.RUNNING).update(
            state=ReviewRun.State.FAILED, finished_at=now, tool_calls=recorded_tool_calls(run),
            error="The worker stopped before recording a result.",
        )
    candidate = ReviewRun.objects.filter(state=ReviewRun.State.QUEUED).order_by("created_at", "id").first()
    if candidate is None:
        return None
    # SQLite has no row-level FOR UPDATE. One conditional update wins the claim.
    claimed = ReviewRun.objects.filter(pk=candidate.pk, state=ReviewRun.State.QUEUED).update(
        state=ReviewRun.State.RUNNING, started_at=now
    )
    return ReviewRun.objects.get(pk=candidate.pk) if claimed else None


def recorded_tool_calls(run: ReviewRun) -> int:
    try:
        return count_tool_calls(read_events(run))
    except (OSError, ValueError):
        return 0


def execute_run(run: ReviewRun) -> None:
    state, error, readiness, report = ReviewRun.State.FAILED, "", "", None
    try:
        if str(run.patient_id) not in synthetic_patient_ids() or run.model not in settings.RRR_CLAUDE_MODELS:
            raise ValueError("Run configuration is no longer permitted.")
        settings.RRR_RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        command = [
            sys.executable, str(settings.BASE_DIR / "scripts" / "run_agent.py"), str(run.patient_id),
            "--review-date", run.review_date.isoformat(), "--model", run.model,
            "--run-id", str(run.pk), "--trace-path", str(trace_path(run)),
        ]
        # Supply only the credentials for the selected route.
        transport = os.environ.get("RRR_TRANSPORT", "langcare")
        common = ("ANTHROPIC_API_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR")
        credentials = {
            "langcare": ("LANGCARE_MCP_URL", "LANGCARE_MCP_TOKEN"),
            "stackone": ("STACKONE_ACCOUNT_ID", "STACKONE_OAUTH_FILE"),
        }.get(transport, ())
        env = {name: os.environ[name] for name in (*common, *credentials) if name in os.environ}
        env["RRR_TRANSPORT"] = transport
        result = subprocess.run(
            command, cwd=settings.BASE_DIR, env=env, capture_output=True, text=True,
            timeout=settings.RRR_RUN_TIMEOUT, check=False,
        )
        if result.returncode:
            error = "The agent process exited before producing a report. Check its runtime configuration."
        else:
            validated = Report.model_validate_json(result.stdout)
            events = read_events(run)
            if (
                not events or events[0].get("event") != "start" or events[0].get("run_id") != str(run.pk)
                or events[0].get("model") != run.model
                or events[0].get("transport", "langcare") != transport
                or events[0].get("patient_id") != str(run.patient_id)
                or events[0].get("review_date") != run.review_date.isoformat()
                or events[-1].get("event") != "finish" or events[-1].get("run_id") != str(run.pk)
                or events[-1].get("readiness") != validated.readiness.value
                or validated.patient_id != str(run.patient_id) or validated.review_date != run.review_date
            ):
                raise ValueError("Run metadata and report did not match.")
            readiness = validated.readiness.value
            report = validated.model_dump(mode="json")
            if any(event.get("event") == "run_error" for event in events):
                error = "The agent reported an execution error. See the timeline."
            else:
                state = ReviewRun.State.COMPLETED
    except subprocess.TimeoutExpired:
        error = WORKER_TIMEOUT_ERROR
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Suppress raw subprocess stderr, payloads, paths, and credentials.
        error = "The run configuration, report, or trace could not be verified."
    ReviewRun.objects.filter(pk=run.pk, state=ReviewRun.State.RUNNING).update(
        state=state, finished_at=timezone.now(), error=error,
        readiness=readiness, report=report, tool_calls=recorded_tool_calls(run),
    )
