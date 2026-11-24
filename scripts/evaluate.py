"""Evaluate the existing RRR agent against the nine live rrr-v1 fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Optional
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from reporting import FindingStatus, Readiness, Report, Requirement
from run_agent import MAX_TOOL_CALLS, READ_TYPES, ROOT, SEARCH_FILTERS, SYSTEM, settings

MODEL = "claude-haiku-5-5"
REVIEW_DATE = "2026-11-25"
DATASET = "rrr-v1"
ANSWER_KEY = ROOT / "evals" / "expected" / f"{DATASET}.json"
FIXTURE_MAP = ROOT / "local-config" / "live-fixture-ids.json"
OUTPUT = ROOT / "evals" / "output"
TIMEOUT_SECONDS = 300
REQUIREMENTS = tuple(item.value for item in Requirement)
Json = dict[str, Any]
Reference = tuple[str, str]


@dataclass(frozen=True)
class Case:
    fixture_id: str
    patient_id: str
    scenario: str
    readiness: str
    checklist: dict[str, str]


def join_cases(key: Json, live_map: Json) -> list[Case]:
    """Join by fixture UUID and validate the complete dataset before agent calls."""
    if key["dataset"] != DATASET or key["review_date"] != REVIEW_DATE or len(key["cases"]) != 9:
        raise ValueError("Expected nine rrr-v1 cases with the fixed review date.")
    cases = []
    for row in key["cases"]:
        fixture_id = str(UUID(row["patient_id"]))
        patient_id = str(UUID(live_map[fixture_id]["patient"]))
        if set(row["checklist"]) != set(REQUIREMENTS):
            raise ValueError(f"Incomplete answer key for fixture {fixture_id}.")
        checklist = {name: FindingStatus(row["checklist"][name].upper()).value for name in REQUIREMENTS}
        cases.append(Case(fixture_id, patient_id, row["scenario"], Readiness(row["status"]).value, checklist))
    if len({c.fixture_id for c in cases}) != 9 or len({c.patient_id for c in cases}) != 9:
        raise ValueError("Fixture IDs and mapped FHIR patient IDs must each be unique.")
    return cases


@dataclass
class Audit:
    tool_calls: int = 0
    tool_attempts: int = 0
    references: set[Reference] = field(default_factory=set)
    execution_errors: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    retrieval_errors: list[Json] = field(default_factory=list)
    guardrail_violations: list[str] = field(default_factory=list)


def scoped(tool: str, params: Json, patient: str, seen: set[Reference]) -> bool:
    kind = params.get("resourceType")
    if tool == "fhir_search" and kind in SEARCH_FILTERS and set(params) == {"resourceType", "queryParams"}:
        query = parse_qs(params["queryParams"], keep_blank_values=True)
        filters = query.keys() - {"patient", "_count"}
        return (query.get("patient") == [patient] and query.get("_count") == ["25"]
                and filters <= SEARCH_FILTERS[kind] and all(len(query[key]) == 1 for key in filters))
    if tool == "fhir_read" and kind in READ_TYPES and set(params) == {"resourceType", "id"}:
        return params["id"] == patient if kind == "Patient" else (kind, params["id"]) in seen
    return False


def audit_trace(events: list[Json], case: Case, run_id: str, report: Optional[Report]) -> Audit:
    """Use only successful patient-scoped tool results as citation provenance."""
    audit = Audit()
    identity = {"run_id": run_id, "patient_id": case.patient_id, "review_date": REVIEW_DATE, "model": MODEL}
    if not events or events[0].get("event") != "start" or any(
        events[0].get(key) != value for key, value in identity.items()
    ):
        audit.execution_errors.append("Trace start is missing or does not match the case run.")
    starts = []
    validations = []
    for event in events:
        event_type = event.get("event")
        if event_type == "run_error":
            audit.execution_errors.append(event["message"])
        elif event_type == "validation_error":
            audit.validation_errors.append(event["message"])
        elif event_type == "validation_finish":
            validations.append(event["failures"])
        elif event_type == "tool_start":
            starts.append((event["tool"], event["parameters"]))
            audit.tool_calls += 1
        elif event_type == "tool":
            audit.tool_attempts += 1
            tool, params = event["tool"], event["parameters"]
            if params is None:
                audit.guardrail_violations.append(f"Attempt {audit.tool_attempts}: {event['error']}")
                continue
            if not starts or starts.pop(0) != (tool, params):
                audit.execution_errors.append("A tool result has no matching dispatch.")
                continue
            if not scoped(tool, params, case.patient_id, audit.references):
                audit.guardrail_violations.append(f"Attempt {audit.tool_attempts}: invalid tool or patient scope.")
                continue
            if "error" in event:
                error = event["error"]
                audit.retrieval_errors.append({"tool": tool, "resource_type": params["resourceType"],
                                               "message": error})
                if error.startswith(("LangCare returned evidence outside", "LangCare returned an unexpected",
                                     "LangCare returned a different resource")):
                    audit.guardrail_violations.append(f"Attempt {audit.tool_attempts}: {error}")
                continue
            references = {(ref["resource_type"], ref["resource_id"]) for ref in event["result"]["references"]}
            kind = params["resourceType"]
            if any(ref[0] != kind for ref in references) or (
                tool == "fhir_read" and references != {(kind, params["id"])}
            ):
                audit.guardrail_violations.append(f"Attempt {audit.tool_attempts}: unexpected resource reference.")
                continue
            audit.references.update(references)

    if starts:
        audit.execution_errors.append("Trace contains an unfinished tool dispatch.")
    if max(audit.tool_calls, audit.tool_attempts) > MAX_TOOL_CALLS:
        audit.guardrail_violations.append(f"Trace exceeds the {MAX_TOOL_CALLS}-attempt budget.")
    if report is not None:
        last = events[-1] if events else {}
        statuses = {item.requirement_id.value: item.status.value for item in report.findings}
        if (last.get("event"), last.get("run_id"), last.get("statuses"), last.get("readiness")) != (
            "finish", run_id, statuses, report.readiness.value
        ):
            audit.execution_errors.append("Trace finish is missing or does not match the structured report.")
        if validations != [len(audit.validation_errors)]:
            audit.execution_errors.append("Trace validation phase is missing or incomplete.")
    return audit


def score(case: Case, report: Optional[Report], audit: Audit) -> Json:
    """Give case credit only when labels, evidence, schema and execution pass."""
    actual = {item.requirement_id.value: item.status.value for item in report.findings} if report else {}
    readiness = report.readiness.value if report else None
    mismatches = [
        {"requirement": name, "expected": expected, "actual": actual.get(name)}
        for name, expected in case.checklist.items() if actual.get(name) != expected
    ]
    invalid_citations = []
    citation_count = 0
    if report:
        for item in report.findings:
            for ref in item.evidence:
                citation_count += 1
                if (ref.resource_type, ref.resource_id) not in audit.references:
                    invalid_citations.append({
                        "requirement": item.requirement_id.value,
                        "resource_type": ref.resource_type,
                        "resource_id": ref.resource_id,
                    })
    citations_valid = report is not None and not (
        invalid_citations or audit.validation_errors or audit.execution_errors or audit.guardrail_violations
    )
    retrieval_errors = ([error.model_dump() for error in report.retrieval_errors]
                        if report else audit.retrieval_errors)
    # A recovered retrieval error can coexist with a fully assessed, valid report.
    unresolved_retrieval = bool(retrieval_errors and "UNKNOWN" in actual.values())
    return {
        "fixture_id": case.fixture_id, "patient_id": case.patient_id, "scenario": case.scenario,
        "expected_readiness": case.readiness, "actual_readiness": readiness,
        "expected_checklist": case.checklist, "actual_checklist": actual, "mismatches": mismatches,
        "schema_valid": report is not None, "readiness_correct": readiness == case.readiness,
        "strict_pass": citations_valid and not mismatches and readiness == case.readiness
        and not unresolved_retrieval,
        "citations_valid": citations_valid, "citation_count": citation_count,
        "invalid_citations": invalid_citations, "tool_calls": audit.tool_calls,
        "tool_attempts": audit.tool_attempts, "execution_errors": audit.execution_errors,
        "validation_errors": audit.validation_errors, "retrieval_errors": retrieval_errors,
        "unresolved_retrieval": unresolved_retrieval, "guardrail_violations": audit.guardrail_violations,
    }


def run_case(case: Case, directory: Path) -> Json:
    directory.mkdir(mode=0o700)
    run_id = str(uuid4())
    trace_path = directory / "trace.jsonl"
    report_path = directory / "report.json"
    started = perf_counter()
    report = None
    execution_errors = []
    validation_errors = []
    # The subprocess receives only run inputs. The evaluator retains the answer key.
    command = [sys.executable, str(ROOT / "scripts/run_agent.py"), case.patient_id, "--review-date",
               REVIEW_DATE, "--model", MODEL, "--run-id", run_id, "--trace-path", str(trace_path)]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=TIMEOUT_SECONDS)
        if result.returncode:
            execution_errors.append(f"Agent exited with code {result.returncode}.")
        else:
            try:
                report = Report.model_validate_json(result.stdout)
                if (report.patient_id, report.review_date.isoformat()) != (case.patient_id, REVIEW_DATE):
                    raise ValueError("Report identifies a different patient or date.")
            except ValueError:
                # Raw invalid output can contain record text. Preserve the trace and failure category.
                report = None
                validation_errors.append("Agent output failed report schema or patient/date validation.")
    except subprocess.TimeoutExpired:
        execution_errors.append(f"Agent exceeded the {TIMEOUT_SECONDS}-second case timeout.")
    except OSError as error:
        execution_errors.append(f"Could not launch agent ({type(error).__name__}).")

    try:
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        audit = audit_trace(events, case, run_id, report)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        audit = Audit(execution_errors=["Agent trace could not be read or audited."])
    audit.execution_errors.extend(execution_errors)
    audit.validation_errors.extend(validation_errors)
    if report:
        report_path.write_text(report.model_dump_json(indent=2) + "\n")
    return {
        **score(case, report, audit), "agent_run_id": run_id,
        "duration_ms": round((perf_counter() - started) * 1000, 1),
        "trace_path": f"{directory.name}/{trace_path.name}" if trace_path.exists() else None,
        "report_path": f"{directory.name}/{report_path.name}" if report else None,
    }


def rate(count: int, total: int) -> Json:
    return {"count": count, "total": total, "percentage": round(100 * count / total, 2) if total else None}


def metrics(cases: list[Json]) -> Json:
    count = len(cases)
    correct = {name: sum(case["actual_checklist"].get(name) == case["expected_checklist"][name]
                         for case in cases) for name in REQUIREMENTS}
    cited = sum(case["citation_count"] for case in cases)
    unmatched = sum(len(case["invalid_citations"]) for case in cases)
    unknown = sum(list(case["actual_checklist"].values()).count("UNKNOWN") for case in cases)
    result = {
        "strict_case_accuracy": rate(sum(case["strict_pass"] for case in cases), count),
        "requirement_accuracy": rate(sum(correct.values()), count * len(REQUIREMENTS)),
        "overall_readiness_accuracy": rate(sum(case["readiness_correct"] for case in cases), count),
        "schema_success": rate(sum(case["schema_valid"] for case in cases), count),
        "case_citation_validation": rate(sum(case["citations_valid"] for case in cases), count),
        "citation_reference_matches": rate(cited - unmatched, cited),
        "unknown_findings": rate(unknown, count * len(REQUIREMENTS)),
        "per_requirement": {name: rate(value, count) for name, value in correct.items()},
        "total_tool_calls": sum(case["tool_calls"] for case in cases),
        "total_tool_attempts": sum(case["tool_attempts"] for case in cases),
        "total_case_duration_ms": round(sum(case["duration_ms"] for case in cases), 1),
        "mean_case_duration_ms": round(sum(case["duration_ms"] for case in cases) / count, 1),
    }
    for name in ("retrieval_errors", "execution_errors", "validation_errors", "guardrail_violations"):
        result[f"cases_with_{name}"] = rate(sum(bool(case[name]) for case in cases), count)
        result[f"total_{name}"] = sum(len(case[name]) for case in cases)
    return result


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> Optional[str]:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL,
                                       text=True).rstrip()
    except (OSError, subprocess.CalledProcessError):
        return None


def provenance(answer_key: bytes, fixture_map: bytes) -> Json:
    paths = ["scripts/run_agent.py", "scripts/reporting.py", "scripts/verify_langcare.py"]
    files = {path: sha256((ROOT / path).read_bytes()) for path in paths}
    return {
        "model": MODEL, "dataset": DATASET, "review_date": REVIEW_DATE,
        "answer_key_sha256": sha256(answer_key), "fixture_map_sha256": sha256(fixture_map),
        "git_head": git("rev-parse", "HEAD"), "agent_worktree_status": git("status", "--short", "--", *paths),
        "agent_files_sha256": files, "agent_sha256": sha256(json.dumps(files, sort_keys=True).encode()),
        "evaluator_sha256": sha256(Path(__file__).read_bytes()),
        "prompt_sha256": sha256(SYSTEM.encode()), "system_instructions": SYSTEM,
    }


def markdown(result: Json) -> str:
    def percent(metric: Json) -> str:
        suffix = f" ({metric['percentage']:.2f}%)" if metric["percentage"] is not None else ""
        return f"{metric['count']}/{metric['total']}{suffix}"

    lines = [
        "# RRR evaluation", "", f"- Run `{result['evaluation_run_id']}`",
        f"- Started {result['started_at']} and finished {result['finished_at']}",
        f"- Model `{result['model']}`, dataset `{result['dataset']}`, review date `{result['review_date']}`",
        f"- Agent SHA-256 `{result['agent_sha256']}` at Git HEAD `{result['git_head']}`",
        f"- System prompt SHA-256 `{result['prompt_sha256']}`",
        "- [Machine-readable results and exact system instructions](results.json)",
        "", "## Metrics", "",
    ]
    if result["agent_worktree_status"]:
        lines.insert(7, "- Agent source includes working-tree changes. Exact source hashes are in the JSON.")
    for name, value in result["metrics"].items():
        if name == "per_requirement":
            lines.extend(f"- {item.replace('_', ' ').capitalize()} accuracy — {percent(rate)}"
                         for item, rate in value.items())
        else:
            display = percent(value) if isinstance(value, dict) else value
            lines.append(f"- {name.replace('_', ' ').capitalize()} — {display}")
    lines += ["", "## Cases", "", "| Fixture | Scenario | Expected | Actual | Strict | Calls | Duration | Artifacts |",
              "| --- | --- | --- | --- | --- | ---: | ---: | --- |"]
    for case in result["cases"]:
        links = ", ".join(f"[{name}]({case[f'{name}_path']})" for name in ("trace", "report") if case[f"{name}_path"])
        lines.append(f"| `{case['fixture_id']}` | {case['scenario']} | {case['expected_readiness']} | "
                     f"{case['actual_readiness'] or 'No report'} | {'Pass' if case['strict_pass'] else 'Fail'} | "
                     f"{case['tool_calls']} | {case['duration_ms'] / 1000:.2f} s | {links or '—'} |")
    lines += ["", "## Failure details", ""]
    for case in result["cases"]:
        if case["strict_pass"] and not case["retrieval_errors"]:
            continue
        lines += [f"### {case['scenario']} — `{case['fixture_id']}`", ""]
        if not case["readiness_correct"]:
            actual = case["actual_readiness"] or "NO_REPORT"
            lines.append(f"- Readiness expected `{case['expected_readiness']}` and received `{actual}`.")
        for mismatch in case["mismatches"]:
            lines.append(f"- `{mismatch['requirement']}` expected `{mismatch['expected']}` and "
                         f"received `{mismatch['actual'] or 'NO_REPORT'}`.")
        for name in ("execution_errors", "retrieval_errors", "validation_errors", "guardrail_violations"):
            lines.extend(f"- {name.replace('_', ' ').capitalize()} — "
                         f"{error['message'] if isinstance(error, dict) else error}" for error in case[name])
        for ref in case["invalid_citations"]:
            lines.append(f"- Unmatched citation `{ref['resource_type']}/{ref['resource_id']}` "
                         f"for `{ref['requirement']}`.")
        lines.append("")
    if all(case["strict_pass"] and not case["retrieval_errors"] for case in result["cases"]):
        lines.append("All cases passed without retrieval errors.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    config = settings()
    required = ("ANTHROPIC_API_KEY", "LANGCARE_MCP_URL", "LANGCARE_MCP_TOKEN")
    missing = [name for name in required if not config.get(name)]
    if missing:
        parser.error("Configure the agent environment or .env before evaluating: " + ", ".join(missing))
    try:
        answer_key, fixture_map = ANSWER_KEY.read_bytes(), FIXTURE_MAP.read_bytes()
        cases = join_cases(json.loads(answer_key), json.loads(fixture_map))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        parser.error(f"Could not load the evaluation dataset and live fixture map ({type(error).__name__}).")

    started = datetime.now(timezone.utc)
    eval_id = str(uuid4())
    OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = OUTPUT / f"{started:%Y%m%dT%H%M%SZ}-{eval_id}"
    directory.mkdir(mode=0o700)
    result = {
        **provenance(answer_key, fixture_map), "evaluation_run_id": eval_id,
        "started_at": started.isoformat(), "cases": [],
    }
    print(f"Evaluation artifacts: {directory}", flush=True)
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case.scenario}", flush=True)
        result["cases"].append(run_case(case, directory / f"{index:02d}-{case.fixture_id}"))

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["metrics"] = metrics(result["cases"])
    (directory / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    (directory / "summary.md").write_text(markdown(result))
    print(f"Summary: {directory / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
