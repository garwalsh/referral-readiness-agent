"""Administrator-only views of recorded evaluations."""
from uuid import UUID

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .evaluations import (
    METRIC_LABELS, Evaluation, EvaluationCase, SavedEvaluation, compare, discover, load_report, load_trace,
)
from .prompt_display import display_diff, formatted_prompt, literal_prompt
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


METRIC_EXPLANATIONS = {
    "strict_case_accuracy": (
        "Cases where all five checklist statuses and final readiness match the answer key, "
        "citations pass, and there are no unresolved retrieval errors."
    ),
    "requirement_accuracy": "Checklist statuses matching the answer key, out of five per evaluated case.",
    "overall_readiness_accuracy": "Cases whose final readiness matches the answer key, out of all evaluated cases.",
    "case_citation_validation": (
        "Cases with a valid report, all cited references matched, and no validation, execution, or guardrail errors."
    ),
    "schema_success": "Cases with a schema-valid report for the requested patient and review date.",
    "citation_reference_matches": (
        "Individual citations whose resource type and ID match successfully retrieved FHIR records within patient scope."
    ),
    "unknown_findings": "Findings marked UNKNOWN, out of five checklist slots per evaluated case.",
    "cases_with_retrieval_errors": "Cases with at least one recorded retrieval error, including recovered errors.",
    "cases_with_execution_errors": "Cases with at least one agent execution or trace audit error.",
    "cases_with_validation_errors": "Cases with at least one recorded report or evidence validation error.",
    "cases_with_guardrail_violations": "Cases with at least one detected guardrail violation during the run.",
    "total_tool_calls": "Total FHIR tool dispatches recorded across evaluated cases.",
    "total_case_duration_ms": "Sum of elapsed execution time for all evaluated cases.",
}


def metric_rows(run: Evaluation) -> list[dict]:
    """Pair saved metric values with display labels and explanations."""
    rows = []
    for key, label in METRIC_LABELS.items():
        if (rate := getattr(run.metrics, key)) is not None:
            rows.append({"key": key, "label": label, "explanation": METRIC_EXPLANATIONS[key], "rate": rate})
    rows.extend([
        {"key": "total_tool_calls", "label": "Tool calls", "explanation": METRIC_EXPLANATIONS["total_tool_calls"],
         "value": run.metrics.total_tool_calls},
        {"key": "total_case_duration_ms", "label": "Duration", "explanation": METRIC_EXPLANATIONS["total_case_duration_ms"],
         "value": f"{run.duration_seconds:.2f} s"},
    ])
    return rows


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
    mode = request.GET.get("prompt")
    instructions = run.system_instructions
    if mode == "download" and instructions is not None:
        return HttpResponse(instructions.encode("utf-8"), content_type="text/plain; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="rrr-prompt-{run_id}.txt"'})
    raw = mode == "raw"
    prompt = (literal_prompt(instructions) if raw else formatted_prompt(instructions)) if instructions else None
    cases = [{"case": case, "mismatches": [row for row in checklist(case) if row["mismatch"]]}
             for case in run.cases]
    requirement_rates = [(label, run.metrics.per_requirement.get(key)) for key, label in REQUIREMENT_LABELS.items()]
    return show(request, "detail", run=run, cases=cases, metrics=metric_rows(run),
                requirement_rates=requirement_rates, raw_prompt=raw, rendered_prompt=prompt)


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
    return show(request, "comparison", before=before, after=after, rows=rows, metrics=metrics,
                diff_lines=display_diff(diff), newline="\n")
