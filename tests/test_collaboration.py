import copy
import threading
import pytest
from ai_system.service import Service
from ai_system.store import Conflict


class Remote:
    def __init__(self):
        self.snapshots = {}
        self.refs = {}
        self.lock = threading.RLock()
        self.counter = 0
    def client(self, user_id=1):
        return Client(self, user_id)


class Client:
    def __init__(self, remote, user_id):
        self.r = remote
        self.user_id = user_id
    def close(self):
        pass
    def user(self):
        return {"id": self.user_id, "login": "person" + str(self.user_id)}
    def user_by_login(self, login):
        return {"id": 2, "type": "User", "login": login}
    def snapshot(self, project):
        if project["id"] not in self.r.snapshots:
            raise KeyError(project["id"])
        return copy.deepcopy(self.r.snapshots[project["id"]])
    def publish_snapshot(self, project, snapshot, expected_sha=None):
        with self.r.lock:
            old = self.r.snapshots.get(project["id"])
            if old and old[1] != expected_sha:
                raise Conflict("stale SHA")
            self.r.counter += 1
            sha = str(self.r.counter)
            self.r.snapshots[project["id"]] = (copy.deepcopy(snapshot), sha)
            return sha
    def claim(self, project, task, owner):
        with self.r.lock:
            if task["id"] in self.r.refs:
                raise Conflict("already owned")
            self.r.refs[task["id"]] = owner
        return "ai-work/" + task["id"], "claim"
    def check_owner(self, project, task):
        if self.r.refs[task["id"]] != task["owner"]:
            raise Conflict("ownership changed")


def make_pair(tmp_path):
    r = Remote()
    a = Service(tmp_path / "a", github_factory=lambda: r.client(1))
    b = Service(tmp_path / "b", github_factory=lambda: r.client(2))
    p = a.create_project({"name": "共同作業", "intent": "共同作業のテスト", "leader_login": "leader"})
    a.sync(p["id"])
    b.join(p["repo"], p["id"])
    return r, a, b, p


def test_two_devices_merge_different_tasks(tmp_path):
    _, a, b, p = make_pair(tmp_path)
    t1, t2 = p["tasks"][:2]
    a.store.change("tasks", t1["id"], note="from a")
    b.store.change("tasks", t2["id"], note="from b")
    a.sync(p["id"])
    b.sync(p["id"])
    a.sync(p["id"])
    assert a.store.get("tasks", t2["id"])["note"] == "from b"
    assert b.store.get("tasks", t1["id"])["note"] == "from a"


def test_conflict_preserves_local(tmp_path):
    _, a, b, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    a.store.change("tasks", tid, note="a")
    b.store.change("tasks", tid, note="b")
    a.sync(p["id"])
    with pytest.raises(Conflict):
        b.sync(p["id"])
    assert b.store.get("tasks", tid)["note"] == "b"
    assert b.store.get("projects", p["id"])["sync_error"]


def test_only_one_device_can_claim(tmp_path):
    _, a, b, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    a.claim(tid)
    with pytest.raises(Conflict):
        b.claim(tid)
    # Other tasks can run independently.
    assert b.claim(p["tasks"][1]["id"])["owner"]["user_id"] == 2


def test_old_owner_cannot_publish_after_handover(tmp_path):
    r, a, b, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    a.claim(tid)
    r.refs[tid] = dict(r.refs[tid], device_id=b.settings()["device_id"], user_id=2)
    with pytest.raises(Conflict):
        a.claim(tid)


def test_leader_change_invalidates_old_id(tmp_path):
    _, a, _, p = make_pair(tmp_path)
    assert a.store.get("projects", p["id"])["leader_id"] == 2


def test_agile_cycle_is_not_project_completion(tmp_path):
    s = Service(tmp_path)
    p = s.create_project({"name": "agile", "intent": "test", "workflow": "agile"})
    for t in p["tasks"]:
        s.store.change("tasks", t["id"], state="succeeded")
    assert s.project(p["id"])["state"] == "active"
    p = s.finish_project(p["id"])
    assert p["tasks"][-1]["project_final"]
    assert p["tasks"][-1]["state"] == "ready"
    assert p["state"] == "active"
