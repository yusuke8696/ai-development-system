import json
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_system.diagnostics import diagnose_github
from ai_system.github import GitHub
from ai_system.server import create_app
from ai_system.service import Service

SECRET = "github_pat_test_do_not_log"


def diagnostic(handler=None, **options):
    calls, clients = [], []
    def transport(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "api.github.com"
        if handler:
            response = handler(request)
            if response is not None:
                return response
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "tester", "ignored": SECRET})
        if request.url.path == "/repos/owner/repo":
            return httpx.Response(200, json={"full_name": "owner/repo", "default_branch": "main"})
        return httpx.Response(200, json={"object": {"type": "commit", "sha": "a" * 40}})
    def factory():
        client = GitHub(SECRET, transport=httpx.MockTransport(transport))
        clients.append(client)
        return client
    factory_fn = options.get("factory", factory)
    result = diagnose_github(options.get("repo", "owner/repo"), lambda token: factory_fn(),
                             options.get("configured", lambda: True), options.get("loader", lambda: SECRET))
    assert SECRET not in json.dumps(result, ensure_ascii=False)
    assert all(client.client.is_closed for client in clients)
    return result, calls


def test_complete_read_only_diagnosis_and_secret_isolation():
    result, calls = diagnostic(repo="https://github.com/owner/repo.git")
    assert result["ok"] and result["repository"] == "owner/repo"
    assert result["login"] == "tester"
    assert [s["state"] for s in result["steps"]] == ["passed"] * 6
    assert [r.url.path for r in calls] == ["/user", "/repos/owner/repo", "/repos/owner/repo/git/ref/heads/main"]


def test_unregistered_token_stops_before_network():
    result, calls = diagnostic(configured=lambda: False)
    assert result["failed_stage"] == "credentials" and not calls
    assert result["steps"][1]["code"] == "token_missing"
    assert result["steps"][2]["state"] == "not_run"


def test_invalid_repository_is_not_echoed():
    result, calls = diagnostic(repo="https://" + SECRET + "@github.com/owner/repo")
    assert result["failed_stage"] == "configuration" and not calls
    assert not result["repository"]


@pytest.mark.parametrize("target", ["configuration_read", "decrypt"])
def test_credentials_failure_is_sanitized(target):
    def fail():
        raise OSError("C:/private-path/" + SECRET)
    options = {"configured": fail} if target == "configuration_read" else {"loader": fail}
    result, calls = diagnostic(**options)
    assert result["steps"][1]["code"] == "credentials_unreadable" and not calls
    assert "private-path" not in json.dumps(result)


def test_client_setup_error_does_not_claim_the_token_is_rejected():
    def failed_factory():
        raise ValueError("Unknown scheme for proxy URL: https://user:" + SECRET + "@proxy")
    result, _ = diagnostic(factory=failed_factory)
    assert result["steps"][1]["state"] == "passed"
    assert result["steps"][2]["code"] == "client_configuration"


@pytest.mark.parametrize("error,code", [(FileNotFoundError(SECRET), "client_resource_missing"),
                                      (OSError(SECRET), "client_initialization")])
def test_http_client_initialization_failure_is_never_reported_as_decryption(error, code):
    def failed_factory():
        raise error
    result, calls = diagnostic(factory=failed_factory)
    assert not calls
    assert result["failed_stage"] == "client"
    assert result["steps"][1]["state"] == "passed"
    assert result["steps"][2]["code"] == code


