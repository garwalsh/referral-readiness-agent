"""Administrator views for queueing, following, and inspecting reviews."""

from collections import Counter
import json
from uuid import UUID

from django.contrib.auth.decorators import user_passes_test
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from .forms import ReviewForm, synthetic_patient_ids
from .models import ReviewRun
from .worker import read_events

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
    "tool_start": "Retrieving clinical evidence",
    "tool": "Tool completed",
    "validation_start": "Validating report",
    "validation_finish": "Report validation finished",
    "validation_error": "Validation issue",
    "run_error": "Agent error",
    "finish": "Readiness calculated",
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
        status = "Error" if error else "OK"
        if kind in {"tool_start", "validation_start"}:
            status = "Started"
        if kind == "validation_finish" and event.get("failures"):
            status = "Issues found"
        display = {
            "timestamp": event.get("timestamp", ""),
            "label": "Tool failed" if kind == "tool" and error else EVENT_LABELS[kind],
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
    return render(request, "dashboard/detail.html", {
        "active_page": "results", "run": run, "findings": findings,
        "readiness_label": READINESS_LABELS.get(run.readiness),
        "requirement_counts": [
            {"status": status, "label": label, "count": counts[status]}
            for status, (label, _) in REQUIREMENT_STATUSES.items()
        ],
        "events": timeline(run, include_citation_diagnostics=True),
    })


@administrator_required
@never_cache
@require_safe
def progress(request: HttpRequest, run_id: UUID) -> JsonResponse:
    run = get_object_or_404(ReviewRun, pk=run_id)
    return JsonResponse({
        "state": run.get_state_display(), "terminal": run.terminal,
        "events": timeline(run),
    })


@administrator_required
@never_cache
@require_safe
def history(request: HttpRequest) -> HttpResponse:
    runs = ReviewRun.objects.select_related("created_by").defer("report")
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    return render(request, "dashboard/history.html", {"active_page": "history", "page_obj": page})
