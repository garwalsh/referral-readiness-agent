"""Offline checks for fixture identity, strict scoring and case invocation."""
import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from evaluate import (
    ANSWER_KEY, MODEL, REVIEW_DATE, Audit, audit_trace, join_cases, metrics, run_case, score,
)
from reporting import Report


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.key = json.loads(ANSWER_KEY.read_bytes())
        self.live_map = {row['patient_id']: {'patient': str(uuid5(NAMESPACE_URL, row['patient_id']))}
                         for row in self.key['cases']}
        self.case = join_cases(self.key, self.live_map)[0]
        self.ref = {"resource_type": "DiagnosticReport", "resource_id": "example"}
        self.report = Report.model_validate({
            "patient_id": self.case.patient_id, "review_date": REVIEW_DATE,
            "findings": [
                {"requirement_id": name, "status": status, "explanation": "Test finding",
                 "evidence": [{**self.ref, "date": REVIEW_DATE}]}
                for name, status in self.case.checklist.items()
            ],
            "readiness": self.case.readiness, "retrieval_errors": [], "uncertainties": [],
        })

    def events(self, run_id, patient=None):
        params = {"resourceType": "DiagnosticReport",
                  "queryParams": f"patient={patient or self.case.patient_id}&_count=25"}
        return [
            {"event": "start", "run_id": run_id, "patient_id": self.case.patient_id,
             "model": MODEL, "review_date": REVIEW_DATE},
            {"event": "tool_start", "tool": "fhir_search", "parameters": params},
            {"event": "tool", "tool": "fhir_search", "parameters": params,
             "result": {"references": [self.ref]}},
            {"event": "validation_finish", "failures": 0},
            {"event": "finish", "run_id": run_id, "readiness": self.case.readiness,
             "statuses": self.case.checklist},
        ]

    def test_join_by_fixture_id_and_validate_map(self):
        reordered = dict(reversed(list(self.live_map.items())))
        self.assertEqual(join_cases(self.key, reordered)[0], self.case)
        reordered.pop(self.case.fixture_id)
        with self.assertRaises(KeyError):
            join_cases(self.key, reordered)

    def test_strict_pass_and_requirement_metric_are_independent(self):
        audit = audit_trace(self.events("example"), self.case, "example", self.report)
        self.assertTrue(score(self.case, self.report, audit)["strict_pass"])
        expected = replace(self.case, checklist={**self.case.checklist, "ecg": "OUTDATED"},
                           readiness="NEEDS_REVIEW")
        result = {**score(expected, self.report, audit), "duration_ms": 10}
        self.assertFalse(result["strict_pass"])
        self.assertEqual(metrics([result])["requirement_accuracy"]["count"], 4)
        self.assertEqual(metrics([result])["overall_readiness_accuracy"]["count"], 0)

    def test_valid_labels_do_not_excuse_unmatched_evidence(self):
        bad_events = self.events("example")
        bad_events[2]["result"]["references"] = []
        audit = audit_trace(bad_events, self.case, "example", self.report)
        result = score(self.case, self.report, audit)
        self.assertEqual(len(result["invalid_citations"]), 5)
        self.assertTrue(result["readiness_correct"])
        self.assertFalse(result["strict_pass"])

    def test_reject_out_of_scope_and_excess_attempts(self):
        events = self.events("example", patient="other")
        events[1:3] *= 11
        audit = audit_trace(events, self.case, "example", self.report)
        self.assertEqual(audit.tool_calls, 11)
        self.assertEqual(len(audit.guardrail_violations), 12)
        self.assertFalse(score(self.case, self.report, audit)["strict_pass"])

    def test_execution_and_validation_failure_with_schema_valid_report(self):
        for audit in (Audit(execution_errors=["Agent failed"]), Audit(validation_errors=["Evidence failed"])):
            with self.subTest(audit=audit):
                audit.references.add(("DiagnosticReport", "example"))
                result = score(self.case, self.report, audit)
                self.assertTrue(result["schema_valid"])
                self.assertFalse(result["strict_pass"])

    @patch("evaluate.subprocess.run")
    def test_case_invocation_and_artifacts(self, launch):
        def simulate(command, **options):
            self.assertEqual(command[2:7], [self.case.patient_id, "--review-date", REVIEW_DATE, "--model", MODEL])
            self.assertEqual(command[7], "--run-id")
            self.assertEqual(command[9], "--trace-path")
            self.assertEqual(len(command), 11)
            Path(command[10]).write_text("\n".join(json.dumps(row) for row in self.events(command[8])))
            return subprocess.CompletedProcess(command, 0, self.report.model_dump_json().encode(), b"")

        launch.side_effect = simulate
        with TemporaryDirectory() as directory:
            result = run_case(self.case, Path(directory) / "case")
            self.assertTrue(result["strict_pass"])
            self.assertTrue((Path(directory) / result["report_path"]).is_file())
            self.assertTrue((Path(directory) / result["trace_path"]).is_file())
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
