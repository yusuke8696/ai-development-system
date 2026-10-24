import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_system.secrets import Secrets

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".exe-user-1").resolve()
root.mkdir(parents=True, exist_ok=True)
exe = Path("dist/AIWorkspace/AIWorkspace.exe").resolve()
launch = root / "launch.json"
report = {"exe": exe.name}
fixture = {"token": "test-only-llm-token", "authenticated": False}


class ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        fixture["authenticated"] = self.headers.get("Authorization") == "Bearer " + fixture["token"]
        body = json.dumps({"data": [{"id": "smoke-model"}]}).encode()
        self.send_response(200 if self.path == "/v1/models" and fixture["authenticated"] else 403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


models_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
threading.Thread(target=models_server.serve_forever, daemon=True).start()
process = subprocess.Popen([str(exe), "--no-browser", "--data-dir", str(root / "data"),
    "--launch-file", str(launch)], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
try:
    for _ in range(100):
        if launch.exists():
            break
        if process.poll() is not None:
            raise RuntimeError("exe terminated: " + str(process.returncode))
        time.sleep(0.2)
    info = json.loads(launch.read_text("utf-8"))
    with httpx.Client(follow_redirects=True, trust_env=False, timeout=10) as client:
        page = client.get(info["url"])
        assert page.status_code == 200 and "AI Development Workspace" in page.text
        base = "http://127.0.0.1:" + str(info["port"])
        state = client.get(base + "/api/state").json()
        client.headers["x-csrf-token"] = state["csrf"]
        assert len(state["workflows"]) == 3
        assert any(s["id"] == "ponytail" for s in state["skills"])
        diagnosis = client.post(base + "/api/github/test", json={})
        assert diagnosis.status_code == 200
        assert diagnosis.json()["failed_stage"] == "credentials"
        assert diagnosis.json()["steps"][1]["code"] == "token_missing"
        report["github_diagnostics"] = "passed"
        # A non-ASCII dummy credential reaches HTTP header validation after
        # decryption, without making any external GitHub request.
        dummy = "試験専用トークン"
        saved = client.post(base + "/api/settings", json={"expected_version": state["settings"]["version"], "github_token": dummy})
        assert saved.status_code == 200
        assert Secrets(root / "data").get("github") == dummy
        diagnosis = client.post(base + "/api/github/test", json={}).json()
        assert diagnosis["steps"][1]["state"] == "passed"
        assert diagnosis["failed_stage"] == "client"
        assert diagnosis["steps"][2]["code"] == "invalid_token"
        report["credential_roundtrip"] = "passed"
        # HTTPX initializes a TLS context even for this HTTP endpoint. This
        # catches missing frozen SSL support files while using localhost only.
        model = client.post(base + "/api/models", json={"name": "exe TLS fixture", "provider": "openai",
            "endpoint": f"http://127.0.0.1:{models_server.server_port}/v1", "model_id": "smoke-model",
            "roles": [], "secret": fixture["token"]})
        assert model.status_code == 200
        model_id = model.json()["id"]
        connected = client.post(base + "/api/models/" + model_id + "/test", json={})
        assert connected.status_code == 200, "Frozen HTTP client or credential initialization failed"
        assert "smoke-model" in connected.json()["models"] and fixture["authenticated"]
        report["frozen_http_client"] = "passed"
        response = client.post(base + "/api/projects", json={"name": "exe起動試験", "intent": "永続化の確認"})
        assert response.status_code == 200
        project_id = response.json()["id"]
        state = client.get(base + "/api/state").json()
        project = next(p for p in state["projects"] if p["id"] == project_id)
        assert not project["template_preparation"]["ready"]
        assert client.post(base + "/api/tasks/" + project["tasks"][0]["id"] + "/start", json={}).status_code == 409
        assert "開発準備・書式登録" in page.text
        report.update(startup="passed", project_creation="passed", tasks=len(response.json()["tasks"]), bundled_ponytail="passed")
        report["unapproved_template_gate"] = "passed"
        client.post(base + "/api/shutdown", json={})
    process.wait(timeout=10)
    report["shutdown"] = "passed"
    # Python and the frozen exe must read each other's DPAPI records, too.
    fixture["token"] = "test-only-token-after-restart"
    fixture["authenticated"] = False
    Secrets(root / "data").set("model:" + model_id, fixture["token"])
    launch2 = root / "restart.json"
    process = subprocess.Popen([str(exe), "--no-browser", "--data-dir", str(root / "data"),
        "--launch-file", str(launch2)], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    for _ in range(100):
        if launch2.exists():
            break
        time.sleep(0.2)
    info = json.loads(launch2.read_text("utf-8"))
    with httpx.Client(follow_redirects=True, trust_env=False, timeout=10) as client:
        client.get(info["url"])
        base = "http://127.0.0.1:" + str(info["port"])
        state = client.get(base + "/api/state").json()
        assert any(p["id"] == project_id for p in state["projects"])
        client.headers["x-csrf-token"] = state["csrf"]
        diagnosis = client.post(base + "/api/github/test", json={}).json()
        assert diagnosis["steps"][1]["state"] == "passed"
        assert diagnosis["steps"][2]["code"] == "invalid_token"
        assert client.post(base + "/api/models/" + model_id + "/test", json={}).status_code == 200
        assert fixture["authenticated"] and fixture["token"] not in json.dumps(state)
        report["credential_restart_persistence"] = "passed"
        client.post(base + "/api/shutdown", json={})
    process.wait(timeout=10)
    report["restart_persistence"] = "passed"
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=True))
finally:
    models_server.shutdown()
    models_server.server_close()
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
