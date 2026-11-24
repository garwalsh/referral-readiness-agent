"""Evaluation page tests using temporary artifacts and no model calls."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .evaluations import REQUIREMENTS, discover


@override_settings(SECURE_SSL_REDIRECT=False)
class EvaluationPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.admin = users.objects.create_user("evaluation-admin", is_staff=True)
        cls.viewer = users.objects.create_user("evaluation-viewer")

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        settings = self.settings(RRR_EVAL_DIR=self.root)
        settings.enable()
        self.addCleanup(settings.disable)
        self.client.force_login(self.admin)
        self.fixture = str(uuid4())
        self.other_fixture = str(uuid4())

    def write_run(self, prompt="Original instructions.\n<literal markup>", reverse_order=False):
        run_id = str(uuid4())
        directory = self.root / f"20261124T200047Z-{run_id}"
        directory.mkdir()
        cases = []
        for fixture, passed in [(self.fixture, reverse_order), (self.other_fixture, not reverse_order)]:
            case = {
                "fixture_id": fixture, "patient_id": str(uuid4()), "agent_run_id": str(uuid4()),
                "scenario": "missing referral reason" if fixture == self.fixture else "other case",
                "strict_pass": passed, "citations_valid": True, "tool_calls": 2,
                "expected_readiness": "NEEDS_REVIEW", "actual_readiness": "READY",
                "expected_checklist": dict.fromkeys(REQUIREMENTS, "PRESENT") | {"referral_reason": "MISSING"},
                "actual_checklist": dict.fromkeys(REQUIREMENTS, "PRESENT"),
                "execution_errors": [], "validation_errors": [], "guardrail_violations": [],
                "report_path": f"01-{fixture}/report.json", "trace_path": f"01-{fixture}/trace.jsonl",
            }
            cases.append(case)

        rate = {"count": 1, "total": 2, "percentage": 50}
        now = datetime.now(timezone.utc).isoformat()
        result = {
            "evaluation_run_id": run_id, "model": "claude-haiku-5-5", "dataset": "rrr-v1",
            "review_date": "2026-11-25", "started_at": now, "finished_at": now,
            "prompt_sha256": sha256((prompt or "").encode()).hexdigest(), "system_instructions": prompt,
            "metrics": {
                "strict_case_accuracy": rate, "requirement_accuracy": rate, "overall_readiness_accuracy": rate,
                "case_citation_validation": rate, "total_tool_calls": 4, "total_case_duration_ms": 2123,
            },
            "cases": list(reversed(cases)) if reverse_order else cases,
        }
        self.write_result(directory, result)
        case = cases[0]
        case_dir = directory / f"01-{self.fixture}"
        case_dir.mkdir()
        evidence = {"resource_type": "DiagnosticReport", "resource_id": "example",
                    "date": result["review_date"], "excerpts": ["Palpitations occur 2 times per week"]}
        report = {
            "patient_id": case["patient_id"], "review_date": result["review_date"], "readiness": "READY",
            "findings": [{"requirement_id": key, "status": status, "evidence": [evidence],
                          "explanation": "Test evidence"} for key, status in case["actual_checklist"].items()],
            "retrieval_errors": [], "uncertainties": [],
        }
        (directory / case["report_path"]).write_text(json.dumps(report))
        trace = [
            {"event": "start", "run_id": case["agent_run_id"], "patient_id": case["patient_id"],
             "model": result["model"], "review_date": result["review_date"]},
            {"event": "tool", "tool": "fhir_search", "parameters": {"resourceType": "DiagnosticReport",
             "queryParams": f"patient={case['patient_id']}", "credential": "unused-private-field"},
             "result": {"count": 1, "references": [{"resource_type": "DiagnosticReport", "resource_id": "example"}]}},
        ]
        (directory / case["trace_path"]).write_text("\n".join(map(json.dumps, trace)))
        return directory, result

    @staticmethod
    def write_result(directory, result):
        (directory / "results.json").write_text(json.dumps(result))

    def test_discovery_metrics_and_exact_historical_prompt(self):
        _, result = self.write_run()
        runs, skipped = discover()
        self.assertEqual((len(runs), skipped), (1, 0))
        history = self.client.get(reverse("evaluations"))
        self.assertContains(history, "1/2 (50.00%)")
        self.assertContains(history, "2.12 s")
        detail = self.client.get(reverse("evaluation_detail", args=[result["evaluation_run_id"]]))
        self.assertContains(detail, "Original instructions.\n&lt;literal markup&gt;")
        self.assertContains(detail, result["prompt_sha256"])
        self.assertContains(detail, "Referral reason · MISSING → PRESENT")

    def test_case_drilldown_shows_statuses_citation_and_trace(self):
        _, result = self.write_run()
        url = reverse("evaluation_case", args=[result["evaluation_run_id"], self.fixture])
        response = self.client.get(url)
        self.assertContains(response, "Palpitations occur 2 times per week")
        self.assertContains(response, 'data-status="MISSING">MISSING')
        self.assertContains(response, 'data-status="PRESENT">PRESENT')
        self.assertContains(response, "DiagnosticReport/example")
        self.assertContains(response, "1 returned")
        self.assertContains(response, "Citation validation")
        self.assertContains(response, "fhir_search")
        self.assertNotContains(response, "unused-private-field")

    def test_comparison_matches_fixture_ids_and_diffs_instructions(self):
        _, before = self.write_run("Common\nOld instructions")
        folder, after = self.write_run("Common\nNew instructions", reverse_order=True)
        url = reverse("evaluation_compare")
        selection = {"before": before["evaluation_run_id"], "after": after["evaluation_run_id"]}
        response = self.client.get(url, selection)
        self.assertContains(response, "Improved")
        self.assertContains(response, "Regressed")
        self.assertEqual(response.context["rows"][0]["after"].scenario, "missing referral reason")
        self.assertEqual(response.context["rows"][0]["outcome"], "Improved")
        self.assertContains(response, "-Old instructions")
        self.assertContains(response, "+New instructions")
        after["system_instructions"] = None
        self.write_result(folder, after)
        self.assertContains(self.client.get(url, selection), "need recorded system instructions")
        after["dataset"] = "different"
        self.write_result(folder, after)
        self.assertContains(self.client.get(url, selection), "same dataset")

    def test_malformed_and_missing_artifacts(self):
        directory, result = self.write_run()
        case = result["cases"][0]
        (directory / case["report_path"]).unlink()
        (directory / case["trace_path"]).write_text("broken JSON")
        url = reverse("evaluation_case", args=[result["evaluation_run_id"], self.fixture])
        response = self.client.get(url)
        self.assertContains(response, "Supporting evidence is unavailable")
        self.assertContains(response, "trace is missing or unreadable")
        (directory / "results.json").write_text("broken JSON")
        self.assertContains(self.client.get(reverse("evaluations")), "1 evaluation artifact could not be loaded")
        self.assertContains(self.client.get(reverse("evaluations")), "No completed evaluations")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_authentication_and_path_safety(self):
        directory, result = self.write_run()
        case_url = reverse("evaluation_case", args=[result["evaluation_run_id"], self.fixture])
        links = [reverse("evaluations"), case_url, reverse("evaluation_compare"),
                 reverse("evaluation_detail", args=[result["evaluation_run_id"]])]
        for user in (None, self.viewer):
            self.client.logout()
            if user:
                self.client.force_login(user)
            for link in links:
                self.assertEqual(self.client.get(link).status_code, 302)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/evaluations/%2e%2e/").status_code, 404)
        case = result["cases"][0]
        report = directory / case["report_path"]
        outside = self.root / "outside.json"
        report.rename(outside)
        report.symlink_to(outside)
        self.assertNotContains(self.client.get(case_url), "Palpitations occur 2 times per week")
        case["report_path"] = "../outside.json"
        self.write_result(directory, result)
        self.assertNotContains(self.client.get(case_url), "Palpitations occur 2 times per week")
        self.assertNotContains(self.client.get(case_url), self.root)
