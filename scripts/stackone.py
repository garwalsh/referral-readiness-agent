"""Member OAuth and the two allowed LangCare actions through StackOne Gateway."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from itertools import count
import json
import os
from pathlib import Path
import re
import secrets
from tempfile import NamedTemporaryFile
from time import monotonic, time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener
from uuid import UUID
import webbrowser

from verify_langcare import NoRedirect, rpc

ENDPOINT = "https://mcp.stackone.com/mcp"
ISSUER = "https://idp-api.stackone.com/api/auth"
OAUTH = f"{ISSUER}/oauth2"
SCOPE = "mcp offline_access"
LOGIN = "Run python3 scripts/stackone.py login to authorize StackOne again."
ACTIONS = {
    "fhir_search": "mcp_rrrlangcareserver_fhir_search",
    "fhir_read": "mcp_rrrlangcareserver_fhir_read",
}


def oauth_post(operation: str, fields: dict, *, registration: bool = False) -> dict:
    payload = json.dumps(fields) if registration else urlencode(fields)
    content_type = "application/json" if registration else "application/x-www-form-urlencoded"
    request = Request(f"{OAUTH}/{operation}", data=payload.encode(), headers={"Content-Type": content_type})
    try:
        with build_opener(NoRedirect()).open(request, timeout=25) as response:
            result = json.load(response)
    except HTTPError as error:
        raise ValueError(f"StackOne OAuth {operation} returned HTTP {error.code}. {LOGIN}") from None
    except (OSError, ValueError):
        raise ValueError(f"StackOne OAuth {operation} did not complete.") from None
    if not isinstance(result, dict):
        raise ValueError("StackOne returned an invalid OAuth response.")
    return result


def private_path() -> Path:
    return Path.home() / ".config" / "rrr" / "stackone-oauth.json"


@contextmanager
def locked_store(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o077 or path.parent.is_symlink():
        raise ValueError("StackOne credential directory must be owned by you with mode 0700.")
    lock = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, "rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def save_tokens(path: Path, tokens: dict) -> None:
    temp = None
    try:
        with NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as file:
            temp = Path(file.name)
            json.dump(tokens, file)
        temp.replace(path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def credential_record(client_id: str, response: dict, old_refresh: str | None = None) -> dict:
    access = response.get("access_token")
    remaining = response.get("expires_in")
    refresh = response.get("refresh_token", old_refresh)
    if (not isinstance(access, str) or not access or response.get("token_type", "").lower() != "bearer"
            or type(remaining) not in (int, float) or not 0 < remaining <= 86400
            or not isinstance(refresh, str) or not refresh):
        raise ValueError("StackOne did not return usable bearer and refresh credentials.")
    return {"client_id": client_id, "access_token": access, "refresh_token": refresh,
            "expires_at": time() + remaining}


def access_token(path: Path) -> str:
    with locked_store(path):
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as file:
                info = os.fstat(file.fileno())
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("Credentials are not private.")
                record = json.load(file)
            client, access, refresh = (record[key] for key in ("client_id", "access_token", "refresh_token"))
            if not all(isinstance(value, str) and value for value in (client, access, refresh)):
                raise ValueError("Incomplete credentials.")
            expires = float(record["expires_at"])
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError(f"StackOne OAuth credentials are missing or unreadable. {LOGIN}") from None
        if expires > time() + 45:
            return access
        response = oauth_post("token", {"grant_type": "refresh_token", "client_id": client,
                                        "refresh_token": refresh, "resource": ENDPOINT})
        record = credential_record(client, response, refresh)
        save_tokens(path, record)
        return record["access_token"]


def login(path: Path) -> None:
    """Use an ephemeral loopback callback. The browser handles all account sign-in."""
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    outcome: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass  # A callback URL contains the authorization code.

        def do_GET(self) -> None:
            target = urlsplit(self.path)
            params = parse_qs(target.query)
            valid = (target.path == "/callback" and len(params.get("state", [])) == 1
                     and secrets.compare_digest(params["state"][0].encode(), state.encode())
                     and params.get("iss", [ISSUER]) == [ISSUER])
            code = params.get("code", [])
            if valid and len(code) == 1 and not params.get("error"):
                outcome["code"] = code[0]
            elif valid:
                outcome["error"] = "StackOne authorization was declined or failed."
            message = (
                b"StackOne authorized. You can close this tab." if "code" in outcome
                else b"StackOne authorization did not complete."
            )
            self.send_response(200 if "code" in outcome else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        registration = oauth_post("register", {
            "client_name": "RRR Python Agent", "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "scope": SCOPE,
        }, registration=True)
        client_id = registration.get("client_id")
        if not isinstance(client_id, str) or not client_id or registration.get("token_endpoint_auth_method", "none") != "none":
            raise ValueError("StackOne did not register the expected public OAuth client.")
        query = urlencode({"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                           "resource": ENDPOINT, "scope": SCOPE, "state": state,
                           "code_challenge_method": "S256", "code_challenge": challenge})
        print("Complete sign-in in your browser. Select the existing LangCare account and only FHIR Search and Read.",
              flush=True)
        if not webbrowser.open(f"{OAUTH}/authorize?{query}"):
            raise ValueError("The browser could not be opened. Run login from your desktop session.")
        deadline = monotonic() + 300
        server.timeout = 1
        while not outcome and monotonic() < deadline:
            server.handle_request()
    if "code" not in outcome:
        raise ValueError(outcome.get("error", "StackOne authorization timed out."))
    response = oauth_post("token", {"grant_type": "authorization_code", "client_id": client_id,
                                    "code": outcome["code"], "redirect_uri": redirect,
                                    "code_verifier": verifier, "resource": ENDPOINT})
    record = credential_record(client_id, response)
    with locked_store(path):
        save_tokens(path, record)


def normalize_response(response: dict) -> tuple[dict, dict]:
    """Extract FHIR only from the verified StackOne action envelope."""
    if response.get("isError"):
        raise ValueError("StackOne reported an MCP tool error.")
    structured = response.get("structuredContent")
    try:
        if structured is None:
            content = response["content"]
            if len(content) != 1 or content[0]["type"] != "text":
                raise ValueError
            structured = json.loads(content[0]["text"])
        if structured.get("isError") is not False:
            raise ValueError
        resource = structured["result"]["data"]
        if not isinstance(resource, dict):
            raise ValueError
    except (ValueError, KeyError, AttributeError, TypeError, IndexError):
        raise ValueError("StackOne returned a failed or invalid action result.") from None

    audit = {}
    run_id = structured.get("actionRunId")
    if isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,80}", run_id):
        audit["action_run_id"] = run_id
    metadata = structured.get("defenderMetadata")
    if isinstance(metadata, dict):
        audit["defender_applied"] = metadata.get("applied") is True
        verdict = metadata.get("result")
        if isinstance(verdict, dict):
            if verdict.get("riskLevel") in ("low", "medium", "high", "critical"):
                audit["defender_risk"] = verdict["riskLevel"]
            if verdict.get("allowed") is False:
                raise ValueError("StackOne Defender blocked the action result.")
    return resource, audit


def connect_stackone(account: str, on_action: Callable[[dict], None], path: Path | None = None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", account):
        raise ValueError("Set STACKONE_ACCOUNT_ID to the existing LangCare linked account ID.")
    path = path if path is not None else private_path()
    opener = build_opener(NoRedirect())
    ids = count(1)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def send(method, params=None):
        headers["Authorization"] = f"Bearer {access_token(path)}"
        try:
            return rpc(ENDPOINT, headers, opener, method, params,
                       None if method == "notifications/initialized" else next(ids))
        except HTTPError as error:
            if error.code == 401:
                raise ValueError(f"StackOne Gateway rejected the OAuth grant. {LOGIN}") from None
            raise ValueError(f"StackOne Gateway returned HTTP {error.code}.") from None

    initialized = send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "RRR Python Agent", "version": "1"}})
    headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
    send("notifications/initialized")

    def call(name: str, arguments: dict) -> dict:
        if name not in ACTIONS:
            raise ValueError("Tool is outside the StackOne read-only allowlist.")
        result = send("tools/call", {"name": "stackone_execute_action", "arguments": {
            "action_id": ACTIONS[name], "stackone_account_id": account, "body": arguments,
        }})
        resource, audit = normalize_response(result)
        on_action({"action": ACTIONS[name], **audit})
        return resource

    return call


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("login")
    subcommands.add_parser("probe").add_argument("patient_id", type=UUID)
    args = parser.parse_args()
    try:
        if args.command == "login":
            login(private_path())
            print("StackOne member grant saved privately.")
        else:
            from run_agent import PatientTools, settings
            audit = []
            tools = PatientTools(str(args.patient_id), connect_stackone(settings().get("STACKONE_ACCOUNT_ID", ""),
                                                                        audit.append))
            name = "fhir_search"
            params = tools.parameters(name, {"resourceType": "DiagnosticReport"})
            _, result = tools.retrieve(name, params)
            print(json.dumps({"transport": "stackone", "count": len(result["resources"]),
                              "patient_reference_verified": True, "actions": audit}, indent=2))
    except (ValueError, OSError, URLError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if type(error) is ValueError else type(error).__name__
        parser.exit(1, f"{message}\n")


if __name__ == "__main__":
    main()
