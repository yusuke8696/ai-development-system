import copy
import json

import pytest
from fastapi.testclient import TestClient

from ai_system.project_templates import digest, template_payload
from ai_system.server import create_app
from ai_system.service import Service
from ai_system.store import Conflict
from ai_system.templates import validate_template


class FormatGitHub:
    def __init__(self):
        self.files = {}
        self.number = 0
        self.merged = False
        self.approved = True
        self.author_id = 7
        self.with_reviews = True
        self.reads = []
        self.offline = False
        self.repo = "example/repo"
        self.base_branch = "main"
        self.review_head = None
        self.remote_head = None
        self.base_repo = None

    def close(self):
        pass

    def user_by_login(self, login):
        return {"id": 42 if login == "leader" else 43, "login": login, "type": "User"}

    def publish_files_pr(self, project, branch, files, title, body):
        assert branch.startswith("ai-formats/" + project["id"] + "/")
        self.repo, self.base_branch = project["repo"], project["base_branch"]
        self.number += 1
        self.files = dict(files)
        self.head = f"{self.number:040x}"
        self.commit = f"{100 + self.number:040x}"
        return {"number": self.number, "url": f"https://github.com/{self.repo}/pull/{self.number}", "sha": self.head}

    def pr_reviews(self, repo, number):
        if self.offline:
            raise ValueError("offline")
        assert repo == self.repo and number == self.number
        pr = {"user": {"id": self.author_id, "type": "User"}, "merged": self.merged,
              "state": "closed" if self.merged else "open", "merge_commit_sha": self.commit,
              "merged_by": {"id": self.author_id, "type": "User"},
              "merged_at": "2026-10-24T01:00:00Z" if self.merged else None,
              "html_url": f"https://github.com/{repo}/pull/{number}",
              "head": {"sha": self.remote_head or self.head, "repo": {"full_name": repo}},
              "base": {"ref": self.base_branch, "repo": {"full_name": self.base_repo or repo}}}
        reviews = [{"id": 900, "user": {"id": 42, "login": "leader", "type": "User"},
                    "state": "APPROVED" if self.approved else "CHANGES_REQUESTED",
                    "submitted_at": "2026-10-24T00:00:00Z", "commit_id": self.review_head or self.head}]
        return pr, reviews if self.with_reviews else []

    def content(self, repo, path, ref):
        assert repo == self.repo and ref == self.commit  # Never mutable main or a personal draft.
        self.reads.append((path, ref))
        return self.files[path], "blob-sha"


def default_bindings(service, project):
    rows = service.template_preparation(project["id"])["required"]
    personal = service.store.list("templates")
    return {r["document_type"]: next(t["id"] for t in personal if t["document_type"] == r["document_type"]) for r in rows}


def prepare_verified(service, project, gh, bindings=None):
    """Exercise the real registration+approval path against an isolated GitHub fake."""
    service.prepare_templates(project["id"], bindings or default_bindings(service, project))
    gh.merged = True
    return service.confirm_templates(project["id"])


@pytest.fixture
def workspace(tmp_path):
    gh = FormatGitHub()
    service = Service(tmp_path, github_factory=lambda: gh)
    project = service.create_project({"name": "demo", "intent": "project format tests", "repo": "example/repo", "leader_login": "leader"})
    yield service, project, gh
    service.close()


def test_new_project_cannot_start_before_format_registration(workspace):
    service, project, _ = workspace
    assert not service.template_preparation(project["id"])["ready"]
    with pytest.raises(Conflict, match="書式|開発準備"):
        service.start(project["tasks"][0]["id"], {})
    assert not service.jobs


def test_registration_contains_only_selected_project_formats(workspace):
    service, project, gh = workspace
    before = copy.deepcopy(service.store.list("templates"))
    result = service.prepare_templates(project["id"], default_bindings(service, project))
    assert not result["ready"] and result["pr"]["number"] == 1
    assert len(result["required"]) == 5
    assert all(path.startswith((".ai-dev/templates/" + project["id"], ".ai-dev/template-locks/",
                               ".ai-dev/oss/" + project["id"], ".github/workflows/aiworkspace-oss-" + project["id"])) for path in gh.files)
    assert not any("backlog" in path or "owner.json" in path for path in gh.files)
    assert service.store.list("templates") == before
    assert not service.store.list("project_templates")


def test_registration_rejects_missing_or_wrong_type_bindings(workspace):
    service, project, _ = workspace
    with pytest.raises(ValueError, match="すべて"):
        service.prepare_templates(project["id"], {})
    bindings = default_bindings(service, project)
    bindings["requirements"] = "design"
    with pytest.raises(ValueError, match="文書種別"):
        service.prepare_templates(project["id"], bindings)


