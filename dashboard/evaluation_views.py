"""Administrator-only views of recorded evaluations."""
from uuid import UUID

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .evaluations import (
    METRIC_LABELS, Evaluation, EvaluationCase, SavedEvaluation, compare, discover, load_report, load_trace,
)
from .views import EVENT_LABELS, REQUIREMENT_LABELS, administrator_required


def show(request: HttpRequest, page: str, **context) -> HttpResponse:
    return render(request, f"dashboard/evaluations/{page}.html", {"active_page": "evaluations", **context})


def find_run(run_id: UUID) -> SavedEvaluation:
    runs, _ = discover()
    if run_id not in runs:
        raise Http404("Evaluation is unavailable.")
    return runs[run_id]


def checklist(case: EvaluationCase, evidence: dict | None = None) -> list[dict]:
    rows = []
    evidence = evidence or {}
    for key, label in REQUIREMENT_LABELS.items():
        expected = case.expected_checklist[key]
        actual = case.actual_checklist.get(key)
        rows.append({
            "label": label, "expected": expected.value, "actual": actual.value if actual else None,
            "mismatch": actual != expected, "evidence": evidence.get(key, []),
        })
    return rows


def rates(run: Evaluation) -> list[tuple]:
    return [(label, getattr(run.metrics, key)) for key, label in METRIC_LABELS.items()]


def projected_events(events: list[dict]) -> list[dict]:
    """Display recorded tool parameters and reference summaries using known fields."""
    timeline = []
    for event in events:
        kind = event.get("event")
        if not isinstance(kind, str) or kind not in EVENT_LABELS:
            continue
        parameters = event.get("parameters") or {}
        result = event.get("result") or {}
        timeline.append({
            "label": EVENT_LABELS[kind], "timestamp": event.get("timestamp"),
            "tool": event.get("tool"), "duration_ms": event.get("duration_ms"),
            "parameters": [(key, parameters[key]) for key in ("resourceType", "queryParams", "id")
                           if isinstance(parameters.get(key), str)],
            "count": result.get("count"), "more_pages": result.get("more_pages"),
            "references": [(ref["resource_type"], ref["resource_id"]) for ref in result.get("references", [])
                           if isinstance(ref.get("resource_type"), str) and isinstance(ref.get("resource_id"), str)],
            "error": event.get("error") or event.get("message"),
            "readiness": event.get("readiness"), "failures": event.get("failures"),
        })
    return timeline


@administrator_required
@never_cache
@require_safe
def history(request: HttpRequest) -> HttpResponse:
    runs, skipped = discover()
    evaluations = sorted((saved.result for saved in runs.values()), key=lambda run: run.started_at, reverse=True)
    return show(request, "history", evaluations=evaluations, skipped=skipped)


@administrator_required
@never_cache
@require_safe
def detail(request: HttpRequest, run_id: UUID) -> HttpResponse:
    run = find_run(run_id).result
    cases = [{"case": case, "mismatches": [row for row in checklist(case) if row["mismatch"]]}
             for case in run.cases]
    requirement_rates = [(label, run.metrics.per_requirement.get(key)) for key, label in REQUIREMENT_LABELS.items()]
    return show(request, "detail", run=run, cases=cases, rates=rates(run), requirement_rates=requirement_rates)


@administrator_required
@never_cache
@require_safe
def case_detail(request: HttpRequest, run_id: UUID, fixture_id: UUID) -> HttpResponse:
    saved = find_run(run_id)
    case = next((item for item in saved.result.cases if item.fixture_id == fixture_id), None)
    if case is None:
        raise Http404("Evaluation case is unavailable.")
    try:
        report = load_report(saved, case)
    except (OSError, ValueError):
        report = None
    try:
        events = projected_events(load_trace(saved, case))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        events = []
    evidence = {item.requirement_id.value: item.evidence for item in report.findings} if report else {}
    return show(request, "case", run=saved.result, case=case, findings=checklist(case, evidence),
                report_available=report is not None, events=events)


@administrator_required
@never_cache
@require_safe
def comparison(request: HttpRequest) -> HttpResponse:
    runs, _ = discover()
    before_id, after_id = request.GET.get("before", ""), request.GET.get("after", "")
    try:
        if UUID(before_id) == UUID(after_id):
            raise ValueError("Choose two distinct evaluations.")
        before = runs[UUID(before_id)].result
        after = runs[UUID(after_id)].result
    except (KeyError, ValueError):
        return show(request, "comparison", error="Choose two distinct, completed evaluations.")
    try:
        rows, diff = compare(before, after)
    except ValueError as error:
        return show(request, "comparison", error=str(error))
    keys = tuple(METRIC_LABELS)[:4]
    metrics = [(METRIC_LABELS[key], getattr(before.metrics, key), getattr(after.metrics, key)) for key in keys]
    # Keep diff line endings uniform for rendering inside a preformatted block.
    return show(request, "comparison", before=before, after=after, rows=rows, metrics=metrics,
                diff="\n".join(line.rstrip("\n") for line in diff))
