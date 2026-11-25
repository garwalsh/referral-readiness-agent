"""Authentication, CSRF, and configuration-page smoke tests."""

import json
import os
import subprocess
from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from scripts.reporting import CHECKLIST, Finding, FindingStatus, Report, readiness_for

from .models import ReviewRun
from .worker import claim_next, execute_run, read_events, trace_path


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class DashboardPagesTest(TestCase):
    patient = "a34c2416-8263-4819-9acf-690e2e6fdd01"

    @classmethod
    def setUpTestData(cls) -> None:
        users = get_user_model()
        cls.admin = users.objects.create_superuser("demo-admin", password="test-door-key-459")
        cls.viewer = users.objects.create_user("viewer", password="test-door-key-459")

    def test_application_pages_require_login(self) -> None:
        for name in ("run_agent", "results", "history"):
            url = reverse(name)
            with self.subTest(page=name):
                self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_login_requires_administrator_and_csrf(self) -> None:
        client = Client(enforce_csrf_checks=True)
        url = reverse("login")
        credentials = {"username": self.admin.username, "password": "test-door-key-459"}
        self.assertEqual(client.post(url, credentials).status_code, 403)
        self.assertContains(client.get(url), "Administrator sign-in")
        credentials["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        viewer = dict(credentials, username=self.viewer.username)
        self.assertContains(client.post(url, viewer), "dashboard administrator account")
        self.assertRedirects(client.post(url, credentials), reverse("run_agent"))
        self.assertEqual(client.get(reverse("logout")).status_code, 405)
        credentials["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertRedirects(client.post(reverse("logout"), credentials), reverse("login"))

    def test_non_admin_session_cannot_view_dashboard(self) -> None:
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("run_agent")).status_code, 302)

    def test_configuration_uses_fixture_ids_and_configured_models(self) -> None:
        self.client.force_login(self.admin)
        with TemporaryDirectory() as directory:
            fixture_map = Path(directory) / "fixture-map.json"
            fixture_map.write_text(json.dumps({"offline-id": {"patient": self.patient, "note": "EXCLUDED_TEXT"}}))
            with self.settings(RRR_FIXTURE_MAP=fixture_map, RRR_CLAUDE_MODELS=("claude-sonnet-4-6",)):
                response = self.client.get(reverse("run_agent"))
        self.assertContains(response, self.patient)
        self.assertContains(response, "claude-sonnet-4-6")
        self.assertNotContains(response, "claude-haiku-5-5")
        self.assertContains(response, 'value="2026-11-25"')
        self.assertNotContains(response, "EXCLUDED_TEXT")
        self.assertContains(response, "Run Review")

    def test_missing_fixture_map_and_empty_pages_render(self) -> None:
        self.client.force_login(self.admin)
        with TemporaryDirectory() as directory:
            with self.settings(RRR_FIXTURE_MAP=Path(directory) / "missing.json"):
                self.assertContains(self.client.get(reverse("run_agent")), "fixture ID map is unavailable")
        self.assertContains(self.client.get(reverse("results")), "No report selected")
        self.assertContains(self.client.get(reverse("history")), "No reviews recorded.")
        self.assertEqual(self.client.post(reverse("run_agent"), {"patient_id": "invalid"}).status_code, 400)


    def test_queue_submission_validates_patient_and_model(self) -> None:
        self.client.force_login(self.admin)
        with TemporaryDirectory() as directory:
            fixture_map = Path(directory) / "fixture-map.json"
            fixture_map.write_text(json.dumps({"offline-id": {"patient": self.patient}}))
            with self.settings(RRR_FIXTURE_MAP=fixture_map):
                url = reverse("run_agent")
                form = {"patient_id": self.patient, "model": "claude-sonnet-4-6", "review_date": "2026-11-25"}
                self.assertEqual(self.client.post(url, dict(form, patient_id=str(uuid4()))).status_code, 400)
                self.assertEqual(self.client.post(url, dict(form, model="unverified")).status_code, 400)
                self.assertEqual(ReviewRun.objects.count(), 0)
                response = self.client.post(url, form)
        run = ReviewRun.objects.get()
        self.assertEqual(run.created_by, self.admin)
        self.assertEqual(run.model, "claude-sonnet-4-6")
        self.assertEqual(run.review_date, date(2026, 11, 25))
        self.assertEqual(run.state, ReviewRun.State.QUEUED)
        self.assertRedirects(response, reverse("run_detail", args=[run.id]))
        self.assertEqual(self.client.get(reverse("run_progress", args=[run.id])).json(),
                         {"state": "Queued", "terminal": False, "events": [], "elapsed_ms": None,
                          "activity": {"mode": "queued", "label": "Waiting for the worker",
                                       "detail": "This review is queued."}})
        self.client.logout()
        for name in ("run_detail", "run_progress"):
            self.assertEqual(self.client.get(reverse(name, args=[run.id])).status_code, 302)


