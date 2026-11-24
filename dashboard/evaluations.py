"""Read and compare completed evaluator artifacts without a database."""
from dataclasses import dataclass
from datetime import date, datetime
from difflib import unified_diff
from pathlib import Path
import hashlib
import json
import re
from uuid import UUID

from django.conf import settings
from pydantic import BaseModel, Field, model_validator

from scripts.reporting import FindingStatus, Readiness, Report, Requirement

RUN_DIRECTORY = re.compile(r"[0-9]{8}T[0-9]{6}Z-(?P<id>[0-9a-f-]{36})")
METRIC_LABELS = {
    "strict_case_accuracy": "Strict case accuracy",
    "requirement_accuracy": "Checklist requirement accuracy",
    "overall_readiness_accuracy": "Overall readiness accuracy",
    "case_citation_validation": "Citation validity",
    "schema_success": "Schema validation",
    "citation_reference_matches": "Citation reference matches",
    "unknown_findings": "Unknown findings",
    "cases_with_retrieval_errors": "Cases with retrieval errors",
    "cases_with_execution_errors": "Cases with execution errors",
    "cases_with_validation_errors": "Cases with validation errors",
    "cases_with_guardrail_violations": "Cases with guardrail violations",
}
REQUIREMENTS = {item.value for item in Requirement}


class Rate(BaseModel):
    count: int = Field(ge=0)
    total: int = Field(ge=0)
    percentage: float | None = Field(ge=0, le=100)


class EvaluationMetrics(BaseModel):
    strict_case_accuracy: Rate
    requirement_accuracy: Rate
    overall_readiness_accuracy: Rate
    case_citation_validation: Rate
    schema_success: Rate | None = None
    citation_reference_matches: Rate | None = None
    unknown_findings: Rate | None = None
    cases_with_retrieval_errors: Rate | None = None
    cases_with_execution_errors: Rate | None = None
    cases_with_validation_errors: Rate | None = None
    cases_with_guardrail_violations: Rate | None = None
    per_requirement: dict[str, Rate] = Field(default_factory=dict)
    total_tool_calls: int = Field(ge=0)
    total_case_duration_ms: float = Field(ge=0)


class EvaluationCase(BaseModel):
    fixture_id: UUID
    patient_id: UUID
    agent_run_id: UUID
    scenario: str
    expected_readiness: Readiness
    actual_readiness: Readiness | None
    expected_checklist: dict[str, FindingStatus]
    actual_checklist: dict[str, FindingStatus]
    strict_pass: bool
    citations_valid: bool
    tool_calls: int = Field(ge=0)
    execution_errors: list[str]
    validation_errors: list[str]
    guardrail_violations: list[str]
    report_path: str | None = None
    trace_path: str | None = None

    @model_validator(mode="after")
    def validate_checklists(self):
        if self.expected_checklist.keys() != REQUIREMENTS or self.actual_checklist.keys() not in (set(), REQUIREMENTS):
            raise ValueError("Unexpected checklist")
        return self


class Evaluation(BaseModel):
    evaluation_run_id: UUID
    started_at: datetime
    finished_at: datetime
    model: str
    dataset: str
    review_date: date
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    system_instructions: str | None = None
    metrics: EvaluationMetrics
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_run(self):
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise ValueError("Expected timestamp offsets")
        if self.finished_at < self.started_at:
            raise ValueError("Evaluation finish precedes its start")
        if self.system_instructions is not None:
            if hashlib.sha256(self.system_instructions.encode()).hexdigest() != self.prompt_sha256:
                raise ValueError("Prompt hash does not match recorded instructions")
        keys = {case.fixture_id for case in self.cases}
        if len(keys) != len(self.cases) or len(keys) != self.metrics.strict_case_accuracy.total:
            raise ValueError("Incomplete or duplicate fixture results")
        return self

    @property
    def duration_seconds(self) -> float:
        return self.metrics.total_case_duration_ms / 1000


@dataclass(frozen=True)
class SavedEvaluation:
    directory: Path
    result: Evaluation


