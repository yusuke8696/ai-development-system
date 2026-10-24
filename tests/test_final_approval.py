import pytest
from ai_system.service import Service
from ai_system.store import Conflict


class Reviews:
    def __init__(self):
        self.head = "final-head"
        self.state = "APPROVED"
        self.author_id = 1
        self.merged = False
        self.closed = False
        self.with_reviews = True
        self.ci_passed = True
        self.check_calls = 0
        self.final_reads = 0
        self.on_recheck = None
    def close(self):
        pass
    def checks_pass(self, repo, sha):
        self.check_calls += 1
        return self.ci_passed
    def pr_reviews(self, repo, number):
        if number == 99:
            self.final_reads += 1
            if self.final_reads == 2 and self.on_recheck:
                self.on_recheck()
        head = self.head if number == 99 else "artifact-head"
        pr = {"number": number, "head": {"sha": head}, "user": {"id": self.author_id},
              "state": "closed" if self.merged or self.closed else "open", "merged": self.merged,
              "merge_commit_sha": "a" * 40 if self.merged else None,
              "merged_by": {"id": self.author_id} if self.merged else None,
              "merged_at": "2026-10-24T01:00:00Z" if self.merged else None,
              "html_url": f"https://github.com/o/r/pull/{number}",
              "mergeable": True, "mergeable_state": "clean"}
        reviews = [{"id": 1, "user": {"id": 2, "type": "User"}, "state": self.state,
                    "commit_id": head, "submitted_at": "2026-10-24T00:00:00Z", "html_url": "https://github.com/o/r/pull/99#review"}]
        return pr, reviews if self.with_reviews else []


def setup_final(tmp_path):
    remote = Reviews()
    s = Service(tmp_path, github_factory=lambda: remote)
    p = s.create_project({"name": "final test", "intent": "final test"})
    s.store.change("projects", p["id"], leader_id=2)
    manifest = []
    for i, t in enumerate(p["tasks"][:-1]):
        pr = {"number": i + 1, "sha": "artifact-head"}
        s.store.change("tasks", t["id"], state="succeeded", pr=pr)
        manifest.append({"task_id": t["id"], "pr": pr})
    final = s.store.change("tasks", p["tasks"][-1]["id"], state="waiting_approval",
        pr={"number": 99, "sha": "final-head"}, review_manifest={"leader_id": 2, "artifacts": manifest})
    return s, remote, p, final


def test_final_approval_completes_and_dismissal_reopens(tmp_path):
    s, remote, p, final = setup_final(tmp_path)
    original = s.store.get("tasks", final["id"])
    snapshot = s.snapshot(p["id"])
    assert s.refresh_approval(final["id"])["state"] == "succeeded"
    assert s.project(p["id"])["state"] == "completed"
    assert s.store.get("tasks", final["id"]) == original
    assert s.snapshot(p["id"]) == snapshot
    remote.state = "DISMISSED"
    assert s.refresh_approval(final["id"])["state"] == "waiting_approval"
    assert s.project(p["id"])["state"] == "active"
    assert s.store.get("tasks", final["id"]) == original
    assert s.snapshot(p["id"]) == snapshot


def test_replica_restart_does_not_retain_observed_completion_as_shared_state(tmp_path):
    s, remote, p, final = setup_final(tmp_path)
    assert s.refresh_approval(final["id"])["state"] == "succeeded"
    restarted = Service(tmp_path, github_factory=lambda: remote)
    assert restarted.store.get("tasks", final["id"])["state"] == "waiting_approval"
    assert restarted.snapshot(p["id"])["records"]["tasks:" + final["id"]]["state"] == "waiting_approval"
    remote.state = "DISMISSED"
    restarted.refresh_approval(final["id"])
    assert restarted.project(p["id"])["state"] == "active"


def test_external_final_push_invalidates(tmp_path):
    s, remote, _, final = setup_final(tmp_path)
    remote.head = "other-head"
    with pytest.raises(Conflict):
        s.refresh_approval(final["id"])
    assert s.store.get("tasks", final["id"])["state"] == "waiting_approval"