@pytest.mark.parametrize("status,path,headers,expected", [
    (401, "/user", {}, "authentication"),
    (403, "/user", {}, "permission"),
    (403, "/user", {"x-github-sso": "required; url=" + SECRET}, "sso"),
    (403, "/user", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "bad"}, "rate_limit"),
    (429, "/user", {"retry-after": "120"}, "rate_limit"),
    (404, "/repos/owner/repo", {}, "repository_access"),
    (404, "/repos/owner/repo/git/ref/heads/main", {}, "branch_missing"),
    (409, "/repos/owner/repo/git/ref/heads/main", {}, "branch_unavailable"),
    (502, "/user", {}, "upstream"),
    (302, "/user", {"location": "https://example.org/" + SECRET}, "upstream"),
])
def test_http_error_reports_failed_stage_status_and_safe_request_id(status, path, headers, expected):
    def response(request):
        if request.url.path == path:
            return httpx.Response(status, headers=dict(headers, **{"x-github-request-id": "AB12:34CD:FF99"}),
                                  json={"message": "upstream echo " + SECRET})
    result, calls = diagnostic(response)
    failed = next(s for s in result["steps"] if s["state"] == "failed")
    assert failed["code"] == expected and failed["http_status"] == status
    assert failed["request_id"] == "AB12:34CD:FF99"
    assert calls[-1].url.path == path
    if status == 429:
        assert failed["retry_after_seconds"] == 120


@pytest.mark.parametrize("kind,expected", [("timeout", "timeout"), ("tls", "tls_certificate"),
    ("proxy", "proxy"), ("dns", "dns"), ("connection", "network"), ("header", "invalid_token")])
def test_transport_failures_have_specific_safe_guidance(kind, expected):
    def failure(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if kind == "proxy":
            raise httpx.ProxyError("https://user:" + SECRET + "@proxy.local", request=request)
        if kind == "header":
            raise httpx.LocalProtocolError(SECRET, request=request)
        cause = ssl.SSLCertVerificationError(SECRET) if kind == "tls" else socket.gaierror(SECRET)
        if kind in {"tls", "dns"}:
            raise httpx.ConnectError(SECRET, request=request) from cause
        raise httpx.ConnectError(SECRET, request=request)
    result, calls = diagnostic(failure)
    assert result["failed_stage"] == "authentication"
    assert result["steps"][3]["code"] == expected and len(calls) == 1


def test_invalid_json_response_and_token_like_request_id_not_exposed():
    result, _ = diagnostic(lambda request: httpx.Response(200, text=SECRET, headers={"x-github-request-id": SECRET}))
    assert result["steps"][3]["code"] == "invalid_response"
    assert "request_id" not in result["steps"][3]


def test_unexpected_exception_does_not_disclose_credentials():
    def failure(request):
        raise RuntimeError(SECRET)
    result, _ = diagnostic(failure)
    assert result["steps"][3]["code"] == "unexpected"


def test_api_returns_structured_diagnostics_with_csrf_auth(tmp_path):
    s = Service(tmp_path)
    app = create_app(tmp_path, bootstrap="test", service=s, polling=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/github/test", json={}).status_code == 401
        client.get("/launch/test")
        assert client.post("/api/github/test", json={}).status_code == 403
        client.headers["x-csrf-token"] = client.get("/api/state").json()["csrf"]
        result = client.post("/api/github/test", json={})
        assert result.status_code == 200
        assert result.json()["failed_stage"] == "credentials"
        assert result.json()["steps"][1]["code"] == "token_missing"


def test_cli_never_creates_missing_settings(tmp_path):
    root = tmp_path / "not-created"
    script = Path(__file__).resolve().parents[1] / "scripts/github_diagnose.py"
    result = subprocess.run([sys.executable, str(script), "--data-dir", str(root)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and not result.stderr
    assert json.loads(result.stdout)["steps"][0]["code"] == "settings_unreadable"
    assert not root.exists()


def test_cli_reads_existing_settings_without_changing_them(tmp_path):
    service = Service(tmp_path)
    before = (tmp_path / "workspace.db").read_bytes()
    script = Path(__file__).resolve().parents[1] / "scripts/github_diagnose.py"
    result = subprocess.run([sys.executable, str(script), "--data-dir", str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and not result.stderr
    assert json.loads(result.stdout)["steps"][1]["code"] == "token_missing"
    assert (tmp_path / "workspace.db").read_bytes() == before
    assert service.store.get("settings", "local")["version"] == 1