@pytest.mark.parametrize("change", ["unmerged", "changed_head", "wrong_repo", "wrong_base"])
def test_registration_requires_matching_github_merge(workspace, change):
    service, project, gh = workspace
    service.prepare_templates(project["id"], default_bindings(service, project))
    gh.merged = True
    if change == "unmerged":
        gh.merged = False
    elif change == "changed_head":
        gh.remote_head = gh.review_head = "f" * 40
    elif change == "wrong_repo":
        gh.base_repo = "other/repo"
    else:
        gh.base_branch = "other"
    with pytest.raises(Conflict):
        service.confirm_templates(project["id"])
    assert not service.template_preparation(project["id"])["ready"]
    assert not service.store.list("template_verifications")


@pytest.mark.parametrize("author_id", [7, 42])
@pytest.mark.parametrize("reviews", ["none", "changes_requested", "stale"])
def test_github_merge_confirms_formats_including_solo_leader(workspace, author_id, reviews):
    service, project, gh = workspace
    gh.author_id = author_id
    service.prepare_templates(project["id"], default_bindings(service, project))
    gh.merged = True
    gh.with_reviews = reviews != "none"
    gh.approved = reviews != "changes_requested"
    gh.review_head = "f" * 40 if reviews == "stale" else None
    result = service.confirm_templates(project["id"])
    assert result["ready"] and result["commit"] == gh.commit
    assert result["approval_kind"] == "merge" and result["review_id"] is None
    proof = service.store.list("template_verifications")[0]["approval_evidence"]
    assert proof["kind"] == "merge" and proof["merger_id"] == author_id
    assert proof["merge_commit_sha"] == gh.commit and proof["sha"] == gh.head
    assert service.project_template(project["id"], "requirements")["pinned_commit"] == gh.commit


def test_solo_leader_waits_for_merge_without_requesting_another_leader(workspace):
    service, project, gh = workspace
    gh.author_id = 42
    gh.with_reviews = False
    service.prepare_templates(project["id"], default_bindings(service, project))
    with pytest.raises(Conflict, match="GitHubで内容を確認してマージ"):
        service.confirm_templates(project["id"])
    assert not service.template_preparation(project["id"])["ready"]


def test_confirm_pins_merge_commit_and_personal_edits_cannot_change_it(workspace):
    service, project, gh = workspace
    pin = prepare_verified(service, project, gh)
    original = service.project_template(project["id"], "requirements")
    service.store.change("templates", "requirements", body="# Personal edit\n")
    assert service.project_template(project["id"], "requirements")["body"] == original["body"]
    assert pin["commit"] == gh.commit
    assert all(ref == gh.commit for _, ref in gh.reads)
    reads = list(gh.reads)
    gh.offline = True
    assert service.ensure_prepared(project["id"])["ready"]
    assert gh.reads == reads  # Ready cache works offline and never queries mutable main.


def test_repeated_confirmation_on_another_pc_never_changes_shared_pin(workspace, tmp_path):
    from ai_system.service import shared
    service, project, gh = workspace
    prepare_verified(service, project, gh)
    before = shared(service.store.get("projects", project["id"]))
    second = Service(tmp_path / "second", github_factory=lambda: gh)
    second.store.put("projects", before)
    for task in service.store.list("tasks"):
        second.store.put("tasks", shared(task))
    assert second.confirm_templates(project["id"])["ready"]
    assert shared(second.store.get("projects", project["id"])) == before
    service.confirm_templates(project["id"])
    assert shared(service.store.get("projects", project["id"])) == before


@pytest.mark.parametrize("tamper", ["body", "manifest", "lock"])
def test_merged_content_integrity_is_verified(workspace, tamper):
    service, project, gh = workspace
    service.prepare_templates(project["id"], default_bindings(service, project))
    gh.merged = True
    ending = {"body": "template.md", "manifest": "manifest.json", "lock": ".json"}[tamper]
    path = next(p for p in gh.files if p.endswith(ending) and (tamper != "lock" or "/template-locks/" in p))
    if tamper == "body":
        gh.files[path] += "\nTampered"
    else:
        value = json.loads(gh.files[path])
        value["name" if tamper == "manifest" else "repo"] = "tampered"
        gh.files[path] = json.dumps(value)
    with pytest.raises(Conflict):
        service.confirm_templates(project["id"])
    assert not service.store.list("project_templates")


def test_joined_pc_verifies_remote_ready_and_keeps_personal_defaults(workspace, tmp_path):
    service, project, gh = workspace
    custom = validate_template({"id": "company-req", "name": "Company", "document_type": "requirements", "body": "# Company\n## Purpose", "required_headings": ["Purpose"]})
    service.store.put("templates", custom)
    bindings = default_bindings(service, project)
    bindings["requirements"] = "company-req"
    prepare_verified(service, project, gh, bindings)
    other = Service(tmp_path / "second", github_factory=lambda: gh)
    try:
        personal_before = copy.deepcopy(other.store.list("templates"))
        for task in service.tasks(project["id"]):
            other.store.put("tasks", task)
        other.store.put("projects", service.store.get("projects", project["id"]))
        assert not other.template_preparation(project["id"])["ready"]
        assert other.ensure_prepared(project["id"])["ready"]
        pinned = other.project_template(project["id"], "requirements")
        assert pinned["id"] == "company-req" and pinned["body"] == custom["body"]
        assert other.store.list("templates") == personal_before
        assert "company-req" not in {t["id"] for t in other.store.list("templates")}
    finally:
        other.close()


