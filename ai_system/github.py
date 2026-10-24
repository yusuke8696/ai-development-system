import base64
import copy
import json
import re
import time
import hashlib
import threading
import math
from datetime import datetime
from urllib.parse import quote
import httpx
from .store import Conflict


def repository(url):
    match = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", url.strip())
    if not match:
        raise ValueError("GitHub.comのリポジトリURL（owner/repo）を指定してください。")
    return "/".join(match.groups())


class GitHub:
    _cache = {}
    _cooldown = {}
    _request_lock = threading.RLock()

    def __init__(self, token="", transport=None):
        self.identity = hashlib.sha256(token.encode()).hexdigest()
        self.client = httpx.Client(base_url="https://api.github.com", timeout=30, transport=transport,
                                   headers={"Accept": "application/vnd.github+json", "User-Agent": "AIWorkspace/0.1",
                                            **({"Authorization": "Bearer " + token} if token else {})})

    def close(self):
        self.client.close()

    def request(self, method, path, body=None):
        key = (self.identity, path)
        with self._request_lock:
            until = self._cooldown.get(self.identity, 0)
            if until > time.time():
                remaining = math.ceil(until - time.time())
                retry_at = datetime.fromtimestamp(until).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                raise ValueError(f"GitHub APIの利用制限で待機中です。{remaining}秒後（{retry_at}）に再実行してください。")
            cached = self._cache.get(key) if method == "GET" else None
            headers = {"If-None-Match": cached[0]} if cached else {}
            response = self.client.request(method, path, json=body, headers=headers)
            if response.status_code == 304 and cached:
                return cached[1]
            limited = response.status_code == 429 or (response.status_code == 403 and
                (response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("retry-after")))
            if response.status_code == 403 and not limited:
                try:
                    message = str(response.json().get("message", "")).lower()
                    limited = "secondary rate limit" in message or "abuse detection" in message
                except (ValueError, AttributeError):
                    pass
            if limited:
                delay = 60
                try:
                    delay = max(delay, int(response.headers.get("retry-after", 0)),
                                int(response.headers.get("x-ratelimit-reset", 0)) - int(time.time())
                                if response.headers.get("x-ratelimit-remaining") == "0" else 0)
                except ValueError:
                    pass
                self._cooldown[self.identity] = time.time() + delay
            if method == "GET" and response.status_code == 200 and response.headers.get("etag"):
                if len(self._cache) > 2048:
                    self._cache.clear()
                self._cache[key] = (response.headers["etag"], response.json())
            if method != "GET" and response.is_success:
                self._cache.clear()
        if response.status_code in (409, 422):
            raise Conflict("GitHubで競合または入力エラー。状態を再取得してください。")
        if limited:
            raise ValueError(f"GitHub APIの利用制限に達しました。少なくとも{delay}秒待って再実行してください。")
        if response.status_code == 403:
            raise ValueError("GitHubが操作を拒否しました（HTTP 403）。トークンの対象リポジトリ・権限・組織の承認を確認してください。書式・OSS設定の登録にはContents・Pull requests・Workflowsの書き込み権限が必要です。")
        if response.status_code == 401:
            raise ValueError("GitHubの認証を設定してください。")
        if response.status_code == 404:
            raise KeyError("GitHub対象が見つからないか、権限がありません。")
        if response.is_error:
            raise ValueError("GitHub APIエラー: " + str(response.status_code))
        return response.json() if response.content else {}

    def pages(self, path):
        result = []
        sep = "&" if "?" in path else "?"
        for page in range(1, 101):
            items = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("取得上限を超えました。対象を絞ってください。")

    def user(self):
        return self.request("GET", "/user")

    def meta(self, repo):
        return self.request("GET", f"/repos/{repo}")

    def user_by_login(self, login):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            raise ValueError("リーダーのGitHubユーザー名を指定してください。")
        return self.request("GET", "/users/" + login)

    def content(self, repo, path, ref):
        item = self.request("GET", f"/repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
        if item.get("encoding") == "none":
            item = self.request("GET", f"/repos/{repo}/git/blobs/{item['sha']}")
        return base64.b64decode(item["content"]).decode("utf-8"), item["sha"]

    def create_commit(self, repo, parent, files, message):
        old = self.request("GET", f"/repos/{repo}/git/commits/{parent}")
        tree = self.request("POST", f"/repos/{repo}/git/trees", {
            "base_tree": old["tree"]["sha"],
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}
                     for path, content in files.items()]})
        return self.request("POST", f"/repos/{repo}/git/commits", {
            "message": message, "tree": tree["sha"], "parents": [parent]})["sha"]

    def ref(self, repo, branch):
        return self.request("GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")["object"]["sha"]

    def create_ref(self, repo, branch, sha):
        return self.request("POST", f"/repos/{repo}/git/refs", {"ref": "refs/heads/" + branch, "sha": sha})

    def advance(self, repo, branch, sha):
        return self.request("PATCH", f"/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}", {"sha": sha, "force": False})

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("共有レコードのIDが不正です。")
        return value

    def _record_location(self, project_id, key):
        project_id = self._identifier(project_id)
        if key == "project":
            return "ai-system/" + project_id, ".ai-dev/project.json"
        kind, separator, record_id = key.partition(":")
        if not separator or kind not in {"tasks", "artifacts", "dialogue", "test_runs"}:
            raise ValueError("共有レコードの種別が不正です。")
        return f"ai-data/{project_id}/{kind}/{self._identifier(record_id)}", ".ai-dev/record.json"

    def _read_record(self, repo, branch, path):
        try:
            raw, sha = self.content(repo, path, branch)
        except KeyError:
            return None, None
        return json.loads(raw), sha

    def _ensure_branch(self, project, branch):
        repo = project["repo"]
        try:
            return self.ref(repo, branch)
        except KeyError:
            parent = self.ref(repo, project["base_branch"])
        try:
            self.create_ref(repo, branch, parent)
            return parent
        except (Conflict, httpx.TransportError):
            # A competing creator or a lost response must never reset its ref.
            return self.ref(repo, branch)

    def publish_snapshot(self, project, snapshot, expected_sha=None):
        """Publish only changed records, each with its own Contents API CAS.

        The opaque token includes each record's baseline as well as its blob SHA.
        A stale unchanged record is skipped, so independent task owners never
        write each other's status. A conflicting change to one record is refused.
        Partial success is safe to retry after a transport error or conflict.
        """
        versions = copy.deepcopy(expected_sha) if isinstance(expected_sha, dict) else {}
        records = snapshot["records"]
        repo = project["repo"]
        # Publish the project last: legacy readers retain a complete old snapshot
        # until the new per-record representation has been created successfully.
        for key in sorted(records, key=lambda item: (item == "project", item)):
            value = records[key]
            branch, path = self._record_location(project["id"], key)
            expected = versions.get(key)
            if expected and not expected.get("legacy") and value == expected["record"]:
                continue
            current, sha = self._read_record(repo, branch, path)
            if current == value and sha:
                versions[key] = {"sha": sha, "record": copy.deepcopy(value)}
                continue
            if sha and (not expected or current != expected["record"] or sha != expected.get("sha")):
                raise Conflict("同じ共有レコードが変更されています: " + key)
            if not sha and expected and not expected.get("legacy"):
                raise Conflict("共有レコードが削除されています: " + key)
            self._ensure_branch(project, branch)
            payload = {"message": "Update development record " + key, "branch": branch,
                       "content": base64.b64encode(json.dumps(value, ensure_ascii=False, indent=2).encode()).decode()}
            if sha:
                payload["sha"] = sha
            try:
                result = self.request("PUT", f"/repos/{repo}/contents/{path}", payload)
                new_sha = result["content"]["sha"]
            except (Conflict, httpx.TransportError):
                current, new_sha = self._read_record(repo, branch, path)
                if current != value or not new_sha:
                    raise Conflict("共有レコードの公開が競合しました: " + key) from None
            versions[key] = {"sha": new_sha, "record": copy.deepcopy(value)}
        return {key: value for key, value in versions.items() if key in records}

    def snapshot(self, project):
        repo, project_id = project["repo"], self._identifier(project["id"])
        records, versions = {}, {}
        branch, path = self._record_location(project_id, "project")
        value, sha = self._read_record(repo, branch, path)
        if sha:
            records["project"] = value
            versions["project"] = {"sha": sha, "record": copy.deepcopy(value)}
        else:
            # Migration reads are deliberately read-only. Legacy templates are
            # quarantined rather than imported into anyone's personal library.
            try:
                raw, _ = self.content(repo, ".ai-dev/shared.json", branch)
                legacy = json.loads(raw)
            except KeyError:
                legacy = {"records": {}}
            for key, record in legacy.get("records", {}).items():
                if key != "project" and key.partition(":")[0] not in {"tasks", "artifacts", "dialogue", "test_runs"}:
                    continue
                self._record_location(project_id, key)
                records[key] = record
                versions[key] = {"sha": None, "record": copy.deepcopy(record), "legacy": True}
        refs = self.request("GET", f"/repos/{repo}/git/matching-refs/heads/ai-data/{project_id}/")
        prefix = "refs/heads/ai-data/" + project_id + "/"
        for entry in refs:
            ref = entry["ref"]
            if not ref.startswith(prefix):
                continue
            parts = ref.removeprefix(prefix).split("/")
            if len(parts) != 2 or parts[0] not in {"tasks", "artifacts", "dialogue", "test_runs"}:
                continue
            key = parts[0] + ":" + parts[1]
            self._record_location(project_id, key)
            # Use the enumerated immutable commit to avoid mixing a blob and a
            # version from different updates of the same record.
            value, sha = self._read_record(repo, entry["object"]["sha"], ".ai-dev/record.json")
            if sha:
                records[key] = value
                versions[key] = {"sha": sha, "record": copy.deepcopy(value)}
        if "project" not in records:
            raise KeyError("共有プロジェクトが見つかりません。")
        return {"schema_version": 2, "records": records}, versions

    def discover(self, repo):
        refs = self.request("GET", f"/repos/{repo}/git/matching-refs/heads/ai-system/")
        return [r["ref"].removeprefix("refs/heads/ai-system/") for r in refs]

    def claim(self, project, task, owner):
        task_id = self._identifier(task["id"])
        repo, branch, control = project["repo"], "ai-work/" + task_id, "ai-control/" + task_id
        actual, _ = self._read_record(repo, control, ".ai-dev/owner.json")
        if actual is None:
            try:
                self.ref(repo, branch)
            except KeyError:
                pass
            else:
                # Existing legacy work must never be stolen when migrating.
                actual, _ = self._read_record(repo, branch, ".ai-dev/owner.json")
                if actual is None or not self._same_claim(actual, owner, task_id):
                    raise Conflict("既存ブランチの作業所有を確認できません。")
            actual = actual or owner
            parent = self.ref(repo, project["base_branch"])
            sha = self.create_commit(repo, parent, {".ai-dev/owner.json": json.dumps(actual)}, "Claim task " + task["title"])
            try:
                self.create_ref(repo, control, sha)
            except (Conflict, httpx.TransportError):
                actual, _ = self._read_record(repo, control, ".ai-dev/owner.json")
                if actual is None:
                    raise
        if not self._same_claim(actual, owner, task_id):
            raise Conflict("別の利用者または端末が担当しています。")
        sha = self._ensure_branch(project, branch)
        self.check_owner(project, dict(task, branch=branch, owner=actual))
        return branch, sha, actual

    @staticmethod
    def _same_claim(actual, requested, task_id):
        fields = ("user_id", "device_id", "task_id")
        return (all(field in actual and field in requested for field in fields)
                and actual["task_id"] == task_id
                and all(actual[field] == requested[field] for field in fields))

    def check_owner(self, project, task):
        actual, _ = self._read_record(project["repo"], "ai-control/" + self._identifier(task["id"]), ".ai-dev/owner.json")
        if actual is None:
            actual, _ = self._read_record(project["repo"], task["branch"], ".ai-dev/owner.json")
        if actual is None or actual != task["owner"]:
            raise Conflict("作業所有が変更されています。公開を停止しました。")

    def transfer_owner(self, project, task, new_owner):
        self.check_owner(project, task)
        repo, control = project["repo"], "ai-control/" + self._identifier(task["id"])
        actual, sha = self._read_record(repo, control, ".ai-dev/owner.json")
        if actual is None:
            # A legacy task is migrated by a create-only ref, without changing
            # its artifact branch or adding any ownership file to future PRs.
            parent = self.ref(repo, project["base_branch"])
            commit = self.create_commit(repo, parent, {".ai-dev/owner.json": json.dumps(new_owner)}, "Handover task")
            try:
                self.create_ref(repo, control, commit)
            except (Conflict, httpx.TransportError):
                actual, _ = self._read_record(repo, control, ".ai-dev/owner.json")
                if actual != new_owner:
                    raise Conflict("引継ぎ中に作業所有が変更されました。") from None
        else:
            if actual != task["owner"]:
                raise Conflict("引継ぎ中に作業所有が変更されました。")
            payload = {"message": "Handover task", "branch": control, "sha": sha,
                       "content": base64.b64encode(json.dumps(new_owner).encode()).decode()}
            try:
                self.request("PUT", f"/repos/{repo}/contents/.ai-dev/owner.json", payload)
            except (Conflict, httpx.TransportError):
                actual, _ = self._read_record(repo, control, ".ai-dev/owner.json")
                if actual != new_owner:
                    raise Conflict("引継ぎ中に作業所有が変更されました。") from None
        self.check_owner(project, dict(task, owner=new_owner))
        return task["branch"], self.ref(repo, task["branch"]), new_owner

    def publish_files_pr(self, project, branch, files, title, body):
        repo = project["repo"]
        if branch == project["base_branch"] or branch.startswith(("ai-system/", "ai-data/", "ai-control/")):
            raise ValueError("成果物用の作業ブランチを指定してください。")
        if not files or any(path in {".ai-dev/owner.json", ".ai-dev/shared.json", ".ai-dev/record.json"} for path in files):
            raise ValueError("公開する成果物ファイルを指定してください。")
        parent = self._ensure_branch(project, branch)
        changed = {}
        for path, value in files.items():
            try:
                current, _ = self.content(repo, path, parent)
            except KeyError:
                current = None
            if current != value:
                changed[path] = value
        sha = parent
        if changed:
            sha = self.create_commit(repo, parent, changed, title)
            try:
                self.advance(repo, branch, sha)
            except (Conflict, httpx.TransportError):
                if self.ref(repo, branch) != sha:
                    raise Conflict("成果物ブランチが変更されました。再取得してください。") from None
        # The PR's head must still be the commit whose contents we checked.
        if self.ref(repo, branch) != sha:
            raise Conflict("成果物ブランチが変更されました。再取得してください。")
        prs = self.request("GET", f"/repos/{repo}/pulls?state=open&head={quote(repo.split('/')[0] + ':' + branch, safe='')}")
        pr = prs[0] if prs else self.request("POST", f"/repos/{repo}/pulls", {
            "title": title, "head": branch, "base": project["base_branch"], "body": body})
        if self.ref(repo, branch) != sha:
            raise Conflict("PR作成中に成果物が変更されました。再取得してください。")
        return {"number": pr["number"], "url": pr["html_url"], "sha": sha}

    def publish_task(self, project, task, files):
        repo, branch = project["repo"], task["branch"]
        self.check_owner(project, task)
        parent = self.ref(repo, branch)
        sha = self.create_commit(repo, parent, files, "Generate " + task["title"])
        self.check_owner(project, task)
        self.advance(repo, branch, sha)
        prs = self.request("GET", f"/repos/{repo}/pulls?state=open&head={quote(repo.split('/')[0] + ':' + branch, safe='')}")
        if prs:
            pr = prs[0]
        else:
            pr = self.request("POST", f"/repos/{repo}/pulls", {
                "title": task["title"], "head": branch, "base": project["base_branch"],
                "body": "AI生成成果物です。内容・検証結果をレビューしてください。\n\nTask: " + task["id"]})
        return {"number": pr["number"], "url": pr["html_url"], "sha": sha}

    def issue(self, project, task):
        marker = "<!-- ai-task:" + task["id"] + " -->"
        for issue in self.pages(f"/repos/{project['repo']}/issues?state=all"):
            if marker in (issue.get("body") or ""):
                return issue
        payload = {"title": task["title"], "body": marker + "\n\n本システムで管理するタスクです。\n状態: " + task["state"]}
        if task["kind"] == "leader_review" and project.get("leader_login"):
            payload["assignees"] = [project["leader_login"]]
        return self.request("POST", f"/repos/{project['repo']}/issues", payload)

    def pr_reviews(self, repo, number):
        pr = self.request("GET", f"/repos/{repo}/pulls/{number}")
        reviews = [] if pr.get("merged") is True else self.pages(f"/repos/{repo}/pulls/{number}/reviews")
        return pr, reviews

    def checks_pass(self, repo, sha):
        runs = self.request("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
        if runs.get("total_count", 0) > 100:
            return False
        status = self.request("GET", f"/repos/{repo}/commits/{sha}/status")
        return all(r["status"] == "completed" and r.get("conclusion") in ("success", "neutral", "skipped")
                   for r in runs.get("check_runs", [])) and all(s["state"] == "success" for s in status.get("statuses", []))
