import pytest
from ai_system.service import Service
from ai_system.store import Conflict


class Reviews:
    def __init__(self):
        self.head = "final-head"
        self.state = "APPROVED"
    def close(self):
        pass
    def checks_pass(self, repo, sha):
        return True
    def pr_reviews(self, repo, number):
        head = self.head if number == 99 else "artifact-head"
        pr = {"number": number, "head": {"sha": head}, "user": {"id": 1}, "state": "open",
              "mergeable": True, "mergeable_state": "clean"}
        return pr, [{"id": 1, "user": {"id": 2, "type": "User"}, "state": self.state,
                    "commit_id": head, "submitted_at": "2026-10-24T00:00:00Z", "html_url": "https://github.com/o/r/pull/99#review"}]


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
    assert s.refresh_approval(final["id"])["state"] == "succeeded"
    assert s.project(p["id"])["state"] == "completed"
    remote.state = "DISMISSED"
    assert s.refresh_approval(final["id"])["state"] == "waiting_approval"
    assert s.project(p["id"])["state"] == "active"


def test_external_final_push_invalidates(tmp_path):
    s, remote, _, final = setup_final(tmp_path)
    remote.head = "other-head"
    with pytest.raises(Conflict):
        s.refresh_approval(final["id"])
    assert s.store.get("tasks", final["id"])["state"] == "waiting_approval"


def test_new_task_invalidates_manifest(tmp_path):
    s, _, p, final = setup_final(tmp_path)
    s.add_task(p["id"], {"title": "追加", "role": "coding", "depends_on": []})
    assert s.refresh_approval(final["id"])["state"] == "waiting_approval"


def test_incomplete_dependencies_prevent_final(tmp_path):
    s, _, p, final = setup_final(tmp_path)
    s.store.change("tasks", p["tasks"][0]["id"], state="ready")
    assert s.refresh_approval(final["id"])["state"] == "waiting_approval"
