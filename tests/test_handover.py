"""Exercise transfer fencing through the public Service API on two PCs."""
import copy

import pytest

from ai_system.service import Service
from ai_system.store import Conflict
from test_collaboration import Client, Remote


class TransferClient(Client):
    def user_by_login(self, login):
        return {"id": {"person1": 1, "person2": 2, "leader": 99}[login], "type": "User", "login": login}

    def transfer_owner(self, project, task, owner):
        with self.r.lock:
            self.check_owner(project, task)
            self.r.refs[task["id"]] = copy.deepcopy(owner)
        return task["branch"], "work-head", owner

    def publish_snapshot(self, project, snapshot, expected_sha=None):
        if getattr(self.r, "fail_publish", False):
            self.r.fail_publish = False
            raise ValueError("simulated offline publish")
        return super().publish_snapshot(project, snapshot, expected_sha)


def pair(tmp_path):
    remote = Remote()
    a = Service(tmp_path / "a", github_factory=lambda: TransferClient(remote, 1))
    b = Service(tmp_path / "b", github_factory=lambda: TransferClient(remote, 2))
    project = a.create_project({"name": "Transfer project", "intent": "Test ownership transfers", "leader_login": "leader"})
    a.sync(project["id"])
    b.join(project["repo"], project["id"])
    task = a.claim(project["tasks"][0]["id"])
    a.sync(project["id"])
    b.sync(project["id"])
    return remote, a, b, project, task


def test_handover_publishes_exact_foreign_owner_then_target_continues(tmp_path):
    remote, a, b, project, before = pair(tmp_path)
    tid = before["id"]
    result = a.handover(tid, "person2", b.settings()["device_id"])
    assert result["id"] == project["id"]
    assert remote.refs[tid]["user_id"] == 2
    assert a.store.get("tasks", tid)["owner"]["device_id"] == b.settings()["device_id"]
    assert a.store.get("transfer_grants", tid)["active"] is False
    b.sync(project["id"])
    assert b.claim(tid)["owner"] == remote.refs[tid]
    b.store.change("tasks", tid, state="running")
    b.sync(project["id"])
    a.sync(project["id"])
    assert a.store.get("tasks", tid)["state"] == "running"
    with pytest.raises(Conflict):
        a.claim(tid)


def test_target_unsynced_work_conflicts_without_losing_local_or_transfer(tmp_path):
    remote, a, b, project, before = pair(tmp_path)
    tid = before["id"]
    # A collaborator's local edits must not be overwritten by an incoming transfer.
    b.store.change("tasks", tid, note="unsynced target work")
    a.handover(tid, "person2", b.settings()["device_id"])
    with pytest.raises(Conflict):
        b.sync(project["id"])
    assert b.store.get("tasks", tid)["note"] == "unsynced target work"
    published = remote.snapshots[project["id"]][0]["records"]["tasks:" + tid]
    assert published["owner"]["device_id"] == b.settings()["device_id"]
    assert "note" not in published


def test_old_owner_cannot_restore_stale_task_record_after_transfer(tmp_path):
    remote, a, b, project, before = pair(tmp_path)
    tid = before["id"]
    a.handover(tid, "person2", b.settings()["device_id"])
    # Model an old local run finishing after remote transfer was observed. The
    # local record claims this PC, but the authority ref belongs to the new PC.
    a.store.change("tasks", tid, owner=before["owner"], state="running", note="preserve stale local work")
    with pytest.raises(Conflict):
        a.sync(project["id"])
    assert a.store.get("tasks", tid)["note"] == "preserve stale local work"
    published = remote.snapshots[project["id"]][0]["records"]["tasks:" + tid]
    assert published["owner"] == remote.refs[tid]
    assert published["state"] == "ready"


def test_transfer_grant_is_exact_and_survives_failed_sync_until_retry(tmp_path):
    remote, a, b, project, before = pair(tmp_path)
    tid = before["id"]
    remote.fail_publish = True
    with pytest.raises(ValueError, match="offline"):
        a.handover(tid, "person2", b.settings()["device_id"])
    grant = a.store.get("transfer_grants", tid)
    assert grant["active"] is True and remote.refs[tid]["user_id"] == 2
    a.store.change("tasks", tid, note="unauthorized edit after transfer")
    with pytest.raises(Conflict):
        a.sync(project["id"])
    assert a.store.get("transfer_grants", tid)["active"] is True
    current = a.store.get("tasks", tid)
    a.store.put("tasks", grant["record"], current["version"])
    a.sync(project["id"])
    assert a.store.get("transfer_grants", tid)["active"] is False
    b.sync(project["id"])
    assert b.store.get("tasks", tid)["owner"] == remote.refs[tid]
