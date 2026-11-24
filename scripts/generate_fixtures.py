#!/usr/bin/env python3
"""Generate synthetic FHIR evidence and a separate evaluation answer key."""

from __future__ import annotations

import argparse
import base64
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5


SEED = 20261125
REVIEW_DATE = date(2026, 11, 25)
VERSION = "rrr-v1"
NAMESPACE = uuid5(NAMESPACE_URL, "urn:rrr:referral-readiness-agent:fixtures:rrr-v1")
PROTECTED_PATIENT_IDS = {
    "a349d267-f0bb-41a3-ba33-902d9b2fad5c",
    "a34bf068-1fc2-429e-8be9-98aa34689c84",
}

CHECKLIST = ("referral_reason", "symptom_history", "physical_assessment", "ecg", "relevant_history")


@dataclass(frozen=True)
class Scenario:
    label: str
    missing: str | None = None
    ecg_age_days: int = 2
    distributed: bool = False


# Keep IDs stable across changes in ordering. Use a new version for a changed dataset.
SCENARIOS = {
    "001": Scenario("complete"),
    "002": Scenario("missing medication history", "relevant_history"),
    "003": Scenario("outdated ECG", ecg_age_days=91),
    "004": Scenario("distributed evidence", distributed=True),
    "005": Scenario("missing referral reason", "referral_reason"),
    "006": Scenario("missing symptom history", "symptom_history"),
    "007": Scenario("missing physical assessment", "physical_assessment"),
    "008": Scenario("missing ECG", "ecg"),
    "009": Scenario("ECG at the 90-day boundary", ecg_age_days=90),
}


def resource_id(case_id, key):
    return str(uuid5(NAMESPACE, f"{case_id}/{key}"))


def reference(resource):
    return {"reference": f"urn:uuid:{resource['id']}"}


def loinc(code, display):
    return {"coding": [{"system": "http://loinc.org", "code": code, "display": display}]}


def encounter(case_id, key, patient, day):
    return {
        "resourceType": "Encounter",
        "id": resource_id(case_id, key),
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": reference(patient),
        "period": {"start": day.isoformat()},
    }


def clinical_note(case_id, key, patient, visit, code, title, narrative):
    return {
        "resourceType": "DiagnosticReport",
        "id": resource_id(case_id, key),
        "status": "final",
        "code": loinc(code, title),
        "subject": reference(patient),
        "encounter": reference(visit),
        "effectiveDateTime": visit["period"]["start"],
        "presentedForm": [{
            "contentType": "text/plain",
            "data": base64.b64encode(narrative.encode("utf-8")).decode("ascii"),
        }],
    }


def quantity(value, unit, code):
    return {"value": value, "unit": unit, "system": "http://unitsofmeasure.org", "code": code}


def vital_sign(case_id, key, patient, visit, code, display, measurement):
    return {
        "resourceType": "Observation",
        "id": resource_id(case_id, key),
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "vital-signs",
        }]}],
        "code": loinc(code, display),
        "subject": reference(patient),
        "encounter": reference(visit),
        "effectiveDateTime": visit["period"]["start"],
        **measurement,
    }


