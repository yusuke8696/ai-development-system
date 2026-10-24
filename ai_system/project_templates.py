"""Leader-approved project formats, isolated from this PC's personal formats."""
import hashlib
import json
import re
import uuid

from .store import Conflict, now
from .templates import template_id, validate_template
from .workflow import evaluate_reviews


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def template_payload(item):
    """Hash only semantic fields. Store versions and prior hashes never enter it."""
    payload = {k: item[k] for k in ("id", "name", "document_type", "body")}
    payload.update(required_headings=item.get("required_headings", []),
                   required_tables=item.get("required_tables", []),
                   heading_order_required=item.get("heading_order_required", True))
    validate_template(dict(payload))
    return payload


class ProjectTemplatesMixin:
    def _required_formats(self, project):
        """Keep a task's source ID stable when its selected project format differs."""
        result = {}
        prior = project.get("template_preparation", {})
        for task in self.tasks(project["id"]):
            if task["kind"] in {"implementation", "leader_review"}:
                continue
            source = task.get("template_id", task["kind"].replace("_", "-"))
            kind = task.get("template_document_type") or task["kind"]
            if kind == "documentation":
                selected = prior.get("task_bindings", {}).get(source)
                pinned_kind = next((k for k, v in prior.get("bindings", {}).items() if v == selected), None)
                if pinned_kind:
                    kind = pinned_kind
                else:
                    try:
                        kind = self.store.get("templates", source)["document_type"]
                    except KeyError:
                        kind = source
            row = result.setdefault(kind, {"document_type": kind, "source_ids": [], "titles": []})
            if source not in row["source_ids"]:
                row["source_ids"].append(source)
            if task["title"] not in row["titles"]:
                row["titles"].append(task["title"])
        return list(result.values())

    @staticmethod
    def _cache_id(project, source, commit):
        return digest([project["repo"].lower(), project["id"], source, commit])

    @staticmethod
    def _verification_id(project, commit):
        return digest([project["repo"].lower(), project["id"], commit])

    @staticmethod
    def _pin(preparation):
        return {k: preparation.get(k) for k in ("pr", "commit", "lock_hash", "bindings", "task_bindings", "templates", "leader_id")}

    def _verified_preparation(self, project):
        p = project.get("template_preparation", {})
        if not p.get("ready") or not p.get("commit"):
            raise Conflict("開発準備で書式登録PRを作成し、リーダー承認・マージを確認してください。")
        try:
            verified = self.store.get("template_verifications", self._verification_id(project, p["commit"]))
        except KeyError:
            raise Conflict("このPCで書式のGitHub承認を確認してください。")
        if (verified.get("pin_hash") != digest(self._pin(p))
                or verified.get("leader_login", "").lower() != project.get("leader_login", "").lower()
                or verified.get("base_branch") != project.get("base_branch")
                or (project.get("leader_id") is not None and project["leader_id"] != p.get("leader_id"))):
            raise Conflict("書式登録情報が変更されています。GitHub承認を再確認してください。")
        for required in self._required_formats(project):
            selected = p.get("bindings", {}).get(required["document_type"])
            if selected not in p.get("templates", {}) or any(p.get("task_bindings", {}).get(s) != selected for s in required["source_ids"]):
                raise Conflict("追加された文書種別の書式を開発準備で登録してください。")
        for source, meta in p.get("templates", {}).items():
            try:
                cached = self.store.get("project_templates", self._cache_id(project, source, p["commit"]))
                payload = template_payload(dict(cached, id=source))
            except (KeyError, ValueError, TypeError):
                raise Conflict("プロジェクト書式のキャッシュを再取得してください。")
            if digest(payload) != meta["hash"] or cached.get("commit") != p["commit"] or cached.get("repo") != project["repo"] or cached.get("project_id") != project["id"]:
                raise Conflict("プロジェクト書式の整合性を確認できません。GitHub承認を再確認してください。")
        return p

    def template_preparation(self, project_id):
        project = self.store.get("projects", project_id)
        preparation = dict(project.get("template_preparation", {}))
        preparation["required"] = self._required_formats(project)
        try:
            self._verified_preparation(project)
            preparation["ready"] = True
            preparation["reason"] = "リーダー承認済みの固定版を使用します。"
        except Conflict as exc:
            preparation["ready"] = False
            preparation["reason"] = str(exc)
        return preparation

    def ensure_prepared(self, project_id):
        project = self.store.get("projects", project_id)
        try:
            return self._verified_preparation(project)
        except Conflict:
            # A synced ready flag alone is never evidence. New PCs verify the PR.
            if project.get("template_preparation", {}).get("ready"):
                self.confirm_templates(project_id)
                return self._verified_preparation(self.store.get("projects", project_id))
            raise

    def prepare_templates(self, project_id, bindings):
        with self.lock:
            project = self.store.get("projects", project_id)
            if not project.get("leader_login"):
                raise ValueError("先にプロジェクトのリーダーを設定してください。")
            if any(t["state"] in {"running", "cancelling"} for t in self.tasks(project_id)):
                raise Conflict("実行中タスクを停止してから書式を更新してください。")
            required = self._required_formats(project)
            if not isinstance(bindings, dict) or set(bindings) != {r["document_type"] for r in required}:
                raise ValueError("すべての文書種別に書式を指定してください。")
            files, registered, task_bindings = {}, {}, {}
            for row in required:
                source = template_id(bindings[row["document_type"]])
                personal = self.store.get("templates", source)
                payload = template_payload(personal)
                if payload["document_type"] != row["document_type"]:
                    raise ValueError("文書種別に対応するテンプレートを選択してください: " + row["document_type"])
                content_hash = digest(payload)
                folder = f".ai-dev/templates/{project_id}/{source}/{content_hash[:16]}"
                manifest = {k: v for k, v in payload.items() if k != "body"}
                manifest.update(schema_version=1, hash=content_hash, body_sha256=hashlib.sha256(payload["body"].encode()).hexdigest())
                files[folder + "/template.md"] = payload["body"]
                files[folder + "/manifest.json"] = canonical(manifest) + "\n"
                registered[source] = {"path": folder, "hash": content_hash}
                task_bindings.update({s: source for s in row["source_ids"]})
            lock = {"schema_version": 1, "repo": project["repo"], "project_id": project_id,
                    "bindings": bindings, "task_bindings": task_bindings, "templates": registered}
            files[f".ai-dev/template-locks/{project_id}.json"] = canonical(lock) + "\n"
            gh = self.github()
            try:
                leader = gh.user_by_login(project["leader_login"])
                if leader.get("type") != "User":
                    raise ValueError("人間のリーダーを指定してください。")
                branch = f"ai-formats/{project_id}/{uuid.uuid4().hex[:12]}"
                pr = gh.publish_files_pr(project, branch, files, "開発前の文書フォーマット登録: " + project["name"],
                    "全員が使用するMarkdown書式を登録します。指定リーダーは内容をレビュー・承認してマージしてください。\n\nProject: " + project_id)
                preparation = {"ready": False, "pr": pr, "lock_hash": digest(lock), "bindings": bindings,
                               "task_bindings": task_bindings, "templates": registered, "leader_id": leader["id"]}
                self.store.change("projects", project_id, template_preparation=preparation)
            finally:
                gh.close()
            return self.template_preparation(project_id)

    def confirm_templates(self, project_id):
        with self.lock:
            project = self.store.get("projects", project_id)
            preparation = project.get("template_preparation", {})
            if not preparation.get("pr", {}).get("number"):
                raise ValueError("先に書式登録PRを作成してください。")
            gh = self.github()
            try:
                leader = gh.user_by_login(project.get("leader_login", ""))
                if leader.get("type") != "User":
                    raise ValueError("人間のリーダーを指定してください。")
                pr, reviews = gh.pr_reviews(project["repo"], preparation["pr"]["number"])
                approved, evidence = evaluate_reviews(pr, reviews, leader["id"])
                if not approved:
                    raise Conflict(str(evidence))
                if not pr.get("merged"):
                    raise Conflict("書式登録PRはリーダー承認後のマージ待ちです。")
                head, base = pr.get("head", {}), pr.get("base", {})
                if head.get("sha") != preparation["pr"].get("sha") or base.get("ref") != project["base_branch"] or base.get("repo", {}).get("full_name", "").lower() != project["repo"].lower() or head.get("repo", {}).get("full_name", "").lower() != project["repo"].lower():
                    raise Conflict("書式登録PRの対象版または対象リポジトリが異なります。登録し直してください。")
                commit = pr.get("merge_commit_sha", "")
                if not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise Conflict("マージ済み固定コミットを取得できません。")
                raw, _ = gh.content(project["repo"], f".ai-dev/template-locks/{project_id}.json", commit)
                lock = json.loads(raw)
                if lock.get("schema_version") != 1 or lock.get("project_id") != project_id or lock.get("repo") != project["repo"] or digest(lock) != preparation.get("lock_hash"):
                    raise Conflict("マージされた書式設定が登録PRの内容と一致しません。")
                cached = []
                for source, meta in lock["templates"].items():
                    template_id(source)
                    expected_path = f".ai-dev/templates/{project_id}/{source}/{meta['hash'][:16]}"
                    if meta.get("path") != expected_path:
                        raise Conflict("登録書式の保存先が不正です。")
                    manifest_raw, _ = gh.content(project["repo"], expected_path + "/manifest.json", commit)
                    body, _ = gh.content(project["repo"], expected_path + "/template.md", commit)
                    manifest = json.loads(manifest_raw)
                    payload = template_payload(dict(manifest, body=body))
                    if payload["id"] != source or digest(payload) != meta["hash"] or manifest.get("hash") != meta["hash"] or manifest.get("body_sha256") != hashlib.sha256(body.encode()).hexdigest():
                        raise Conflict("マージされたMarkdown書式のハッシュが一致しません。")
                    cached.append(dict(payload, id=self._cache_id(project, source, commit), source_id=source,
                                       project_id=project_id, repo=project["repo"], commit=commit, hash=meta["hash"]))
                for row in self._required_formats(project):
                    selected = lock["bindings"].get(row["document_type"])
                    if selected not in lock["templates"] or any(lock["task_bindings"].get(s) != selected for s in row["source_ids"]):
                        raise Conflict("必要な文書種別の書式が未登録です。書式登録をやり直してください。")
                    if next(c for c in cached if c["source_id"] == selected)["document_type"] != row["document_type"]:
                        raise Conflict("登録書式の文書種別が一致しません。")
                for record in cached:
                    try:
                        old = self.store.get("project_templates", record["id"])
                    except KeyError:
                        old = None
                    self.store.put("project_templates", record, old["version"] if old else None)
                pinned = dict(preparation, **{k: lock[k] for k in ("bindings", "task_bindings", "templates")},
                              ready=True, commit=commit, leader_id=leader["id"], review_id=evidence["id"])
                pinned.pop("confirmed_at", None)  # Confirmation time belongs to this PC's verification cache.
                verification = {"id": self._verification_id(project, commit), "project_id": project_id,
                                "repo": project["repo"], "commit": commit, "pin_hash": digest(self._pin(pinned)),
                                "leader_login": leader["login"], "base_branch": project["base_branch"], "verified_at": now()}
                try:
                    prior = self.store.get("template_verifications", verification["id"])
                except KeyError:
                    prior = None
                self.store.put("template_verifications", verification, prior["version"] if prior else None)
                self.store.change("projects", project_id, template_preparation=pinned)
            finally:
                gh.close()
            return self.template_preparation(project_id)

    def project_template(self, project_id, source_template_id):
        preparation = self.ensure_prepared(project_id)
        project = self.store.get("projects", project_id)
        source = preparation.get("task_bindings", {}).get(source_template_id)
        if source is None:
            raise Conflict("この書式はプロジェクトに未登録です。開発準備で書式を追加してください。")
        cached = self.store.get("project_templates", self._cache_id(project, source, preparation["commit"]))
        return dict(cached, id=source, source_id=source, pinned_commit=preparation["commit"])