def read_artifact(directory: Path, relative: str) -> bytes:
    """Read bounded files within one run. Reject traversal and symlinked components."""
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Invalid artifact path")
    candidate = directory
    for part in path.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError("Symlinked artifact")
    if not candidate.is_file():
        raise ValueError("Missing artifact")
    with candidate.open("rb") as stream:
        data = stream.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Oversized artifact")
    return data


def discover() -> tuple[dict[UUID, SavedEvaluation], int]:
    """Return validated runs and the count of skipped or unreadable runs."""
    root = Path(settings.RRR_EVAL_DIR)
    try:
        folders = sorted(root.iterdir())
    except FileNotFoundError:
        return {}, 0
    except OSError:
        return {}, 1
    runs = {}
    skipped = 0
    for directory in folders:
        match = RUN_DIRECTORY.fullmatch(directory.name)
        if not match:
            continue
        try:
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("Invalid evaluation directory")
            result = Evaluation.model_validate_json(read_artifact(directory, "results.json"))
            if str(result.evaluation_run_id) != match["id"] or result.evaluation_run_id in runs:
                raise ValueError("Mismatched or duplicate evaluation ID")
            runs[result.evaluation_run_id] = SavedEvaluation(directory, result)
        except (OSError, ValueError):
            skipped += 1
    return runs, skipped


def case_artifact(saved: SavedEvaluation, case: EvaluationCase, filename: str) -> bytes:
    relative = case.report_path if filename == "report.json" else case.trace_path
    if not relative or not re.fullmatch(rf"[0-9]+-{case.fixture_id}/{re.escape(filename)}", relative):
        raise ValueError("Artifact must belong to this case")
    return read_artifact(saved.directory, relative)


def load_report(saved: SavedEvaluation, case: EvaluationCase) -> Report:
    report = Report.model_validate_json(case_artifact(saved, case, "report.json"))
    if report.patient_id != str(case.patient_id) or report.review_date != saved.result.review_date:
        raise ValueError("Mismatched report identity")
    statuses = {item.requirement_id.value: item.status for item in report.findings}
    if report.readiness != case.actual_readiness or statuses != case.actual_checklist:
        raise ValueError("Report disagrees with evaluation results")
    return report


def load_trace(saved: SavedEvaluation, case: EvaluationCase) -> list[dict]:
    events = [json.loads(line) for line in case_artifact(saved, case, "trace.jsonl").splitlines()]
    if not events or any(not isinstance(event, dict) for event in events):
        raise ValueError("Invalid event trace")
    if any(events[0].get(key) != value for key, value in {
        "event": "start", "run_id": str(case.agent_run_id), "patient_id": str(case.patient_id),
        "model": saved.result.model, "review_date": saved.result.review_date.isoformat(),
    }.items()):
        raise ValueError("Mismatched trace identity")
    return events


def compare(before: Evaluation, after: Evaluation) -> tuple[list[dict], list[str]]:
    if before.dataset != after.dataset:
        raise ValueError("Choose two evaluations of the same dataset.")
    before_cases = {case.fixture_id: case for case in before.cases}
    after_cases = {case.fixture_id: case for case in after.cases}
    if before_cases.keys() != after_cases.keys():
        raise ValueError("These evaluations contain different sets of fixture IDs.")
    if not before.system_instructions or not after.system_instructions:
        raise ValueError("Both evaluations need recorded system instructions to show a prompt diff.")
    outcomes = {(False, True): "Improved", (True, False): "Regressed",
                (True, True): "Stayed correct", (False, False): "Stayed incorrect"}
    rows = []
    for key, left in before_cases.items():
        right = after_cases[key]
        rows.append({"before": left, "after": right, "outcome": outcomes[left.strict_pass, right.strict_pass]})
    diff = list(unified_diff(before.system_instructions.splitlines(keepends=True),
                             after.system_instructions.splitlines(keepends=True),
                             fromfile="Before", tofile="After", lineterm=""))
    return rows, diff
