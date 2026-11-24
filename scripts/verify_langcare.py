#!/usr/bin/env python3
"""Check direct MCP access to a synthetic chart without printing chart contents."""

import argparse
import json
import os
from itertools import count
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


class NoRedirect(HTTPRedirectHandler):
    """Keep the bearer token on the configured LangCare endpoint."""

    def redirect_request(self, *args, **kwargs):
        return None


def rpc(endpoint, headers, opener, method, params=None, request_id=None):
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if request_id is not None:
        payload["id"] = request_id

    request = Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    with opener.open(request, timeout=30) as response:
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        if request_id is None:
            return None
        if response.headers.get_content_type() == "text/event-stream":
            # Older Streamable HTTP servers can reply with a JSON-RPC message in SSE.
            for line in response:
                if line.startswith(b"data:"):
                    message = json.loads(line[5:])
                    if message.get("id") == request_id:
                        break
            else:
                raise ValueError(f"{method} returned no matching response.")
        else:
            message = json.load(response)
    if "error" in message:
        raise ValueError(f"{method} returned JSON-RPC error {message['error'].get('code')}.")
    return message["result"]


def tool_result(result):
    """Read the MCP structured result, or its JSON text representation."""
    if result.get("isError"):
        raise ValueError("LangCare reported a tool execution error.")
    if "structuredContent" in result:
        return result["structuredContent"]
    texts = [part["text"] for part in result.get("content", []) if part["type"] == "text"]
    if len(texts) != 1:
        raise ValueError("Expected a single JSON text result.")
    return json.loads(texts[0])


def coding_codes(resource):
    return {item["code"] for item in resource.get("code", {}).get("coding", []) if "code" in item}


def check_chart(endpoint, token, patient_id):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    opener = build_opener(NoRedirect())
    ids = count(1)

    def call(method, params=None):
        return rpc(endpoint, headers, opener, method, params, next(ids))

    initialized = call("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "rrr-langcare-check", "version": "0.1"},
    })
    headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
    rpc(endpoint, headers, opener, "notifications/initialized")

    names = set()
    params = {}
    while True:
        page = call("tools/list", params)
        for tool in page["tools"]:
            names.add(tool["name"])
            if tool["name"] in {"fhir_read", "fhir_search"}:
                print(f'{tool["name"]} input schema: {json.dumps(tool["inputSchema"])}', flush=True)
        if not page.get("nextCursor"):
            break
        params = {"cursor": page["nextCursor"]}
    print(f"Advertised tools: {', '.join(sorted(names))}", flush=True)
    if not {"fhir_read", "fhir_search"} <= names:
        raise ValueError("LangCare did not advertise both read tools.")

    def fhir_call(name, arguments):
        return tool_result(call("tools/call", {"name": name, "arguments": arguments}))

    patient = fhir_call("fhir_read", {"resourceType": "Patient", "id": patient_id})
    if patient.get("resourceType") != "Patient" or patient.get("id") != patient_id:
        raise ValueError("The returned Patient did not match the requested ID.")
    print(f"Patient/{patient_id}: retrieved", flush=True)

    for kind in ("DiagnosticReport", "Observation"):
        bundle = fhir_call("fhir_search", {
            "resourceType": kind,
            "queryParams": urlencode({"patient": patient_id}),
        })
        if bundle.get("resourceType") != "Bundle":
            raise ValueError(f"{kind} search did not return a FHIR Bundle.")
        resources = [entry["resource"] for entry in bundle.get("entry", [])
                     if entry.get("resource", {}).get("resourceType") == kind]
        if not resources or any(
            not resource.get("subject", {}).get("reference", "").endswith(f"Patient/{patient_id}")
            for resource in resources
        ):
            raise ValueError(f"{kind} search returned no matching patient evidence.")
        codes = set().union(*(coding_codes(resource) for resource in resources))
        print(f"{kind}: {len(resources)} matches on this page; codes: {', '.join(sorted(codes))}", flush=True)
        if kind == "DiagnosticReport":
            attached = sum(any(
                a.get("contentType") == "text/plain" and a.get("data")
                for a in resource.get("presentedForm", [])
            ) for resource in resources)
            if not attached:
                raise ValueError("No clinical reports contained a text attachment.")
            print(f"Clinical reports with text attachments: {attached}", flush=True)
        else:
            if not {"85354-9", "8867-4"} <= codes:
                raise ValueError("Expected both blood pressure and heart rate observations.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patient_id", type=UUID, help="FHIR ID of a synthetic patient")
    args = parser.parse_args()
    endpoint = os.getenv("LANGCARE_MCP_URL", "")
    token = os.getenv("LANGCARE_MCP_TOKEN")
    url = urlsplit(endpoint)
    if url.scheme != "https" or not url.hostname or url.username or url.password or not token:
        parser.error("Set LANGCARE_MCP_URL to the HTTPS endpoint and supply LANGCARE_MCP_TOKEN.")
    try:
        check_chart(endpoint, token, str(args.patient_id))
    except HTTPError as error:
        parser.exit(1, f"LangCare returned HTTP {error.code}.\n")
    except URLError as error:
        parser.exit(1, f"LangCare transport error ({type(error.reason).__name__}).\n")
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