def build_case(case_id, scenario):
    # Each case has its own random stream so adding a scenario does not change earlier cases.
    rng = Random(f"{SEED}/{case_id}")
    patient_id = resource_id(case_id, "patient")
    if patient_id in PROTECTED_PATIENT_IDS:
        raise ValueError(f"Fixture ID collides with a protected patient: {patient_id}")
    patient = {
        "resourceType": "Patient",
        "id": patient_id,
        "active": True,
        "identifier": [{"system": "urn:rrr:referral-readiness-agent:fixtures", "value": patient_id}],
        "birthDate": date(rng.randint(1965, 2000), rng.randint(1, 12), rng.randint(1, 28)).isoformat(),
    }
    visit = encounter(case_id, "consult", patient, REVIEW_DATE - timedelta(days=5))
    resources = [patient, visit]
    paragraphs = {
        "referral_reason": "Reason for proposed cardiology referral: palpitations.",
        "symptom_history": (
            f"Palpitations occur {rng.randint(2, 5)} times per week, last "
            f"{rng.randint(2, 8)} minutes, and are sometimes accompanied by lightheadedness."
        ),
        "physical_assessment": "Cardiovascular examination: regular rhythm without audible murmur.",
        "relevant_history": (
            "Patient reports taking no current medications. "
            "Patient reports no prior cardiac disease and no family history of sudden cardiac death."
        ),
    }
    if scenario.missing == "relevant_history":
        paragraphs["relevant_history"] = (
            "Patient reports no prior cardiac disease and no family history of sudden cardiac death."
        )
    elif scenario.missing in paragraphs:
        del paragraphs[scenario.missing]

    if scenario.distributed:
        history_visit = encounter(case_id, "history-visit", patient, REVIEW_DATE - timedelta(days=3))
        resources.append(history_visit)
        resources.append(clinical_note(
            case_id, "history-note", patient, history_visit, "11506-3", "Progress note",
            paragraphs.pop("relevant_history"),
        ))
    resources.append(clinical_note(
        case_id, "consult-note", patient, visit, "11506-3", "Progress note",
        "\n".join(paragraphs.values()),
    ))

    if scenario.missing != "physical_assessment":
        resources.append(vital_sign(
            case_id, "blood-pressure", patient, visit, "85354-9", "Blood pressure panel",
            {"component": [
                {"code": loinc("8480-6", "Systolic blood pressure"),
                 "valueQuantity": quantity(rng.randint(108, 132), "mmHg", "mm[Hg]")},
                {"code": loinc("8462-4", "Diastolic blood pressure"),
                 "valueQuantity": quantity(rng.randint(62, 84), "mmHg", "mm[Hg]")},
            ]},
        ))
        resources.append(vital_sign(
            case_id, "heart-rate", patient, visit, "8867-4", "Heart rate",
            {"valueQuantity": quantity(rng.randint(60, 92), "beats/minute", "/min")},
        ))

    if scenario.missing != "ecg":
        ecg_day = REVIEW_DATE - timedelta(days=scenario.ecg_age_days)
        ecg_visit = encounter(case_id, "ecg-visit", patient, ecg_day)
        resources.extend([ecg_visit, clinical_note(
            case_id, "ecg-note", patient, ecg_visit, "28570-0", "Procedure note",
            "12-lead ECG: sinus rhythm. No acute ST-segment changes documented.",
        )])

    checklist = dict.fromkeys(CHECKLIST, "present")
    if scenario.missing:
        checklist[scenario.missing] = "missing"
    if scenario.ecg_age_days > 90:
        checklist["ecg"] = "outdated"
    outcome = {
        "patient_id": patient_id,
        "scenario": scenario.label,
        "status": "READY" if all(value == "present" for value in checklist.values()) else "NEEDS_REVIEW",
        "checklist": checklist,
    }
    return resources, outcome


def generate():
    evidence = {"resourceType": "Bundle", "type": "collection", "entry": []}
    expected = {"dataset": VERSION, "seed": SEED, "review_date": REVIEW_DATE.isoformat(), "cases": []}

    for case_id, scenario in SCENARIOS.items():
        resources, outcome = build_case(case_id, scenario)
        evidence["entry"].extend(
            {"fullUrl": f"urn:uuid:{resource['id']}", "resource": resource}
            for resource in resources
        )
        expected["cases"].append(outcome)
    return evidence, expected


def publish_json(output):
    """Publish complete files without replacing an existing file, even on a concurrent run."""
    for path, content in output.items():
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise FileExistsError(f"Refusing to replace {path}. Choose a fresh output directory.")

    for path, content in output.items():
        if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=path.parent) as temporary:
            staged = Path(temporary) / path.name
            staged.write_bytes(content)
            try:
                os.link(staged, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "evals",
        help="parent directory for fixtures/ and expected/ (default: repository evals/)",
    )
    args = parser.parse_args()
    evidence, expected = generate()
    output = {
        args.output_dir / "fixtures" / f"{VERSION}.json": evidence,
        args.output_dir / "expected" / f"{VERSION}.json": expected,
    }
    try:
        publish_json({
            path: (json.dumps(data, indent=2) + "\n").encode("utf-8")
            for path, data in output.items()
        })
    except OSError as error:
        parser.exit(1, f"{error}\n")
    for path in output:
        print(path)


if __name__ == "__main__":
    main()
