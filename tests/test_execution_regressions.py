import json
import sys
import threading
import pytest

from ai_system.service import Service
from ai_system.store import Conflict
from test_core import new_project, wait
from ai_system.tools import write_files


def test_materialized_bytes_match_artifact_on_windows(tmp_path):
    write_files(tmp_path, [{"path": "a.py", "content": "one\ntwo\n"}])
    assert (tmp_path / "a.py").read_bytes() == b"one\ntwo\n"


def implementation(service, project, content="VALUE = 1\n"):
    task = next(t for t in service.tasks(project["id"]) if t["kind"] == "implementation")
    artifact = service.store.put("artifacts", {"project_id": project["id"], "task_id": task["id"],
        "files": [{"path": "calc.py", "content": content}], "body": content, "hash": content, "errors": []})
    return service.store.change("tasks", task["id"], artifacts=[artifact["id"]], approved_artifact=artifact["id"], state="succeeded")


def verification(service, project):
    task = next(t for t in service.tasks(project["id"]) if t["kind"] == "verification")
    artifact = service.store.put("artifacts", {"project_id": project["id"], "task_id": task["id"], "errors": []})
    return service.store.change("tasks", task["id"], artifacts=[artifact["id"]], state="waiting_approval")


def test_verification_tool_reads_dependency_files_on_second_pc(tmp_path):
    from test_collaboration import Client, Remote
    remote = Remote()
    s = Service(tmp_path / "a", github_factory=lambda: Client(remote, 1))
    p = new_project(s)
    implementation(s, p)
    t = verification(s, p)
    s.sync(p["id"])
    second = Service(tmp_path / "b", github_factory=lambda: Client(remote, 2))
    second.join(p["repo"], p["id"])
    t = second.task(t["id"])
    root = second.input_workdir(t)
    assert (root / "calc.py").read_text() == "VALUE = 1\n"
    assert not (second.workdir(t) / "calc.py").exists()


def test_latest_failure_cannot_reuse_old_success(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    implementation(s, p)
    t = verification(s, p)
    basis = s.verification_basis(t)
    for code, finished in [(0, "2026-01-01"), (1, "2026-01-02")]:
        s.store.put("test_runs", {"project_id": p["id"], "task_id": t["id"], "tool_id": "unit",
            "input_hash": basis["hash"], "exit_code": code, "finished_at": finished})
    with pytest.raises(ValueError, match="テストコマンド"):
        s.approve(t["id"], {"approve": True, "expected_version": t["version"]})


def test_evidence_is_bound_to_source_and_shared(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    implementation(s, p)
    t = verification(s, p)
    s.store.change("settings", "local", python=sys.executable, allow_commands=True)
    tool = s.store.put("tools", {"transport": "command", "argv": ["{python}", "-c", "from calc import VALUE; assert VALUE == 1"]})
    result = s.test_command(t["id"], tool["id"])
    assert result["exit_code"] == 0
    assert result["input_hash"] == s.verification_basis(t)["hash"]
    assert "test_runs:" + result["id"] in s.snapshot(p["id"])["records"]
    implementation(s, p, "VALUE = 2\n")
    with pytest.raises(ValueError, match="テストコマンド"):
        s.approve(t["id"], {"approve": True, "expected_version": t["version"]})


def test_cancellation_during_format_repair_persists_no_artifact(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class LLM:
        calls = 0
        def complete(self, *args):
            self.calls += 1
            if self.calls == 2:
                entered.set()
                assert release.wait(10)
            return json.dumps({"markdown": "# invalid"})
        def close(self):
            pass
    s = Service(tmp_path, llm_factory=lambda _: LLM())
    p = new_project(s)
    tid = p["tasks"][0]["id"]
    s.start(tid, {})
    assert entered.wait(10)
    s.cancelled.add(tid)
    release.set()
    assert wait(s, tid)["state"] == "cancelled"
    assert s.store.list("artifacts") == []
    assert list(s.workdir(s.task(tid)).iterdir()) == []


def test_invalid_imported_workdir_id_rejected(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    with pytest.raises(ValueError, match="ID"):
        s.workdir({"project_id": p["id"], "id": "../elsewhere"})