def test_new_task_invalidates_manifest(tmp_path):
    s, _, p, final = setup_final(tmp_path)
    assert s.refresh_approval(final["id"])["state"] == "succeeded"
    s.add_task(p["id"], {"title": "追加", "role": "coding", "depends_on": []})
    assert s.project(p["id"])["state"] == "active"
    assert s.refresh_approval(final["id"])["state"] == "waiting_approval"


def test_incomplete_dependencies_prevent_final(tmp_path):
    s, _, p, final = setup_final(tmp_path)
    s.store.change("tasks", p["tasks"][0]["id"], state="ready")
    assert s.refresh_approval(final["id"])["state"] == "waiting_approval"


@pytest.mark.parametrize("author_id", [1, 2])
def test_merged_final_completes_without_reviews_or_additional_ci_gate(tmp_path, author_id):
    s, remote, p, final = setup_final(tmp_path)
    remote.author_id = author_id
    remote.merged, remote.with_reviews, remote.ci_passed = True, False, False
    before = s.snapshot(p["id"])
    result = s.refresh_approval(final["id"])
    assert result["state"] == "succeeded" and s.project(p["id"])["state"] == "completed"
    assert result["approval_evidence"]["kind"] == "merge"
    assert result["approval_evidence"]["merger_id"] == author_id
    assert result["approval_evidence"]["merge_commit_sha"] == "a" * 40
    assert remote.check_calls == 0
    assert s.snapshot(p["id"]) == before  # Observations stay local on every PC.


def test_solo_leader_must_merge_before_final_completion(tmp_path):
    s, remote, p, final = setup_final(tmp_path)
    remote.author_id, remote.with_reviews = 2, False
    result = s.refresh_approval(final["id"])
    assert result["state"] == "waiting_approval"
    assert "GitHubで内容を確認してマージ" in result["approval_reason"]
    assert s.project(p["id"])["state"] == "active"
    remote.merged = True
    assert s.refresh_approval(final["id"])["state"] == "succeeded"


def test_unmerged_final_still_requires_ci(tmp_path):
    s, remote, _, final = setup_final(tmp_path)
    remote.ci_passed = False
    result = s.refresh_approval(final["id"])
    assert result["state"] == "waiting_approval" and "CI" in result["approval_reason"]
    assert remote.check_calls == 1


@pytest.mark.parametrize("change", ["head", "artifact"])
def test_merged_final_still_requires_the_registered_version(tmp_path, change):
    s, remote, _, final = setup_final(tmp_path)
    remote.merged, remote.with_reviews = True, False
    if change == "head":
        remote.head = "different-head"
    else:
        task = s.store.get("tasks", final["depends_on"][0])
        s.store.change("tasks", task["id"], pr=dict(task["pr"], sha="different-artifact-head"))
    if change == "head":
        with pytest.raises(Conflict):
            s.refresh_approval(final["id"])
    else:
        assert s.refresh_approval(final["id"])["state"] == "waiting_approval"


@pytest.mark.parametrize("change", ["closed", "dismissed"])
def test_approval_is_rechecked_before_completion(tmp_path, change):
    s, remote, _, final = setup_final(tmp_path)
    def revoke():
        if change == "closed":
            remote.closed = True
        else:
            remote.state = "DISMISSED"
    remote.on_recheck = revoke
    assert s.refresh_approval(final["id"])["state"] == "waiting_approval"


def test_merged_solo_final_confirmation_api(tmp_path):
    from fastapi.testclient import TestClient
    from ai_system.server import create_app
    s, remote, p, final = setup_final(tmp_path)
    remote.author_id, remote.merged, remote.with_reviews = 2, True, False
    with TestClient(create_app(tmp_path, bootstrap="test", service=s, polling=False)) as client:
        client.get("/launch/test")
        client.headers["x-csrf-token"] = client.get("/api/state").json()["csrf"]
        result = client.post(f"/api/tasks/{final['id']}/refresh", json={})
        assert result.status_code == 200 and result.json()["state"] == "succeeded"
        assert client.get("/api/state").json()["projects"][0]["state"] == "completed"
