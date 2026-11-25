"""Administrator views for queueing, following, and inspecting reviews."""

from collections import Counter
import json
from uuid import UUID

from django.contrib.auth.decorators import user_passes_test
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from .forms import ReviewForm, synthetic_patient_ids
from .models import ReviewRun
from .worker import WORKER_TIMEOUT_ERROR, read_events

administrator_required = user_passes_test(
    lambda user: user.is_active and user.is_staff,
    login_url="login",
)
REQUIREMENT_LABELS = {
    "referral_reason": "Referral reason",
    "symptom_history": "Symptom history",
    "physical_assessment": "Physical assessment",
    "ecg": "ECG documentation",
    "relevant_history": "Relevant history",
}
REQUIREMENT_STATUSES = {
    "PRESENT": ("Satisfied", "✓"),
    "MISSING": ("Missing", "−"),
    "OUTDATED": ("Outdated", "◷"),
    "UNKNOWN": ("Unresolved", "?"),
}
READINESS_LABELS = {
    "READY": "Documentation requirements satisfied",
    "NEEDS_REVIEW": "Documentation needs review",
    "UNABLE_TO_ASSESS": "Some requirements could not be assessed",
}
EVENT_LABELS = {
    "start": "Agent started",
    "connection_start": "Connecting to clinical records",
    "connection_finish": "Clinical records connected",
    "model_start": "Claude request started",
    "model_finish": "Claude response received",
    "tool_start": "Retrieving clinical evidence",
    "tool": "Tool completed",
    "validation_start": "Validating report",
    "validation_finish": "Report validation finished",
    "validation_error": "Validation issue",
    "run_error": "Agent error",
    "finish": "Readiness calculated",
}
STARTED_EVENTS = {"connection_start", "model_start", "tool_start", "validation_start"}
FAILED_EVENTS = {
    "tool": "Tool failed", "connection_finish": "Clinical records connection failed",
    "model_finish": "Claude request failed",
}
TIMED_OUT_EVENTS = {
    "tool": "Tool timed out", "connection_finish": "Clinical records connection timed out",
    "model_finish": "Claude request timed out",
}


def timeline(run: ReviewRun, *, include_citation_diagnostics: bool = False) -> list[dict]:
    try:
        raw = read_events(run)
    except (OSError, ValueError):
        return []
    events = []
    for event in raw:
        kind = event.get("event")
        if kind not in EVENT_LABELS:
            continue
        parameters = event.get("parameters") or {}
        error = event.get("error") or event.get("message")
        label = EVENT_LABELS[kind]
        status = "OK"
        if event.get("timed_out") or event.get("outcome") == "timed_out":
            label = TIMED_OUT_EVENTS.get(kind, label)
            status = "Timed out"
        elif error or event.get("outcome") == "failed":
            label = FAILED_EVENTS.get(kind, label)
            status = "Error"
        elif kind in STARTED_EVENTS:
            status = "Started"
        elif kind == "validation_finish" and event.get("failures"):
            status = "Issues found"
        display = {
            "kind": kind,
            "timestamp": event.get("timestamp", ""),
            "label": label,
            "tool": event.get("tool"),
            "resource_type": parameters.get("resourceType"),
            "parameters": {key: parameters[key] for key in ("resourceType", "queryParams", "id") if key in parameters},
            "resource_count": (event.get("result") or {}).get("count"),
            "duration_ms": event.get("duration_ms"),
            "status": status,
            "error": error,
            "readiness": event.get("readiness"),
        }
        citation = event.get("citation") if include_citation_diagnostics and kind == "validation_error" else None
        if isinstance(citation, dict):
            # JSON string notation makes line endings, tabs and leading spaces visible in HTML.
            requirement = event.get("requirement")
            display["citation"] = {
                "requirement": REQUIREMENT_LABELS.get(requirement, requirement),
                "component": citation.get("component"),
                "resource_type": citation.get("resource_type"),
                "resource_id": citation.get("resource_id"),
                "submitted_quote": json.dumps(citation.get("submitted_quote"), ensure_ascii=False),
                "source_texts": [json.dumps(note, ensure_ascii=False) for note in citation.get("source_texts", [])],
            }
        events.append(display)
    return events


