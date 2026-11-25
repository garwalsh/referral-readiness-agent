"""Offline coverage for the member OAuth token and FHIR action adapter."""
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from run_agent import PatientTools, select_transport
from stackone import access_token, connect_stackone, normalize_response, save_tokens


PATIENT = "a34c2416-8263-4819-9acf-690e2e6fdd01"


def result(resource: dict) -> dict:
    return {"structuredContent": {"isError": False, "result": {"data": resource}, "actionRunId": "test-id",
                                   "defenderMetadata": {"applied": True, "result": {"riskLevel": "low"}}}}


class StackOneTests(unittest.TestCase):
    def test_select_transport_does_not_silently_fall_back(self):
        with patch("run_agent.connect_langcare") as direct, patch("stackone.connect_stackone") as gateway:
            select_transport({"LANGCARE_MCP_URL": "https://example.test", "LANGCARE_MCP_TOKEN": "demo"}, StringIO())
            direct.assert_called_once()
            gateway.side_effect = ValueError("OAuth required")
            with self.assertRaisesRegex(ValueError, "OAuth required"):
                select_transport({"RRR_TRANSPORT": "stackone", "STACKONE_ACCOUNT_ID": "test"}, StringIO())
            direct.assert_called_once()

    def test_token_refresh_and_rotation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            save_tokens(path, {"client_id": "test", "access_token": "old", "refresh_token": "first",
                               "expires_at": time() - 1})
            response = {"access_token": "new", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "next"}
            with patch("stackone.oauth_post", return_value=response) as refresh:
                self.assertEqual(access_token(path), "new")
                self.assertEqual(access_token(path), "new")
                refresh.assert_called_once()
                self.assertEqual(refresh.call_args.args[1]["refresh_token"], "first")
            self.assertEqual(json.loads(path.read_text())["refresh_token"], "next")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_or_rejected_oauth_grant(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "authorize StackOne again"):
                access_token(Path(directory) / "missing")
        with patch("stackone.access_token", return_value="mock"), patch(
            "stackone.rpc", side_effect=HTTPError("", 401, "Unauthorized", None, None)
        ) as rpc:
            with self.assertRaisesRegex(ValueError, "rejected the OAuth grant"):
                connect_stackone("account", lambda event: None)
            rpc.assert_called_once()

    def test_search_is_patient_scoped_and_read_uses_the_same_index(self):
        record = {"resourceType": "DiagnosticReport", "id": "note", "subject": {"reference": f"Patient/{PATIENT}"}}
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": record}]}
        other = {"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": {**record, "subject": {"reference": "Patient/other"}}}
        ]}
        with patch("stackone.access_token", return_value="mock"), patch("stackone.rpc", side_effect=[
            {"protocolVersion": "2025-06-18"}, None, result(bundle), result(record), result(other),
        ]) as rpc:
            audit = []
            adapter = connect_stackone("account", audit.append)
            tools = PatientTools(PATIENT, adapter)
            search = {"resourceType": "DiagnosticReport"}
            args = tools.parameters("fhir_search", search)
            with self.assertRaises(ValueError):
                tools.parameters("fhir_read", {**search, "id": "note"})
            with self.assertRaises(ValueError):
                adapter("fhir_update", {})
            tools.retrieve("fhir_search", args)
            read = tools.parameters("fhir_read", {**search, "id": "note"})
            tools.retrieve("fhir_read", read)
            with self.assertRaisesRegex(ValueError, "outside the selected patient's chart"):
                tools.retrieve("fhir_search", args)
            search_call, read_call = [call.args[4] for call in rpc.call_args_list[2:4]]
            self.assertEqual(search_call["name"], "stackone_execute_action")
            self.assertEqual(search_call["arguments"], {"action_id": "mcp_rrrlangcareserver_fhir_search",
                "stackone_account_id": "account",
                "body": {"resourceType": "DiagnosticReport", "queryParams": f"patient={PATIENT}&_count=25"}})
            self.assertEqual(read_call["arguments"]["action_id"], "mcp_rrrlangcareserver_fhir_read")
            self.assertEqual(read_call["arguments"]["body"], read)
            self.assertTrue(audit[0]["defender_applied"])

    def test_normalization_matches_the_verified_envelope(self):
        bundle = {"resourceType": "Bundle", "entry": []}
        response = result(bundle)
        self.assertEqual(normalize_response(response)[0], bundle)
        self.assertEqual(normalize_response({"content": [
            {"type": "text", "text": json.dumps(response["structuredContent"])}
        ]})[0], bundle)
        with self.assertRaisesRegex(ValueError, "invalid action result"):
            normalize_response({"structuredContent": {"data": bundle}})
        response["structuredContent"]["defenderMetadata"]["result"]["allowed"] = False
        with self.assertRaisesRegex(ValueError, "Defender blocked"):
            normalize_response(response)


if __name__ == "__main__":
    unittest.main()
