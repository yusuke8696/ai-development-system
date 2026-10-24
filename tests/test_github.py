import base64
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
import httpx
import pytest
from ai_system.github import GitHub, repository
from ai_system.store import Conflict


@pytest.mark.parametrize("url", ["yusuke8696/ai-development-system", "https://github.com/yusuke8696/ai-development-system.git"])
def test_repository_url(url):
    assert repository(url) == "yusuke8696/ai-development-system"


def test_repository_rejects_credentials():
    with pytest.raises(ValueError):
        repository("https://token@github.com/owner/repo")


class RESTRepository:
    """GitHub-shaped REST fixture: immutable commits, refs and blob SHA CAS."""
    def __init__(self):
        self.refs = {"main": "base"}
        self.commits = {"base": {"files": {"README.md": "base\n"}, "parents": []}}
        self.trees = {"base": self.commits["base"]["files"]}
        self.calls, self.prs = [], []
        self.before_put = self.before_advance = None
        self.lose_put_response = self.lose_control_response = False
        self.serial = 0

    @staticmethod
    def blob(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def commit(self, parent, files):
        self.serial += 1
        sha = "commit-" + str(self.serial)
        self.commits[sha] = {"files": dict(self.commits[parent]["files"], **files), "parents": [parent]}
        self.trees[sha] = self.commits[sha]["files"]
        return sha

    def put(self, branch, path, value):
        if branch not in self.refs:
            self.refs[branch] = self.refs["main"]
        self.refs[branch] = self.commit(self.refs[branch], {path: value})

    def files(self, ref):
        return self.commits[self.refs.get(ref, ref)]["files"]

    def client(self):
        return GitHub(transport=httpx.MockTransport(self))

    def __call__(self, req):
        path = req.url.path.removeprefix("/repos/owner/repo")
        body = json.loads(req.content) if req.content else {}
        self.calls.append((req.method, path, body))
        if path.startswith("/git/ref/heads/"):
            branch = path.removeprefix("/git/ref/heads/")
            return httpx.Response(200, json={"object": {"sha": self.refs[branch]}}) if branch in self.refs else httpx.Response(404)
        if path.startswith("/git/matching-refs/heads/"):
            prefix = path.removeprefix("/git/matching-refs/heads/")
            return httpx.Response(200, json=[{"ref": "refs/heads/" + branch, "object": {"sha": sha}}
                                            for branch, sha in self.refs.items() if branch.startswith(prefix)])
        if path == "/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                return httpx.Response(422, json={})
            self.refs[branch] = body["sha"]
            if self.lose_control_response and branch.startswith("ai-control/"):
                self.lose_control_response = False
                raise httpx.ReadTimeout("response lost", request=req)
            return httpx.Response(201, json={"ref": body["ref"]})
        if path.startswith("/git/refs/heads/"):
            branch = path.removeprefix("/git/refs/heads/")
            if self.before_advance:
                callback, self.before_advance = self.before_advance, None
                callback(branch)
            pending = [body["sha"]]
            ancestors = set()
            while pending:
                sha = pending.pop()
                if sha in ancestors:
                    continue
                ancestors.add(sha)
                pending.extend(self.commits[sha]["parents"])
            if self.refs[branch] not in ancestors:
                return httpx.Response(422, json={})
            assert body["force"] is False
            self.refs[branch] = body["sha"]
            return httpx.Response(200, json={"object": {"sha": body["sha"]}})
        if path.startswith("/contents/"):
            file = path.removeprefix("/contents/")
            ref = req.url.params.get("ref") if req.method == "GET" else body["branch"]
            try:
                files = self.files(ref)
            except KeyError:
                return httpx.Response(404)
            if req.method == "GET":
                if file not in files:
                    return httpx.Response(404)
                value = files[file]
                return httpx.Response(200, json={"content": base64.b64encode(value.encode()).decode(),
                                                 "encoding": "base64", "sha": self.blob(value)})
            if self.before_put:
                callback, self.before_put = self.before_put, None
                callback(ref, file)
                files = self.files(ref)
            old_sha = self.blob(files[file]) if file in files else None
            if old_sha != body.get("sha"):
                return httpx.Response(409, json={})
            value = base64.b64decode(body["content"]).decode()
            self.put(ref, file, value)
            if self.lose_put_response:
                self.lose_put_response = False
                raise httpx.ReadTimeout("response lost", request=req)
            return httpx.Response(200, json={"content": {"sha": self.blob(value)}})
        if path.startswith("/git/commits/"):
            sha = path.removeprefix("/git/commits/")
            return httpx.Response(200, json={"tree": {"sha": sha}})
        if path == "/git/trees":
            files = dict(self.trees[body["base_tree"]])
            files.update({item["path"]: item["content"] for item in body["tree"]})
            sha = "tree-" + str(len(self.trees))
            self.trees[sha] = files
            return httpx.Response(201, json={"sha": sha})
        if path == "/git/commits":
            sha = self.commit(body["parents"][0], self.trees[body["tree"]])
            return httpx.Response(201, json={"sha": sha})
        if path == "/pulls":
            if req.method == "GET":
                branch = req.url.params["head"].split(":", 1)[1]
                return httpx.Response(200, json=[pr for pr in self.prs if pr["head"] == branch])
            pr = dict(body, number=len(self.prs) + 1, html_url="https://github.com/owner/repo/pull/1")
            self.prs.append(pr)
            return httpx.Response(201, json=pr)
        raise AssertionError((req.method, path, body))


@pytest.fixture
def project():
    return {"repo": "owner/repo", "base_branch": "main", "id": "project-one"}


def owner(task_id="one", user=1, device="pc-a", execution="run-one"):
    return {"user_id": user, "device_id": device, "task_id": task_id, "execution_id": execution}


def initial_snapshot(project):
    return {"schema_version": 2, "records": {"project": project,
        "tasks:one": {"id": "one", "project_id": project["id"], "state": "ready"},
        "tasks:two": {"id": "two", "project_id": project["id"], "state": "ready"}}}


def test_claim_create_only_and_error(project):
    remote, task = RESTRepository(), {"id": "one", "title": "task"}
    gh = remote.client()
    branch, sha, actual = gh.claim(project, task, owner())
    assert branch == "ai-work/one"
    assert sha == "base" and actual == owner()
    assert ".ai-dev/owner.json" in remote.files("ai-control/one")
    assert ".ai-dev/owner.json" not in remote.files(branch)
    with pytest.raises(Conflict):
        gh.claim(project, task, owner(user=2))
    with pytest.raises(Conflict):
        gh.claim(project, task, owner(device="pc-b"))


def test_claim_retry_recovers_original_execution_after_unknown_result(project):
    remote, task = RESTRepository(), {"id": "one", "title": "task"}
    gh = remote.client()
    remote.lose_control_response = True
    assert gh.claim(project, task, owner())[2] == owner()
    before = dict(remote.refs)
    assert gh.claim(project, task, owner(execution="retry-run"))[2] == owner()
    assert remote.refs == before


def test_transfer_owner_changes_only_control_ref_and_fences_previous_owner(project):
    remote, task = RESTRepository(), {"id": "one", "title": "task"}
    gh = remote.client()
    branch, _, actual = gh.claim(project, task, owner())
    task.update(branch=branch, owner=actual)
    head = remote.refs[branch]
    next_owner = owner(user=2, device="pc-b", execution="next-run")
    gh.transfer_owner(project, task, next_owner)
    assert remote.refs[branch] == head
    assert json.loads(remote.files("ai-control/one")[".ai-dev/owner.json"]) == next_owner
    with pytest.raises(Conflict):
        gh.check_owner(project, task)
    with pytest.raises(Conflict):
        gh.transfer_owner(project, task, owner(user=3))


def test_transfer_cas_rejects_concurrent_handover(project):
    remote, task = RESTRepository(), {"id": "one", "title": "task"}
    gh = remote.client()
    branch, _, actual = gh.claim(project, task, owner())
    task.update(branch=branch, owner=actual)
    other = owner(user=3, device="pc-c", execution="concurrent")
    remote.before_put = lambda branch, file: remote.put(branch, file, json.dumps(other))
    with pytest.raises(Conflict):
        gh.transfer_owner(project, task, owner(user=2))
    assert json.loads(remote.files("ai-control/one")[".ai-dev/owner.json"]) == other


def test_snapshot_round_trip_and_unchanged_sync_does_not_write(project):
    remote, gh_snapshot = RESTRepository(), initial_snapshot(project)
    gh = remote.client()
    versions = gh.publish_snapshot(project, gh_snapshot)
    assert set(remote.refs) == {"main", "ai-system/project-one", "ai-data/project-one/tasks/one", "ai-data/project-one/tasks/two"}
    assert gh.snapshot(project) == (gh_snapshot, versions)
    remote.calls.clear()
    assert gh.publish_snapshot(project, gh_snapshot, versions) == versions
    assert remote.calls == []
    assert not any("shared.json" in files for files in [remote.files(branch) for branch in remote.refs])


def test_independent_updates_never_rewrite_other_record(project):
    remote = RESTRepository()
    gh = remote.client()
    original = initial_snapshot(project)
    versions = gh.publish_snapshot(project, original)
    a, b = copy.deepcopy(original), copy.deepcopy(original)
    a["records"]["tasks:one"]["state"] = "running"
    b["records"]["tasks:two"]["state"] = "running"
    gh.publish_snapshot(project, a, versions)
    remote.calls.clear()
    gh.publish_snapshot(project, b, versions)
    writes = [(path, body["branch"]) for method, path, body in remote.calls if method == "PUT"]
    assert writes == [("/contents/.ai-dev/record.json", "ai-data/project-one/tasks/two")]
    actual, _ = gh.snapshot(project)
    assert all(actual["records"]["tasks:" + key]["state"] == "running" for key in ("one", "two"))


def test_same_record_divergence_is_rejected_and_preserved(project):
    remote, initial = RESTRepository(), initial_snapshot(project)
    gh = remote.client()
    versions = gh.publish_snapshot(project, initial)
    a, b = copy.deepcopy(initial), copy.deepcopy(initial)
    a["records"]["tasks:one"]["state"] = "running"
    b["records"]["tasks:one"]["state"] = "cancelled"
    gh.publish_snapshot(project, a, versions)
    with pytest.raises(Conflict, match="tasks:one"):
        gh.publish_snapshot(project, b, versions)
    assert gh.snapshot(project)[0]["records"]["tasks:one"]["state"] == "running"


def test_snapshot_cas_handles_lost_success_and_refuses_competing_write(project):
    remote, original = RESTRepository(), initial_snapshot(project)
    gh = remote.client()
    versions = gh.publish_snapshot(project, original)
    changed = copy.deepcopy(original)
    changed["records"]["tasks:one"]["state"] = "running"
    remote.lose_put_response = True
    versions = gh.publish_snapshot(project, changed, versions)
    assert versions["tasks:one"]["record"]["state"] == "running"
    changed["records"]["tasks:one"]["state"] = "succeeded"
    rival = dict(changed["records"]["tasks:one"], state="cancelled")
    remote.before_put = lambda branch, file: remote.put(branch, file, json.dumps(rival))
    with pytest.raises(Conflict):
        gh.publish_snapshot(project, changed, versions)
    assert gh.snapshot(project)[0]["records"]["tasks:one"]["state"] == "cancelled"


def test_legacy_snapshot_is_read_only_and_templates_are_quarantined(project):
    remote, legacy = RESTRepository(), initial_snapshot(project)
    legacy["schema_version"] = 1
    legacy["records"]["templates:personal"] = {"id": "personal", "body": "secret personal format"}
    remote.put("ai-system/project-one", ".ai-dev/shared.json", json.dumps(legacy))
    gh = remote.client()
    actual, versions = gh.snapshot(project)
    assert "templates:personal" not in actual["records"]
    assert all(token["legacy"] for token in versions.values())
    assert not any(method in {"PUT", "POST", "PATCH"} for method, _, _ in remote.calls)
    gh.publish_snapshot(project, actual, versions)
    assert json.loads(remote.files("ai-system/project-one")[".ai-dev/shared.json"]) == legacy
    migrated, versions = gh.snapshot(project)
    assert migrated["records"] == actual["records"]
    assert all(not token.get("legacy") for token in versions.values())


def test_incomplete_migration_retains_legacy_and_overlays_new_records(project):
    remote, legacy = RESTRepository(), initial_snapshot(project)
    remote.put("ai-system/project-one", ".ai-dev/shared.json", json.dumps(legacy))
    task = dict(legacy["records"]["tasks:one"], state="running")
    remote.put("ai-data/project-one/tasks/one", ".ai-dev/record.json", json.dumps(task))
    actual, _ = remote.client().snapshot(project)
    assert actual["records"]["tasks:one"]["state"] == "running"
    assert actual["records"]["tasks:two"]["state"] == "ready"


def test_snapshot_never_publishes_personal_template_records(project):
    remote, initial = RESTRepository(), initial_snapshot(project)
    initial["records"]["templates:personal"] = {"id": "personal"}
    with pytest.raises(ValueError):
        remote.client().publish_snapshot(project, initial)
    assert not any("templates" in branch for branch in remote.refs)


def test_publish_files_pr_is_clean_idempotent_and_fails_on_concurrent_head(project):
    remote, branch = RESTRepository(), "ai-templates/registration-one"
    gh = remote.client()
    result = gh.publish_files_pr(project, branch, {".ai-dev/templates/requirements.md": "# Requirements"}, "Templates", "Review templates")
    head = remote.refs[branch]
    assert result["sha"] == head and len(remote.prs) == 1
    assert set(remote.files(branch)) == {"README.md", ".ai-dev/templates/requirements.md"}
    assert gh.publish_files_pr(project, branch, {".ai-dev/templates/requirements.md": "# Requirements"}, "Templates", "Review templates") == result
    assert len(remote.prs) == 1 and remote.refs[branch] == head
    remote.before_advance = lambda target: remote.put(target, "concurrent.txt", "preserve me")
    with pytest.raises(Conflict):
        gh.publish_files_pr(project, branch, {".ai-dev/templates/requirements.md": "# Changed"}, "Templates", "Review templates")
    assert remote.files(branch)["concurrent.txt"] == "preserve me"
    assert remote.files(branch)[".ai-dev/templates/requirements.md"] == "# Requirements"


def test_large_content_uses_git_blob_fallback():
    requests = []
    def server(req):
        requests.append(req.url.path)
        if "/contents/" in req.url.path:
            return httpx.Response(200, json={"encoding": "none", "content": "", "sha": "blob-sha"})
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode("large content".encode()).decode(), "sha": "blob-sha"})
    assert GitHub(transport=httpx.MockTransport(server)).content("owner/repo", "large.md", "main") == ("large content", "blob-sha")
    assert requests[-1] == "/repos/owner/repo/git/blobs/blob-sha"


