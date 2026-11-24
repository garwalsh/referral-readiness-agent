"""Validate Claude's checklist draft and apply the demo readiness policy."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Requirement(str, Enum):
    REFERRAL = "referral_reason"
    SYMPTOMS = "symptom_history"
    EXAM = "physical_assessment"
    ECG = "ecg"
    HISTORY = "relevant_history"


class Component(str, Enum):
    REFERRAL = "referral_reason"
    FREQUENCY = "symptom_frequency"
    DURATION = "symptom_duration"
    ASSOCIATED = "associated_symptoms"
    BP = "blood_pressure"
    HR = "heart_rate"
    EXAM = "cardiovascular_exam"
    ECG = "ecg"
    MEDICATION = "medication_history"
    CARDIAC = "cardiac_history"
    FAMILY = "family_history"


CHECKLIST = {
    Requirement.REFERRAL: (Component.REFERRAL,),
    Requirement.SYMPTOMS: (Component.FREQUENCY, Component.DURATION, Component.ASSOCIATED),
    Requirement.EXAM: (Component.BP, Component.HR, Component.EXAM),
    Requirement.ECG: (Component.ECG,),
    Requirement.HISTORY: (Component.MEDICATION, Component.CARDIAC, Component.FAMILY),
}
VITAL_CODES = {Component.BP: "85354-9", Component.HR: "8867-4"}
EvidenceType = Literal["DiagnosticReport", "Observation"]


class FindingStatus(str, Enum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    OUTDATED = "OUTDATED"
    UNKNOWN = "UNKNOWN"


class Readiness(str, Enum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNABLE_TO_ASSESS = "UNABLE_TO_ASSESS"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftEvidence(ClosedModel):
    component: Component
    resource_type: EvidenceType
    resource_id: str
    quote: Optional[str]  # Exact note excerpt, or null for a structured vital sign.


class DraftFinding(ClosedModel):
    requirement_id: Requirement
    status: FindingStatus
    evidence: list[DraftEvidence]


def require_complete_checklist(findings: list[DraftFinding] | list[Finding]) -> None:
    if len(findings) != len(CHECKLIST) or {item.requirement_id for item in findings} != set(CHECKLIST):
        raise ValueError("Expected exactly one finding for each checklist requirement.")


class DraftReport(ClosedModel):
    """The model supplies findings. The schema gives it no overall status or prose field."""

    findings: list[DraftFinding]

    @model_validator(mode="after")
    def validate_checklist(self) -> DraftReport:
        require_complete_checklist(self.findings)
        return self


class EvidenceReference(ClosedModel):
    resource_type: EvidenceType
    resource_id: str
    date: date
    excerpts: list[str] = Field(default_factory=list)
    measurement: Optional[str] = None
    encounter_reference: Optional[str] = None


class Finding(ClosedModel):
    requirement_id: Requirement
    status: FindingStatus
    evidence: list[EvidenceReference]
    explanation: str

    @model_validator(mode="after")
    def require_evidence(self) -> Finding:
        if self.status == FindingStatus.PRESENT and not self.evidence:
            raise ValueError("PRESENT requires verified evidence.")
        return self


class RetrievalError(ClosedModel):
    tool: str
    resource_type: Optional[str]
    message: str


def readiness_for(findings: list[Finding]) -> Readiness:
    statuses = {item.status for item in findings}
    if FindingStatus.UNKNOWN in statuses:
        return Readiness.UNABLE_TO_ASSESS
    if statuses == {FindingStatus.PRESENT}:
        return Readiness.READY
    return Readiness.NEEDS_REVIEW


class Report(ClosedModel):
    patient_id: str
    review_date: date
    findings: list[Finding]
    readiness: Readiness
    retrieval_errors: list[RetrievalError]
    uncertainties: list[str]

    @model_validator(mode="after")
    def validate_report(self) -> Report:
        require_complete_checklist(self.findings)
        if self.readiness != readiness_for(self.findings):
            raise ValueError("Readiness must match the deterministic policy.")
        return self


def evidence_date(resource: dict[str, Any], component: Component) -> date:
    value = resource.get("effectiveDateTime") or (resource.get("effectivePeriod") or {}).get("start")
    if not value and component != Component.ECG:
        value = resource.get("issued")
    if not isinstance(value, str) or len(value) < 10:
        raise ValueError("Cited evidence has no usable date.")
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise ValueError("Cited evidence has an invalid date.") from None


def codes(resource: dict[str, Any]) -> set[str]:
    return {coding["code"] for coding in resource.get("code", {}).get("coding", []) if "code" in coding}


def has_measurement(resource: dict[str, Any]) -> bool:
    quantity = resource.get("valueQuantity")
    return isinstance(quantity, dict) and type(quantity.get("value")) in (int, float)


# Output guardrails
def verify_support(
    item: DraftEvidence, resources: dict[tuple[str, str], dict[str, Any]], review_date: date
) -> EvidenceReference:
    # The index contains only resources that passed the runner's patient-scope checks.
    resource = resources.get((item.resource_type, item.resource_id))
    if resource is None:
        raise ValueError("A citation was not retrieved from this patient's chart during this run.")
    day = evidence_date(resource, item.component)
    if day > review_date:
        raise ValueError("A citation is dated after the review date.")

    excerpt, measurement = None, None
    if item.component in VITAL_CODES:
        if item.resource_type != "Observation" or VITAL_CODES[item.component] not in codes(resource):
            raise ValueError("A vital-sign citation has the wrong resource type or code.")
        if item.component == Component.BP:
            measured = {
                code: part["valueQuantity"]["value"]
                for part in resource.get("component", []) if has_measurement(part)
                for code in codes(part)
            }
            if not {"8480-6", "8462-4"} <= measured.keys():
                raise ValueError("Cited blood pressure has no complete measured reading.")
            measurement = f"{measured['8480-6']:g}/{measured['8462-4']:g} mmHg"
        elif not has_measurement(resource):
            raise ValueError("Cited heart rate has no measured reading.")
        else:
            quantity = resource["valueQuantity"]
            unit = quantity.get("unit") or quantity.get("code") or ""
            measurement = f"{quantity['value']:g} {unit}".strip()
    else:
        # Exact excerpts verify the source exists. Claude still interprets what the text supports.
        if item.resource_type != "DiagnosticReport" or not item.quote or not item.quote.strip():
            raise ValueError("Narrative support requires an exact excerpt from a clinical report.")
        excerpt = " ".join(item.quote.split())
        if not any(excerpt in " ".join(note.split()) for note in resource.get("notes", [])):
            raise ValueError("A cited excerpt does not occur in the retrieved report.")

    # Persist only a short, verified excerpt or measurement for the report display.
    encounter = resource.get("encounter") or {}
    reference = encounter.get("reference")
    return EvidenceReference(
        resource_type=item.resource_type, resource_id=item.resource_id, date=day,
        excerpts=[excerpt[:240]] if excerpt else [], measurement=measurement,
        encounter_reference=reference if isinstance(reference, str) else None,
    )


def validate_finding(
    draft: DraftFinding,
    resources: dict[tuple[str, str], dict[str, Any]],
    complete_searches: set[str],
    review_date: date,
) -> Finding:
    required = CHECKLIST[draft.requirement_id]
    support: dict[Component, list[EvidenceReference]] = {}
    for item in draft.evidence:
        if item.component not in required:
            raise ValueError("Citation supports a component outside this checklist requirement.")
        support.setdefault(item.component, []).append(verify_support(item, resources, review_date))

    # Missing documentation requires a completed search. Positive evidence can resolve
    # a gap even when a different search failed or was incomplete.
    required_searches = {"DiagnosticReport"}
    if draft.requirement_id == Requirement.EXAM:
        required_searches.add("Observation")
    complete = required_searches <= complete_searches
    unsupported = [component.value.replace("_", " ") for component in required if component not in support]
    combined: dict[tuple[str, str], EvidenceReference] = {}
    for references in support.values():
        for item in references:
            current = combined.setdefault((item.resource_type, item.resource_id), item)
            current.excerpts = list(dict.fromkeys([*current.excerpts, *item.excerpts]))
            if item.measurement and item.measurement != current.measurement:
                current.measurement = ", ".join(filter(None, (current.measurement, item.measurement)))
    evidence = list(combined.values())
    status = draft.status
    if draft.requirement_id == Requirement.ECG and status in {FindingStatus.PRESENT, FindingStatus.OUTDATED}:
        if not evidence:
            raise ValueError("An ECG finding requires a dated 12-lead ECG citation.")
        age = (review_date - max(item.date for item in evidence)).days
        if age <= 90:
            status, explanation = FindingStatus.PRESENT, f"12-lead ECG documented {age} days before review."
        elif complete:
            status, explanation = FindingStatus.OUTDATED, f"Latest cited 12-lead ECG was {age} days before review."
        else:
            status, explanation = FindingStatus.UNKNOWN, "An older ECG was found, but retrieval did not establish whether a recent ECG exists."
    elif status == FindingStatus.PRESENT:
        if unsupported:
            raise ValueError("PRESENT was proposed without verified support for every required component.")
        explanation = "Documentation verified for " + ", ".join(part.value.replace("_", " ") for part in required) + "."
    elif status == FindingStatus.MISSING:
        if not unsupported:
            raise ValueError("MISSING contradicts the proposed supporting evidence.")
        if complete:
            explanation = "Documentation not found for " + ", ".join(unsupported) + "."
        else:
            status, explanation = FindingStatus.UNKNOWN, "The relevant search was incomplete, so a documentation gap could not be established."
    elif status == FindingStatus.UNKNOWN:
        explanation = "The retrieved evidence did not resolve this requirement."
    else:
        raise ValueError("OUTDATED applies only to the ECG requirement.")

    return Finding(requirement_id=draft.requirement_id, status=status, evidence=evidence, explanation=explanation)


def build_report(
    draft_json: Optional[str],
    patient_id: str,
    review_date: date,
    resources: dict[tuple[str, str], dict[str, Any]],
    complete_searches: set[str],
    retrieval_errors: list[RetrievalError],
    run_failure: Optional[str] = None,
) -> tuple[Report, list[str]]:
    validation_errors = []
    drafts = {}
    if draft_json is not None:
        try:
            drafts = {item.requirement_id: item for item in DraftReport.model_validate_json(draft_json).findings}
        except ValidationError as error:
            error_types = ", ".join(sorted({item["type"] for item in error.errors()}))
            validation_errors.append(f"Invalid Claude checklist draft ({error_types}).")

    findings = []
    for requirement in CHECKLIST:
        if requirement in drafts:
            try:
                findings.append(validate_finding(drafts[requirement], resources, complete_searches, review_date))
                continue
            except ValueError as error:
                validation_errors.append(f"{requirement.value}: {error}")
                explanation = "Proposed evidence did not pass validation."
            except (TypeError, AttributeError, KeyError):
                validation_errors.append(f"{requirement.value}: Cited resource had malformed evidence fields.")
                explanation = "Proposed evidence did not pass validation."
        else:
            explanation = "No valid checklist assessment was available."
        findings.append(Finding(
            requirement_id=requirement, status=FindingStatus.UNKNOWN, evidence=[], explanation=explanation
        ))

    uncertainties = ([run_failure] if run_failure else []) + [
        f"{item.requirement_id.value}: {item.explanation}"
        for item in findings if item.status == FindingStatus.UNKNOWN
    ]
    report = Report(patient_id=patient_id, review_date=review_date, findings=findings,
                    readiness=readiness_for(findings), retrieval_errors=retrieval_errors,
                    uncertainties=uncertainties)
    return report, validation_errors
