import base64
import json
import re
import time
import hashlib
import threading
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
            if self._cooldown.get(self.identity, 0) > time.time():
                raise ValueError("GitHub APIの待機時間中です。後で同期してください。")
            cached = self._cache.get(key) if method == "GET" else None
            headers = {"If-None-Match": cached[0]} if cached else {}
            response = self.client.request(method, path, json=body, headers=headers)
            if response.status_code == 304 and cached:
                return cached[1]
            if response.status_code in (403, 429):
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
        if response.status_code in (403, 429):
            raise ValueError("GitHub権限またはAPI制限を確認してください。次回取得は待機します。")
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

    def publish_snapshot(self, project, snapshot, expected_sha=None):
        """A dedicated shared branch uses Contents API SHA checks, never main."""
        repo = project["repo"]
        branch = "ai-system/" + project["id"]
        try:
            head = self.ref(repo, branch)
        except KeyError:
            head = self.ref(repo, project["base_branch"])
            self.create_ref(repo, branch, head)
        payload = {"message": "Update shared development plan",
                   "branch": branch, "content": base64.b64encode(json.dumps(snapshot, ensure_ascii=False, indent=2).encode()).decode()}
        if expected_sha:
            payload["sha"] = expected_sha
        return self.request("PUT", f"/repos/{repo}/contents/.ai-dev/shared.json", payload)["content"]["sha"]

    def snapshot(self, project):
        text, sha = self.content(project["repo"], ".ai-dev/shared.json", "ai-system/" + project["id"])
        return json.loads(text), sha

    def discover(self, repo):
        refs = self.request("GET", f"/repos/{repo}/git/matching-refs/heads/ai-system/")
        return [r["ref"].removeprefix("refs/heads/ai-system/") for r in refs]

    def claim(self, project, task, owner):
        branch = "ai-work/" + task["id"]
        parent = self.ref(project["repo"], project["base_branch"])
        sha = self.create_commit(project["repo"], parent,
                                 {".ai-dev/owner.json": json.dumps(owner)}, "Claim task " + task["title"])
        self.create_ref(project["repo"], branch, sha)
        return branch, sha

    def check_owner(self, project, task):
        raw, _ = self.content(project["repo"], ".ai-dev/owner.json", task["branch"])
        if json.loads(raw) != task["owner"]:
            raise Conflict("作業所有が変更されています。公開を停止しました。")

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
        reviews = self.pages(f"/repos/{repo}/pulls/{number}/reviews")
        return pr, reviews

    def checks_pass(self, repo, sha):
        runs = self.request("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
        if runs.get("total_count", 0) > 100:
            return False
        status = self.request("GET", f"/repos/{repo}/commits/{sha}/status")
        return all(r["status"] == "completed" and r.get("conclusion") in ("success", "neutral", "skipped")
                   for r in runs.get("check_runs", [])) and all(s["state"] == "success" for s in status.get("statuses", []))