def test_untrusted_shared_ready_flag_cannot_bypass_github_merge(workspace, tmp_path):
    service, project, gh = workspace
    prepare_verified(service, project, gh)
    other = Service(tmp_path / "untrusted", github_factory=lambda: gh)
    try:
        other.store.put("projects", service.store.get("projects", project["id"]))
        for task in service.tasks(project["id"]):
            other.store.put("tasks", task)
        gh.merged = False
        with pytest.raises(Conflict):
            other.ensure_prepared(project["id"])
        assert not other.template_preparation(project["id"])["ready"]
    finally:
        other.close()


def test_new_document_kind_and_leader_change_invalidate_preparation(workspace):
    service, project, gh = workspace
    prepare_verified(service, project, gh)
    service.store.change("projects", project["id"], leader_login="other-leader")
    assert not service.template_preparation(project["id"])["ready"]
    service.store.change("projects", project["id"], leader_login="leader")
    service.store.put("templates", validate_template({"id": "audit", "name": "Audit", "document_type": "audit", "body": "# Audit"}))
    service.store.put("tasks", {"project_id": project["id"], "kind": "documentation", "template_id": "audit", "title": "Audit", "state": "ready"})
    assert not service.template_preparation(project["id"])["ready"]
    with pytest.raises(Conflict, match="必要な文書種別"):
        service.ensure_prepared(project["id"])


def test_corrupt_cache_is_not_trusted_offline(workspace):
    service, project, gh = workspace
    prepare_verified(service, project, gh)
    cached = service.store.list("project_templates")[0]
    service.store.change("project_templates", cached["id"], body="# Corrupt")
    assert not service.template_preparation(project["id"])["ready"]
    gh.offline = True
    with pytest.raises(ValueError, match="offline"):
        service.ensure_prepared(project["id"])
    gh.offline = False
    assert service.ensure_prepared(project["id"])["ready"]


@pytest.mark.parametrize("change", [{"base_branch": "release"}, {"leader_id": 999}])
def test_changed_project_approval_authority_invalidates_cached_preflight(workspace, change):
    service, project, gh = workspace
    prepare_verified(service, project, gh)
    service.store.change("projects", project["id"], **change)
    assert not service.template_preparation(project["id"])["ready"]


def test_hash_is_stable_across_local_versions_and_prior_hashes(workspace):
    service, _, _ = workspace
    item = service.store.get("templates", "requirements")
    assert digest(template_payload(item)) == digest(template_payload(dict(item, hash="obsolete", version=999, updated_at="tomorrow")))


def test_same_template_id_is_isolated_between_projects_and_repositories(workspace):
    service, first, gh = workspace
    prepare_verified(service, first, gh)
    first_format = service.project_template(first["id"], "requirements")
    service.store.change("templates", "requirements", body="# Second repository format\n## Purpose")
    second = service.create_project({"name": "second", "intent": "other repo", "repo": "other/repo", "leader_login": "leader"})
    prepare_verified(service, second, gh)
    second_format = service.project_template(second["id"], "requirements")
    assert second_format["body"] != first_format["body"]
    assert service.project_template(first["id"], "requirements")["body"] == first_format["body"]
    cached = service.store.list("project_templates")
    assert len(cached) == len({item["id"] for item in cached}) == 10
    assert {item["repo"] for item in cached} == {"example/repo", "other/repo"}


@pytest.mark.parametrize("solo", [False, True])
def test_preparation_api_and_state_keep_formats_separate(workspace, solo):
    service, project, gh = workspace
    if solo:
        gh.author_id, gh.with_reviews = 42, False
    app = create_app(service.store.root, bootstrap="test", service=service, polling=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/launch/test")
        state = client.get("/api/state").json()
        client.headers["x-csrf-token"] = state["csrf"]
        assert state["project_templates"] == []
        assert not client.get(f"/api/projects/{project['id']}/preparation").json()["ready"]
        assert client.post(f"/api/projects/{project['id']}/templates/prepare", json={"bindings": default_bindings(service, project)}).status_code == 200
        assert client.post(f"/api/projects/{project['id']}/templates/confirm", json={}).status_code == 409
        gh.merged = True
        assert client.post(f"/api/projects/{project['id']}/templates/confirm", json={}).json()["ready"]
        updated = client.get("/api/state").json()
        assert updated["project_templates"]
        assert updated["projects"][0]["template_preparation"]["ready"]
        assert updated["templates"] == state["templates"]