def test_artifact_branches_from_two_claims_merge_without_owner_conflict(tmp_path, project):
    """Materialize REST-created branch contents and perform actual Git merges."""
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    remote = RESTRepository()
    gh = remote.client()
    for task_id in ("one", "two"):
        task = {"id": task_id, "title": task_id}
        branch, _, actual = gh.claim(project, task, owner(task_id=task_id))
        task.update(branch=branch, owner=actual)
        gh.publish_task(project, task, {"docs/" + task_id + ".md": "# " + task_id + "\n"})
    def git(*args):
        result = subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout.strip()
    git("init", "-b", "main")
    git("config", "user.name", "Local integration test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    for task_id in ("one", "two"):
        branch = "ai-work/" + task_id
        git("checkout", "-b", branch, "main")
        for filename, value in remote.files(branch).items():
            target = tmp_path / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value, encoding="utf-8")
        git("add", ".")
        git("commit", "-m", task_id)
    git("checkout", "main")
    git("merge", "--no-ff", "ai-work/one", "-m", "Merge first")
    git("merge", "--no-ff", "ai-work/two", "-m", "Merge second")
    files = git("ls-files").splitlines()
    assert files == ["README.md", "docs/one.md", "docs/two.md"]


def test_paginated_reviews():
    def server(req):
        page = req.url.params["page"]
        return httpx.Response(200, json=[{"id": i} for i in range(100)] if page == "1" else [{"id": 101}])
    gh = GitHub(transport=httpx.MockTransport(server))
    assert len(gh.pages("/repos/o/r/pulls/1/reviews")) == 101
