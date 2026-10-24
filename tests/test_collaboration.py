import copy
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from ai_system.service import Service
from ai_system.store import Conflict


class Remote:
    def __init__(self):
        self.snapshots = {}
        self.refs = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.snapshot_barrier = None
        self.pull_requests = {}
        self.reviews = {}

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
        with self.r.lock:
            if project["id"] not in self.r.snapshots:
                raise KeyError(project["id"])
            result = copy.deepcopy(self.r.snapshots[project["id"]])
            barrier = self.r.snapshot_barrier
        if barrier:
            barrier.wait(timeout=10)
            with self.r.lock:
                if self.r.snapshot_barrier is barrier:
                    self.r.snapshot_barrier = None
        return result

    def publish_snapshot(self, project, snapshot, expected_sha=None):
        """Model per-record CAS, including idempotent retries and partial publication.

        A stale version for an untouched record must not prevent another record
        from being published. A changed record must still match its baseline.
        """
        with self.r.lock:
            old = self.r.snapshots.get(project["id"])
            current = copy.deepcopy(old[0]["records"] if old else {})
            versions = copy.deepcopy({key: value for key, value in old[1].items() if key != "legacy"}
                                     if old and isinstance(old[1], dict) else {})
            if expected_sha is None:
                baseline = {}
            elif isinstance(expected_sha, dict):
                baseline = {key: value["record"] for key, value in expected_sha.items() if key != "legacy"}
            elif old and old[1] == expected_sha:
                baseline = copy.deepcopy(current)  # Legacy single-snapshot token.
            else:
                raise Conflict("stale snapshot token")
            desired = snapshot["records"]
            changed = {key for key in set(baseline) | set(desired) if desired.get(key) != baseline.get(key)}
            for key in changed:
                if current.get(key) not in (baseline.get(key), desired.get(key)):
                    raise Conflict("stale record: " + key)
            for key in changed:
                if key not in desired:
                    current.pop(key, None)
                    versions.pop(key, None)
                    continue
                if current.get(key) != desired[key] or key not in versions:
                    self.r.counter += 1
                    current[key] = copy.deepcopy(desired[key])
                    versions[key] = {"sha": str(self.r.counter), "record": copy.deepcopy(desired[key])}
            # Migration of untouched records from the legacy snapshot.
            for key, record in current.items():
                if key not in versions:
                    self.r.counter += 1
                    versions[key] = {"sha": str(self.r.counter), "record": copy.deepcopy(record)}
            token = versions
            self.r.snapshots[project["id"]] = ({"schema_version": 2, "records": current}, token)
            return copy.deepcopy(token)
    def claim(self, project, task, owner):
        with self.r.lock:
            if task["id"] in self.r.refs:
                raise Conflict("already owned")
            self.r.refs[task["id"]] = owner
        return "ai-work/" + task["id"], "claim"
    def check_owner(self, project, task):
        if self.r.refs[task["id"]] != task["owner"]:
            raise Conflict("ownership changed")

    def pr_reviews(self, repo, number):
        return copy.deepcopy(self.r.pull_requests[number]), copy.deepcopy(self.r.reviews.get(number, []))

    def checks_pass(self, repo, sha):
        return True


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


def test_two_devices_publish_different_tasks_concurrently(tmp_path):
    remote, a, b, p = make_pair(tmp_path)
    first, second = p["tasks"][:2]
    a.store.change("tasks", first["id"], note="from a")
    b.store.change("tasks", second["id"], note="from b")
    # Both writers must read the same remote version before either publishes.
    remote.snapshot_barrier = threading.Barrier(2)
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(lambda service: service.sync(p["id"]), [a, b]))
    assert len(results) == 2
    remote.snapshot_barrier = None
    a.sync(p["id"])
    b.sync(p["id"])
    for service in (a, b):
        assert service.store.get("tasks", first["id"])["note"] == "from a"
        assert service.store.get("tasks", second["id"])["note"] == "from b"


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


def test_simultaneous_claim_has_exactly_one_winner(tmp_path):
    remote, a, b, p = make_pair(tmp_path)
    tid = p["tasks"][0]["id"]
    ready = threading.Barrier(2)

    def claim(service):
        ready.wait(timeout=10)
        try:
            return service.claim(tid)["owner"]
        except Conflict:
            return None

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(claim, [a, b]))
    winners = [owner for owner in results if owner]
    assert len(winners) == 1
    assert remote.refs[tid] == winners[0]
    winner = a if winners[0]["user_id"] == 1 else b
    loser = b if winner is a else a
    winner.sync(p["id"])
    loser.sync(p["id"])
    assert loser.store.get("tasks", tid)["owner"] == winners[0]


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
