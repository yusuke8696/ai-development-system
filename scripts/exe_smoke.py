import json
import os
import subprocess
import sys
import time
from pathlib import Path
import httpx

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".exe-user-1").resolve()
root.mkdir(parents=True, exist_ok=True)
exe = Path("dist/AIWorkspace/AIWorkspace.exe").resolve()
launch = root / "launch.json"
report = {"exe": exe.name}
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
        client.post(base + "/api/shutdown", json={})
    process.wait(timeout=10)
    report["restart_persistence"] = "passed"
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=True))
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