class ReviewWorkerTest(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.admin = get_user_model().objects.create_superuser("worker-test")

    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        fixture_map = path / "fixture-map.json"
        fixture_map.write_text(json.dumps({"fixture": {"patient": DashboardPagesTest.patient}}))
        config = override_settings(RRR_RUN_DIR=path, RRR_FIXTURE_MAP=fixture_map,
                                   SECURE_SSL_REDIRECT=False, CSRF_COOKIE_SECURE=False)
        config.enable()
        self.addCleanup(config.disable)

    def queue(self) -> ReviewRun:
        return ReviewRun.objects.create(
            created_by=self.admin, patient_id=DashboardPagesTest.patient,
            model="claude-sonnet-4-6", review_date=date(2026, 11, 25),
        )

    def test_worker_keeps_runner_report_and_actual_events(self) -> None:
        run = self.queue()
        claimed = claim_next()
        self.assertEqual(claimed.pk, run.pk)
        self.assertIsNone(claim_next())
        findings = [Finding(requirement_id=key, status=FindingStatus.MISSING,
                            evidence=[], explanation="Documentation not found.") for key in CHECKLIST]
        report = Report(patient_id=str(run.patient_id), review_date=run.review_date, findings=findings,
                        readiness=readiness_for(findings), retrieval_errors=[], uncertainties=[])
        events = [
            {"event": "start", "run_id": str(run.id), "model": run.model,
             "patient_id": str(run.patient_id), "review_date": run.review_date.isoformat()},
            {"event": "tool", "tool": "fhir_search", "parameters": {"resourceType": "DiagnosticReport"},
             "result": {"count": 0}},
            {"event": "validation_finish", "failures": 0},
            {"event": "finish", "run_id": str(run.id), "readiness": report.readiness.value},
        ]

        def run_cli(command, **kwargs):
            self.assertEqual(command[command.index("--model") + 1], run.model)
            self.assertEqual(command[command.index("--run-id") + 1], str(run.pk))
            self.assertNotIn("DJANGO_SECRET_KEY", kwargs["env"])
            path = Path(command[command.index("--trace-path") + 1])
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            return subprocess.CompletedProcess(command, 0, stdout=report.model_dump_json())

        with patch("dashboard.worker.subprocess.run", side_effect=run_cli):
            execute_run(claimed)
        run.refresh_from_db()
        self.assertEqual(run.state, ReviewRun.State.COMPLETED)
        self.assertEqual(run.readiness, "NEEDS_REVIEW")
        self.assertEqual(run.tool_calls, 1)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("history")), str(run.pk))
        detail = self.client.get(reverse("run_detail", args=[run.pk]))
        self.assertContains(detail, "Documentation needs review")
        self.assertContains(detail, 'data-status="MISSING" data-count="5"')
        self.assertContains(detail, 'class="checklist-row" data-status="MISSING"', count=5)
        self.assertContains(detail, "Documentation not found.", count=5)
        self.assertContains(detail, 'aria-label="Copy patient ID"')
        self.assertContains(detail, 'aria-label="Copy run ID"')
        self.assertContains(detail, "Documentation findings")
        progress = self.client.get(reverse("run_progress", args=[run.pk])).json()
        self.assertTrue(progress["terminal"])
        self.assertEqual([event["label"] for event in progress["events"]],
                         ["Agent started", "Tool completed", "Report validation finished", "Readiness calculated"])

    def test_queued_review_shows_execution_without_a_documentation_result(self) -> None:
        run = self.queue()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("run_detail", args=[run.pk]))
        self.assertContains(response, "Waiting for the worker")
        self.assertContains(response, 'id="run-state">Queued')
        self.assertContains(response, 'id="execution-elapsed"')
        self.assertContains(response, '<span class="execution-dot" aria-hidden="true"></span>', count=2)
        self.assertContains(response, "Waiting for the first trace event.")
        self.assertNotContains(response, "Checklist summary")

    def test_progress_reports_only_observed_activity_and_measures_from_claim(self) -> None:
        run = self.queue()
        run = claim_next()
        run.started_at = timezone.now() - timedelta(seconds=7)
        run.save(update_fields=["started_at"])
        self.client.force_login(self.admin)
        url = reverse("run_progress", args=[run.pk])
        path = trace_path(run)
        snapshots = [
            ({"event": "start"}, "running", "Agent running", "OK"),
            ({"event": "connection_start"}, "waiting", "Connecting to clinical records", "Started"),
            ({"event": "connection_finish", "outcome": "completed"}, "running", "Agent running", "OK"),
            ({"event": "tool_start"}, "waiting", "Retrieving patient documentation", "Started"),
            ({"event": "tool", "error": "Network stalled.", "timed_out": True},
             "running", "Agent running", "Timed out"),
            ({"event": "model_start"}, "waiting", "Reviewing documentation", "Started"),
            ({"event": "model_finish", "outcome": "completed"}, "running", "Agent running", "OK"),
            ({"event": "model_finish", "outcome": "failed"}, "running", "Agent running", "Error"),
            ({"event": "model_finish", "outcome": "timed_out"}, "running", "Agent running", "Timed out"),
            ({"event": "validation_start"}, "running", "Validating findings", "Started"),
            ({"event": "validation_finish"}, "running", "Preparing readiness report", "OK"),
        ]
        model_labels = {"Started": "Model request started", "OK": "Model response received",
                        "Error": "Model request failed", "Timed out": "Model request timed out"}
        for event, mode, label, status in snapshots:
            with self.subTest(event=event):
                with path.open("a") as trace:
                    trace.write(json.dumps(event) + "\n")
                latest = self.client.get(url).json()
                self.assertEqual(latest["activity"]["label"], label)
                self.assertEqual(latest["activity"]["mode"], mode)
                self.assertEqual(latest["events"][-1]["status"], status)
                if event["event"] in {"model_start", "model_finish"}:
                    self.assertEqual(latest["events"][-1]["label"], model_labels[status])
                if event["event"] == "model_start":
                    self.assertEqual(latest["activity"]["detail"], "Waiting for the model to respond.")
                self.assertGreaterEqual(latest["elapsed_ms"], 7000)
                self.assertFalse(latest["terminal"])

    def test_stackone_worker_passes_only_selected_credentials_and_fails_closed(self) -> None:
        run = self.queue()
        claimed = claim_next()
        config = {"RRR_TRANSPORT": "stackone", "STACKONE_ACCOUNT_ID": "account",
                  "STACKONE_OAUTH_FILE": "/var/lib/rrr-stackone/stackone-oauth.json",
                  "LANGCARE_MCP_TOKEN": "unused-fallback", "LANGCARE_MCP_URL": "https://example.test"}
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="private OAuth details")
        with patch.dict(os.environ, config, clear=True), patch("dashboard.worker.subprocess.run", return_value=result) as launch:
            execute_run(claimed)
        launch.assert_called_once()
        child_env = launch.call_args.kwargs["env"]
        self.assertEqual(child_env["RRR_TRANSPORT"], "stackone")
        self.assertEqual(child_env["STACKONE_OAUTH_FILE"], config["STACKONE_OAUTH_FILE"])
        self.assertNotIn("LANGCARE_MCP_TOKEN", child_env)
        self.assertNotIn("LANGCARE_MCP_URL", child_env)
        run.refresh_from_db()
        self.assertEqual(run.state, ReviewRun.State.FAILED)
        self.assertNotIn("private OAuth details", run.error)

    def test_run_detail_shows_transport_and_gateway_log_reference(self) -> None:
        run = self.queue()
        events = [
            {"event": "start", "transport": "stackone"},
            {"event": "gateway_action", "action": "fhir_search", "action_run_id": "probe-id",
             "defender_applied": True, "defender_risk": "low"},
        ]
        trace_path(run).write_text("".join(json.dumps(event) + "\n" for event in events))
        self.client.force_login(self.admin)
        detail = self.client.get(reverse("run_detail", args=[run.pk]))
        self.assertContains(detail, "StackOne Gateway")
        self.assertContains(detail, "Action run ID · probe-id")
        self.assertContains(detail, "Response scanned")

    def test_worker_timeout_is_terminal_and_keeps_the_error_message(self) -> None:
        self.queue()
        run = claim_next()
        with patch("dashboard.worker.subprocess.run", side_effect=subprocess.TimeoutExpired([], 900)):
            execute_run(run)
        self.client.force_login(self.admin)
        progress = self.client.get(reverse("run_progress", args=[run.pk])).json()
        self.assertEqual(progress["state"], "Timed out")
        self.assertEqual(progress["activity"]["mode"], "timed-out")
        self.assertTrue(progress["terminal"])
        detail = self.client.get(reverse("run_detail", args=[run.pk]))
        self.assertContains(detail, "The agent exceeded the worker time limit.")
        self.assertNotContains(detail, 'class="execution-dot"')
        self.assertNotContains(detail, "data-progress-url")

    def test_citation_diagnostics_are_private_to_the_admin_run_detail(self) -> None:
        run = self.queue()
        source = '  <script>synthetic attachment</script>\r\né'
        trace_path(run).write_text(json.dumps({
            "event": "validation_error", "requirement": "ecg", "message": "ecg: quote mismatch",
            "citation": {"component": "ecg", "resource_type": "DiagnosticReport", "resource_id": "ecg-id",
                         "submitted_quote": "Wrong excerpt", "source_texts": [source], "reason": "quote mismatch"},
        }) + "\n")
        detail_url = reverse("run_detail", args=[run.pk])
        progress_url = reverse("run_progress", args=[run.pk])
        self.assertEqual(self.client.get(detail_url).status_code, 302)
        self.assertEqual(self.client.get(progress_url).status_code, 302)
        viewer = get_user_model().objects.create_user("worker-viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(detail_url).status_code, 302)
        self.assertEqual(self.client.get(progress_url).status_code, 302)

        self.client.force_login(self.admin)
        detail = self.client.get(detail_url)
        self.assertContains(detail, "ECG documentation")
        self.assertContains(detail, "DiagnosticReport/ecg-id")
        self.assertContains(detail, "Wrong excerpt")
        self.assertContains(detail, '&lt;script&gt;synthetic attachment&lt;/script&gt;\\r\\né')
        self.assertNotContains(detail, "<script>synthetic attachment</script>")
        self.assertIn("no-store", detail["Cache-Control"])
        progress = self.client.get(progress_url)
        self.assertContains(progress, "quote mismatch")
        self.assertNotContains(progress, "synthetic attachment")
        self.assertNotContains(progress, "Wrong excerpt")

    def test_partial_line_is_hidden_and_failed_process_is_audited(self) -> None:
        run = self.queue()
        claimed = claim_next()
        trace_path(run).write_text('{"event":"start"}\n{"event":')
        self.assertEqual(read_events(run), [{"event": "start"}])
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="SECRET_TRACE_TEXT")
        with patch("dashboard.worker.subprocess.run", return_value=result):
            execute_run(claimed)
        run.refresh_from_db()
        self.assertEqual(run.state, ReviewRun.State.FAILED)
        self.assertNotIn("SECRET_TRACE_TEXT", run.error)
        self.assertTrue(run.finished_at)

    def test_expired_worker_claim_is_failed_without_retry(self) -> None:
        run = self.queue()
        claim_next()
        ReviewRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=1))
        self.assertIsNone(claim_next())
        run.refresh_from_db()
        self.assertEqual(run.state, ReviewRun.State.FAILED)
