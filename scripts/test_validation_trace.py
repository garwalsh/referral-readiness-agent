"""Exercise the real runner, scoped retrieval and citation trace without network calls."""
from base64 import b64encode
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reporting import Report, Requirement
from run_agent import main

PATIENT = "a349d267-f0bb-41a3-ba33-902d9b2fad5c"
OTHER_PATIENT = "00000000-0000-4000-8000-000000000001"
SOURCE = "  12-LEAD ECG REPORT\r\n\r\n- Normal sinus rhythm.\t\n- Heart rate: 78 bpm. café"
WRONG_QUOTE = "  12-lead ECG\r\n—  Normal sinus rhythm. cafe\u0301 "
CITATION_ERROR = "A cited excerpt does not occur in the retrieved report."
MODEL_KEY = "mock-anthropic-secret"
LANGCARE_TOKEN = "mock-langcare-secret"


def clinical_note(resource_id: str, patient: str, text: str) -> dict:
    return {
        "resourceType": "DiagnosticReport", "id": resource_id, "effectiveDateTime": "2026-11-23",
        "subject": {"reference": f"Patient/{patient}"},
        "presentedForm": [{"contentType": "text/plain", "data": b64encode(text.encode()).decode()}],
    }


class CitationTraceTests(unittest.TestCase):
    def review(self, quote: str, citation_id: str = "ecg", *, outside_patient: bool = False,
               source: str = SOURCE) -> tuple[Report, list[dict], str]:
        resources = [clinical_note("ecg", OTHER_PATIENT if outside_patient else PATIENT, source)]
        if not outside_patient:
            resources.append(clinical_note("consultation", PATIENT, "Unrelated retrieved report."))
        findings = [{"requirement_id": requirement.value, "status": "MISSING", "evidence": []}
                    for requirement in Requirement]
        next(finding for finding in findings if finding["requirement_id"] == "ecg").update(
            status="PRESENT",
            evidence=[{"component": "ecg", "resource_type": "DiagnosticReport",
                       "resource_id": citation_id, "quote": quote}],
        )

        def transport(tool: str, parameters: dict) -> dict:
            self.assertEqual(tool, "fhir_search")
            self.assertEqual(parameters, {"resourceType": "DiagnosticReport",
                                          "queryParams": f"patient={PATIENT}&_count=25"})
            return {"resourceType": "Bundle", "type": "searchset",
                    "entry": [{"resource": resource} for resource in resources]}

        def model(messages: list[dict], allow_tools: bool) -> dict:
            if len(messages) == 1:
                self.assertTrue(allow_tools)
                return {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "search", "name": "fhir_search",
                     "input": {"resourceType": "DiagnosticReport"}},
                ]}
            self.assertEqual(messages[-1]["content"][0]["is_error"], outside_patient)
            return {"stop_reason": "end_turn", "content": [
                {"type": "text", "text": json.dumps({"findings": findings})},
            ]}

        config = {"ANTHROPIC_API_KEY": MODEL_KEY, "LANGCARE_MCP_TOKEN": LANGCARE_TOKEN,
                  "LANGCARE_MCP_URL": "https://synthetic.example/mcp"}
        stdout, stderr = StringIO(), StringIO()
        with TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            args = ["run_agent.py", PATIENT, "--review-date", "2026-11-25", "--trace-path", str(trace)]
            with (patch("sys.argv", args), patch("run_agent.settings", return_value=config),
                  patch("run_agent.connect_langcare", return_value=transport),
                  patch("run_agent.claude_client", return_value=model),
                  redirect_stdout(stdout), redirect_stderr(stderr)):
                main()
            lines = trace.read_text()
        return Report.model_validate_json(stdout.getvalue()), [json.loads(line) for line in lines.splitlines()], (
            stdout.getvalue() + stderr.getvalue()
        )

    def test_bad_quote_preserves_original_comparison_on_the_same_error_event(self):
        report, events, console = self.review(WRONG_QUOTE)
        ecg = next(finding for finding in report.findings if finding.requirement_id.value == "ecg")
        self.assertEqual((ecg.status.value, ecg.evidence, report.readiness.value),
                         ("UNKNOWN", [], "UNABLE_TO_ASSESS"))
        failures = [event for event in events if event["event"] == "validation_error"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["message"], "ecg: " + CITATION_ERROR)
        self.assertEqual(failures[0]["requirement"], "ecg")
        self.assertEqual(failures[0]["citation"], {
            "component": "ecg", "resource_type": "DiagnosticReport", "resource_id": "ecg",
            "submitted_quote": WRONG_QUOTE, "source_texts": [SOURCE], "reason": CITATION_ERROR,
        })
        self.assertNotIn("Unrelated retrieved report.", json.dumps(events))
        self.assertNotIn("12-LEAD ECG REPORT", console)
        self.assertNotIn("Normal sinus rhythm", console)

    def test_successful_quote_does_not_record_source_text(self):
        report, events, _ = self.review("Normal sinus rhythm.")
        ecg = next(finding for finding in report.findings if finding.requirement_id.value == "ecg")
        self.assertEqual(ecg.status.value, "PRESENT")
        self.assertFalse(any(event["event"] == "validation_error" for event in events))
        self.assertNotIn("12-LEAD ECG REPORT", json.dumps(events))

    def test_cross_patient_resource_never_enters_validation_or_trace(self):
        report, events, _ = self.review(WRONG_QUOTE, outside_patient=True, source="Private other-patient note")
        self.assertEqual(report.readiness.value, "UNABLE_TO_ASSESS")
        tool = next(event for event in events if event["event"] == "tool")
        self.assertIn("outside the selected patient's chart", tool["error"])
        diagnostic = next(event["citation"] for event in events if event["event"] == "validation_error")
        self.assertEqual(diagnostic["resource_id"], "ecg")
        self.assertIn("not retrieved from this patient's chart", diagnostic["reason"])
        self.assertEqual(diagnostic["source_texts"], [])
        self.assertNotIn("Private other-patient note", json.dumps(events))

    def test_wrong_resource_reports_only_the_cited_attachment(self):
        _, events, _ = self.review("Normal sinus rhythm.", "consultation")
        diagnostic = next(event["citation"] for event in events if event["event"] == "validation_error")
        self.assertEqual(diagnostic["resource_id"], "consultation")
        self.assertEqual(diagnostic["reason"], CITATION_ERROR)
        self.assertEqual(diagnostic["source_texts"], ["Unrelated retrieved report."])

    def test_runtime_credentials_are_redacted_from_failed_comparisons(self):
        _, events, _ = self.review(f"Wrong quote {MODEL_KEY}", source=f"Synthetic note {LANGCARE_TOKEN}")
        diagnostic = next(event["citation"] for event in events if event["event"] == "validation_error")
        self.assertEqual(diagnostic["submitted_quote"], "Wrong quote [REDACTED]")
        self.assertEqual(diagnostic["source_texts"], ["Synthetic note [REDACTED]"])


if __name__ == "__main__":
    unittest.main()
