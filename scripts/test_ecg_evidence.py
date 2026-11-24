"""Offline tests for ECG excerpts from the synthetic smoke-test chart."""
from base64 import b64encode
from datetime import date
import json
import unittest

from reporting import (
    DraftEvidence, EvidenceReference, FindingStatus, Readiness, Requirement, build_report, verify_support,
)
from run_agent import resource_view


ECG_ID = "a34bdaf5-154e-4876-9a62-e5af64efa6aa"
OTHER_REPORT_ID = "a34bcdbe-8343-40d9-b125-f7a6a64ecf33"
REVIEW_DATE = date(2026, 11, 25)
# The decoded text of the synthetic 2026-11-23 ECG DiagnosticReport.
ECG_NOTE = (
    '12-LEAD ECG REPORT\r\n'
    '\r\n'
    'Date performed: 2026-11-23\r\n'
    'Indication: Intermittent palpitations reported during primary care consultation on 2026-11-20.\r\n'
    '\r\n'
    'Test performed: Resting 12-lead electrocardiogram.\r\n'
    '\r\n'
    'Findings:\r\n'
    '- Normal sinus rhythm.\r\n'
    '- Heart rate: 78 bpm.\r\n'
    '- No acute ST-T abnormalities reported.\r\n'
    '\r\n'
    'Interpretation: No acute abnormalities reported on resting ECG.\r\n'
    'Report status: Final.\r\n'
    '\r\n'
    'Synthetic test data for referral documentation evaluation.'
)


class ECGEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = {
            ("DiagnosticReport", ECG_ID): {"effectiveDateTime": "2026-11-23", "notes": [ECG_NOTE]},
            ("DiagnosticReport", OTHER_REPORT_ID): {
                "effectiveDateTime": "2026-11-20",
                "notes": ["Physical examination: Heart rhythm regular on auscultation."],
            },
        }

    def verify(self, quote: str, resource_id: str = ECG_ID) -> EvidenceReference:
        item = DraftEvidence(component="ecg", resource_type="DiagnosticReport",
                             resource_id=resource_id, quote=quote)
        return verify_support(item, self.resources, REVIEW_DATE)

    def test_exact_excerpt_and_omitted_leading_bullet(self) -> None:
        for quote in ("- Normal sinus rhythm.", "Normal sinus rhythm."):
            with self.subTest(quote=quote):
                self.assertEqual(self.verify(quote).excerpts, [quote])

    def test_whitespace_and_line_breaks_are_normalized(self) -> None:
        quote = "  Findings:\n-\tNormal sinus rhythm.\n -  Heart rate: 78 bpm.  "
        self.assertEqual(self.verify(quote).excerpts,
                         ["Findings: - Normal sinus rhythm. - Heart rate: 78 bpm."])

    def test_case_or_nonbreaking_hyphen_is_not_normalized(self) -> None:
        for quote in ("12-lead ECG report", "12‑LEAD ECG REPORT"):
            with self.subTest(quote=quote):
                with self.assertRaisesRegex(ValueError, "excerpt does not occur"):
                    self.verify(quote)

    def test_paraphrase_and_invented_claim_are_rejected(self) -> None:
        for quote in ("Resting 12-lead ECG was performed.", "Heart rate: 98 bpm.",
                      "Acute ST-T abnormalities reported."):
            with self.subTest(quote=quote):
                with self.assertRaisesRegex(ValueError, "excerpt does not occur"):
                    self.verify(quote)

    def test_quote_is_checked_only_against_the_cited_report(self) -> None:
        with self.assertRaisesRegex(ValueError, "excerpt does not occur"):
            self.verify("Normal sinus rhythm.", OTHER_REPORT_ID)
        with self.assertRaisesRegex(ValueError, "excerpt does not occur"):
            self.verify("Heart rhythm regular on auscultation.")

    def test_base64_text_plain_attachment_is_decoded_before_validation(self) -> None:
        raw = {
            "resourceType": "DiagnosticReport", "id": ECG_ID, "effectiveDateTime": "2026-11-23",
            "presentedForm": [{"contentType": "text/plain",
                               "data": b64encode(ECG_NOTE.encode("utf-8")).decode("ascii")}],
        }
        decoded = resource_view(raw)
        self.assertEqual(decoded["notes"], [ECG_NOTE])
        self.resources["DiagnosticReport", ECG_ID] = decoded
        self.assertEqual(self.verify("Test performed: Resting 12-lead electrocardiogram.").resource_id, ECG_ID)


    def test_nonmatching_excerpt_reproduces_the_recorded_failure_path(self) -> None:
        # The smoke trace retained the error, but discarded the rejected draft.
        # Use a deliberate paraphrase of the captured source to exercise that path.
        findings = [{"requirement_id": key.value, "status": "MISSING", "evidence": []}
                    for key in Requirement]
        ecg = next(finding for finding in findings if finding["requirement_id"] == "ecg")
        ecg.update(status="PRESENT", evidence=[{
            "component": "ecg", "resource_type": "DiagnosticReport", "resource_id": ECG_ID,
            "quote": "Test performed: Resting 12-lead ECG.",
        }])
        report, errors = build_report(
            json.dumps({"findings": findings}), "synthetic", REVIEW_DATE, self.resources,
            {"DiagnosticReport", "Observation"}, [],
        )
        finding = next(finding for finding in report.findings if finding.requirement_id is Requirement.ECG)
        self.assertEqual(errors, ["ecg: A cited excerpt does not occur in the retrieved report."])
        self.assertEqual(finding.status, FindingStatus.UNKNOWN)
        self.assertEqual(finding.evidence, [])
        self.assertEqual(report.readiness, Readiness.UNABLE_TO_ASSESS)
        self.assertEqual(report.retrieval_errors, [])


if __name__ == "__main__":
    unittest.main()