RUNNING_ACTIVITIES = {
    "connection_start": ("waiting", "Connecting to clinical records", "Waiting for LangCare to respond."),
    "model_start": ("waiting", "Reviewing documentation", "Waiting for Claude to respond."),
    "tool_start": ("waiting", "Retrieving patient documentation", "Waiting for the clinical records tool to respond."),
    "validation_start": ("running", "Validating findings", ""),
    "validation_error": ("running", "Validating findings", ""),
    "validation_finish": ("running", "Preparing readiness report", ""),
    "finish": ("running", "Preparing readiness report", ""),
    "run_error": ("running", "Recording execution failure", ""),
}


def execution_progress(run: ReviewRun, events: list[dict]) -> dict:
    elapsed = None
    if run.started_at:
        end = run.finished_at or timezone.now()
        elapsed = max(0, int((end - run.started_at).total_seconds() * 1000))

    state = run.get_state_display()
    if run.state == ReviewRun.State.QUEUED:
        activity = ("queued", "Waiting for the worker", "This review is queued.")
    elif run.state == ReviewRun.State.COMPLETED:
        activity = ("completed", "Execution completed", "")
    elif run.state == ReviewRun.State.FAILED:
        timeout = run.error == WORKER_TIMEOUT_ERROR or any(
            event["kind"] == "run_error" and event["status"] == "Timed out" for event in events
        )
        if timeout:
            state = "Timed out"
            activity = ("timed-out", "Execution timed out", "")
        else:
            activity = ("failed", "Execution failed", "")
    else:
        last_event = events[-1]["kind"] if events else None
        activity = RUNNING_ACTIVITIES.get(last_event, ("running", "Agent running", "Awaiting the next update."))
    mode, label, detail = activity
    return {
        "state": state, "terminal": run.terminal, "elapsed_ms": elapsed,
        "activity": {"mode": mode, "label": label, "detail": detail},
    }


@administrator_required
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
def run_agent(request: HttpRequest) -> HttpResponse:
    try:
        patients = synthetic_patient_ids()
        fixture_error = False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        patients, fixture_error = [], True
    form = ReviewForm(patients, request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        run = ReviewRun.objects.create(created_by=request.user, **form.cleaned_data)
        return redirect("run_detail", run_id=run.pk)
    return render(request, "dashboard/run.html", {
        "active_page": "run_agent",
        "form": form,
        "fixture_error": fixture_error,
        "can_run": bool(patients and form.fields["model"].choices),
    }, status=400 if request.method == "POST" else 200)


@administrator_required
@never_cache
@require_safe
def results(request: HttpRequest) -> HttpResponse:
    latest = ReviewRun.objects.first()
    if latest:
        return redirect("run_detail", run_id=latest.pk)
    return render(request, "dashboard/results.html", {"active_page": "results"})


@administrator_required
@never_cache
@require_safe
def run_detail(request: HttpRequest, run_id: UUID) -> HttpResponse:
    run = get_object_or_404(ReviewRun.objects.select_related("created_by"), pk=run_id)
    findings = [
        {**item, "label": REQUIREMENT_LABELS[item["requirement_id"]],
         "status_icon": REQUIREMENT_STATUSES.get(item["status"], ("", ""))[1]}
        for item in (run.report or {}).get("findings", [])
    ]
    counts = Counter(item["status"] for item in findings)
    events = timeline(run, include_citation_diagnostics=True)
    execution = execution_progress(run, events)
    return render(request, "dashboard/detail.html", {
        "active_page": "results", "run": run, "findings": findings,
        "readiness_label": READINESS_LABELS.get(run.readiness),
        "requirement_counts": [
            {"status": status, "label": label, "count": counts[status]}
            for status, (label, _) in REQUIREMENT_STATUSES.items()
        ],
        "events": events, "execution": execution,
    })


@administrator_required
@never_cache
@require_safe
def progress(request: HttpRequest, run_id: UUID) -> JsonResponse:
    run = get_object_or_404(ReviewRun, pk=run_id)
    events = timeline(run)
    return JsonResponse({**execution_progress(run, events), "events": events})


@administrator_required
@never_cache
@require_safe
def history(request: HttpRequest) -> HttpResponse:
    runs = ReviewRun.objects.select_related("created_by").defer("report")
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    return render(request, "dashboard/history.html", {"active_page": "history", "page_obj": page})
