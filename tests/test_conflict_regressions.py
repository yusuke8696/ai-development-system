"""Regression coverage for local PCs collaborating through GitHub records."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_system.service import Service
from ai_system.store import Conflict
from test_collaboration import Remote, make_pair


def test_restarting_replica_does_not_interrupt_other_device_run(tmp_path):
    remote, a, b, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    a.claim(tid)
    a.store.put("runs", {"id": "remote-run", "task_id": tid, "project_id": p["id"],
                         "state": "running", "device_id": a.settings()["device_id"]})
    a.store.change("tasks", tid, state="running", run_id="remote-run")
    a.sync(p["id"])
    b.sync(p["id"])
    original = b.store.get("tasks", tid)

    restarted = Service(tmp_path / "b", github_factory=lambda: remote.client(2))
    assert restarted.store.get("tasks", tid) == original
    assert restarted.store.get("tasks", tid)["state"] == "running"
    restarted.sync(p["id"])
    a.sync(p["id"])
    assert a.store.get("tasks", tid)["state"] == "running"


def test_restarting_owner_recovers_its_own_run(tmp_path):
    remote, a, _, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    a.claim(tid)
    a.store.put("runs", {"id": "local-run", "task_id": tid, "project_id": p["id"],
                         "state": "running", "device_id": a.settings()["device_id"]})
    a.store.change("tasks", tid, state="running", run_id="local-run")

    restarted = Service(tmp_path / "a", github_factory=lambda: remote.client(1))
    assert restarted.store.get("tasks", tid)["state"] == "blocked"
    assert restarted.store.get("runs", "local-run")["state"] != "running"


def prepare_shared_approval(remote, a, b, p):
    manifest = []
    for i, task in enumerate(p["tasks"][:-1], start=1):
        pr = {"number": i, "sha": f"artifact-{i}"}
        a.store.change("tasks", task["id"], state="succeeded", pr=pr)
        manifest.append({"task_id": task["id"], "pr": pr})
        remote.pull_requests[i] = {"number": i, "head": {"sha": pr["sha"]}}
    final_id = p["tasks"][-1]["id"]
    a.store.change("tasks", final_id, state="waiting_approval", pr={"number": 99, "sha": "final-head"},
                   review_manifest={"leader_id": 2, "artifacts": manifest})
    remote.pull_requests[99] = {"number": 99, "head": {"sha": "final-head"}, "user": {"id": 1},
                                "state": "open", "mergeable": True, "mergeable_state": "clean"}
    remote.reviews[99] = [{"id": 101, "user": {"id": 2, "type": "User"}, "state": "APPROVED",
                           "commit_id": "final-head", "submitted_at": "2026-10-24T00:00:00Z",
                           "html_url": "https://github.com/o/r/pull/99#review"}]
    a.sync(p["id"])
    b.sync(p["id"])
    return final_id


def test_both_devices_refresh_same_approval_without_shared_writes(tmp_path):
    remote, a, b, p = make_pair(tmp_path)
    final_id = prepare_shared_approval(remote, a, b, p)
    original_a = a.store.get("tasks", final_id)
    original_b = b.store.get("tasks", final_id)
    shared_before = copy.deepcopy(remote.snapshots[p["id"]][0])

    for _ in range(3):
        assert a.refresh_approval(final_id)["state"] == "succeeded"
        assert b.refresh_approval(final_id)["state"] == "succeeded"
        assert a.project(p["id"])["state"] == "completed"
        assert b.project(p["id"])["state"] == "completed"
        a.sync(p["id"])
        b.sync(p["id"])

    assert a.store.get("tasks", final_id) == original_a
    assert b.store.get("tasks", final_id) == original_b
    assert remote.snapshots[p["id"]][0] == shared_before
    assert all(not key.startswith("observations:") for key in a.snapshot(p["id"])["records"])


def test_foreign_owner_record_cannot_be_published_by_replica(tmp_path):
    remote, a, b, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    a.claim(tid)
    a.sync(p["id"])
    b.sync(p["id"])
    baseline = copy.deepcopy(remote.snapshots[p["id"]][0]["records"]["tasks:" + tid])
    b.store.change("tasks", tid, state="failed", error="accidental replica write")

    with pytest.raises(Conflict):
        b.sync(p["id"])
    assert remote.snapshots[p["id"]][0]["records"]["tasks:" + tid] == baseline
    assert b.store.get("tasks", tid)["state"] == "failed"


def test_adding_work_preserves_foreign_final_record_and_invalidates_approval(tmp_path):
    remote, a, b, p = make_pair(tmp_path)
    final_id = prepare_shared_approval(remote, a, b, p)
    a.claim(final_id)
    a.sync(p["id"])
    b.sync(p["id"])
    assert b.refresh_approval(final_id)["state"] == "succeeded"
    original = b.store.get("tasks", final_id)

    added = b.add_task(p["id"], {"title": "additional work", "role": "coding", "depends_on": []})
    assert b.store.get("tasks", final_id) == original
    assert b.project(p["id"])["state"] == "active"
    assert next(t for t in b.project(p["id"])["tasks"] if t["id"] == final_id)["state"] == "waiting_approval"
    b.sync(p["id"])
    a.sync(p["id"])
    assert a.store.get("tasks", added["id"])["title"] == "additional work"
    assert a.store.get("tasks", final_id)["depends_on"] == original["depends_on"]
    assert b.refresh_approval(final_id)["state"] == "waiting_approval"


def test_simultaneous_same_task_edits_do_not_overwrite_each_other(tmp_path):
    remote, a, b, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    a.store.change("tasks", tid, note="writer a")
    b.store.change("tasks", tid, note="writer b")
    remote.snapshot_barrier = threading.Barrier(2)

    def sync(service):
        try:
            service.sync(p["id"])
            return True
        except Conflict:
            return False

    with ThreadPoolExecutor(2) as workers:
        outcomes = list(workers.map(sync, [a, b]))
    remote.snapshot_barrier = None
    assert sum(outcomes) == 1
    winner = "writer a" if outcomes[0] else "writer b"
    assert remote.snapshots[p["id"]][0]["records"]["tasks:" + tid]["note"] == winner
    assert a.store.get("tasks", tid)["note"] == "writer a"
    assert b.store.get("tasks", tid)["note"] == "writer b"


def test_personal_and_other_project_templates_never_enter_snapshot(tmp_path):
    remote = Remote()
    service = Service(tmp_path, github_factory=lambda: remote.client(1))
    p = service.create_project({"name": "private templates", "intent": "privacy"})
    service.store.change("templates", "requirements", body="# private personal format")
    service.store.put("templates", {"id": "other-project-secret", "project_id": "another-project",
                                     "name": "private", "body": "secret format"})
    snapshot = service.snapshot(p["id"])
    assert not any(key.startswith("templates:") for key in snapshot["records"])
    assert "secret format" not in str(snapshot)
    service.sync(p["id"])
    assert not any(key.startswith("templates:") for key in remote.snapshots[p["id"]][0]["records"])


def test_join_legacy_snapshot_preserves_local_personal_template(tmp_path):
    remote = Remote()
    a = Service(tmp_path / "a", github_factory=lambda: remote.client(1))
    b = Service(tmp_path / "b", github_factory=lambda: remote.client(2))
    p = a.create_project({"name": "legacy", "intent": "join legacy project"})
    a.sync(p["id"])
    snapshot = copy.deepcopy(remote.snapshots[p["id"]][0])
    snapshot["schema_version"] = 1
    snapshot["records"]["templates:requirements"] = {
        "id": "requirements", "name": "legacy remote personal", "body": "# remote personal format"}
    remote.snapshots[p["id"]] = snapshot, "legacy-shared-sha"
    b.store.change("templates", "requirements", body="# my personal format")
    original = b.store.get("templates", "requirements")

    b.join(p["repo"], p["id"])
    assert b.store.get("templates", "requirements") == original
    b.sync(p["id"])
    assert b.store.get("templates", "requirements") == original
    assert not any(key.startswith("templates:") for key in b.snapshot(p["id"])["records"])


def test_recordwise_remote_accepts_stale_unchanged_keys_and_rejects_divergence():
    remote = Remote()
    client = remote.client()
    project = {"id": "p"}
    original = {"schema_version": 2, "records": {"project": {"id": "p"}, "tasks:a": {"id": "a"},
                                                  "tasks:b": {"id": "b"}}}
    token = client.publish_snapshot(project, original)
    first = copy.deepcopy(original)
    first["records"]["tasks:a"]["state"] = "running"
    client.publish_snapshot(project, first, token)
    second = copy.deepcopy(original)
    second["records"]["tasks:b"]["state"] = "running"
    client.publish_snapshot(project, second, token)
    current, _ = client.snapshot(project)
    assert current["records"]["tasks:a"]["state"] == "running"
    assert current["records"]["tasks:b"]["state"] == "running"
    divergent = copy.deepcopy(original)
    divergent["records"]["tasks:a"]["state"] = "failed"
    with pytest.raises(Conflict):
        client.publish_snapshot(project, divergent, token)
    assert client.snapshot(project)[0] == current
