import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_system.service import Service
from ai_system.store import Conflict


def setup_verification(tmp_path):
    service = Service(tmp_path)
    project = service.create_project({"name": "test evidence", "intent": "approval must await tests"})
    implementation = next(t for t in project["tasks"] if t["kind"] == "implementation")
    source = service.store.put("artifacts", {"project_id": project["id"], "task_id": implementation["id"],
        "files": [{"path": "calc.py", "content": "VALUE = 1\n"}], "errors": []})
    service.store.change("tasks", implementation["id"], artifacts=[source["id"]], approved_artifact=source["id"],
                         state="succeeded")
    task = next(t for t in project["tasks"] if t["kind"] == "verification")
    report = service.store.put("artifacts", {"project_id": project["id"], "task_id": task["id"], "errors": []})
    task = service.store.change("tasks", task["id"], state="waiting_approval", artifacts=[report["id"]])
    service.store.change("settings", "local", allow_commands=True, python=sys.executable)
    tool = service.store.put("tools", {"transport": "command", "argv": ["{python}", "-c", "pass"]})
    return service, project, task, tool


def evidence(service, task, tool, **changes):
    return service.store.put("test_runs", dict({"project_id": task["project_id"], "task_id": task["id"],
        "tool_id": tool["id"], "device_id": service.settings()["device_id"], "state": "succeeded", "exit_code": 0,
        "input_hash": service.verification_basis(task)["hash"], "stdout": "", "stderr": "",
        "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:01:00Z"}, **changes))


def test_in_flight_test_invalidates_approval_and_blocks_manual_override(tmp_path, monkeypatch):
    service, _, task, tool = setup_verification(tmp_path)
    evidence(service, task, tool)
    service.approve(task["id"], {"approve": True, "expected_version": task["version"]})
    entered, release = threading.Event(), threading.Event()

    def command(*args):
        entered.set()
        assert release.wait(10)
        return {"exit_code": 1, "stdout": "", "stderr": "a new test failed"}

    monkeypatch.setattr("ai_system.service.run_command", command)
    with ThreadPoolExecutor(1) as workers:
        future = workers.submit(service.test_command, task["id"], tool["id"])
        try:
            assert entered.wait(10)
            current = service.store.get("tasks", task["id"])
            assert current["state"] == "waiting_approval"
            assert current["verification_basis"] is None
            running = [r for r in service.store.list("test_runs") if r.get("state") == "running"]
            assert len(running) == 1 and running[0]["exit_code"] is None
            assert "test_runs:" + running[0]["id"] in service.snapshot(task["project_id"])["records"]
            for comment in ("", "manual verification recorded"):
                with pytest.raises(Conflict, match="実行中"):
                    service.approve(task["id"], {"approve": True, "expected_version": current["version"], "comment": comment})
        finally:
            release.set()
        assert future.result(timeout=10)["state"] == "failed"
    with pytest.raises(ValueError, match="テストコマンド"):
        current = service.store.get("tasks", task["id"])
        service.approve(task["id"], {"approve": True, "expected_version": current["version"]})


def test_last_started_run_is_used_instead_of_last_finished_run(tmp_path):
    service, _, task, tool = setup_verification(tmp_path)
    evidence(service, task, tool, exit_code=1, state="failed", started_at="2026-01-01", finished_at="2026-01-04")
    latest = evidence(service, task, tool, started_at="2026-01-02", finished_at="2026-01-03")
    approved = service.approve(task["id"], {"approve": True, "expected_version": task["version"]})
    assert approved["state"] == "succeeded"
    assert approved["verification_basis"]["tests"] == [latest["id"]]


def test_any_pending_run_blocks_approval_even_if_later_success_exists(tmp_path):
    service, _, task, tool = setup_verification(tmp_path)
    evidence(service, task, tool, exit_code=None, state="running", finished_at=None)
    evidence(service, task, tool, started_at="2026-01-02", finished_at="2026-01-03")
    with pytest.raises(Conflict, match="実行中"):
        service.approve(task["id"], {"approve": True, "expected_version": task["version"], "comment": "manual check"})


def test_restart_marks_only_local_pending_test_interrupted(tmp_path):
    service, _, task, tool = setup_verification(tmp_path)
    service.store.change("tasks", task["id"], state="succeeded", verification_basis={"hash": "old"})
    local = evidence(service, task, tool, exit_code=None, state="running", finished_at=None)
    foreign = evidence(service, task, tool, exit_code=None, state="running", finished_at=None, device_id="other-pc")

    restarted = Service(tmp_path)
    interrupted = restarted.store.get("test_runs", local["id"])
    assert interrupted["state"] == "interrupted"
    assert interrupted["exit_code"] == -1
    assert interrupted["finished_at"]
    assert restarted.store.get("test_runs", foreign["id"]) == foreign
    assert restarted.store.get("tasks", task["id"])["state"] == "waiting_approval"


def test_command_exception_persists_failure_instead_of_pending_record(tmp_path, monkeypatch):
    service, _, task, tool = setup_verification(tmp_path)

    def failed_command(*args):
        raise OSError("error containing a private local path")

    monkeypatch.setattr("ai_system.service.run_command", failed_command)
    result = service.test_command(task["id"], tool["id"])
    assert result["exit_code"] == -1 and result["state"] == "failed"
    assert result["started_at"] and result["finished_at"]
    assert "private local path" not in result["stderr"]
    assert not any(r["state"] == "running" for r in service.store.list("test_runs"))


def test_close_prevents_starting_new_test_command(tmp_path):
    service, _, task, tool = setup_verification(tmp_path)
    service.close()
    with pytest.raises(Conflict, match="終了処理"):
        service.test_command(task["id"], tool["id"])
    assert service.store.list("test_runs") == []
