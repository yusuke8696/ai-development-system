import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from ai_system.store import Store, Conflict
from ai_system.templates import defaults, validate_template, validate_document, render_template, MD
from ai_system.tools import safe_path, write_files, run_command, read_file, search_files
from ai_system.workflow import plan, evaluate_reviews
from ai_system.service import Service, merge_records
from ai_system.llm import clean_json


def test_store_restart_and_event(tmp_path):
    s = Store(tmp_path)
    x = s.put("tasks", {"id": "one", "state": "ready"})
    s.put("tasks", dict(x, state="running"), x["version"])
    with pytest.raises(Conflict):
        s.put("tasks", x, x["version"])
    assert Store(tmp_path).get("tasks", "one")["state"] == "running"
    assert len(s.events()) == 2


def test_atomic_concurrent_updates(tmp_path):
    s = Store(tmp_path)
    a = s.put("tasks", {"id": "one"})
    def update(i):
        try:
            s.put("tasks", dict(a, result=i), a["version"])
            return True
        except Conflict:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(update, [1, 2])) == 1


@pytest.mark.parametrize("workflow,first", [("waterfall", "requirements"), ("agile", "vision"), ("aidlc", "intent")])
def test_workflow_plan(workflow, first):
    tasks = plan("p", workflow)
    assert tasks[0]["kind"] == first
    assert tasks[-1]["kind"] == "leader_review"
    assert sum(t["state"] == "ready" for t in tasks) == 1
    assert len({t["id"] for t in tasks}) == len(tasks)


def test_template_structure_and_variables():
    t = next(defaults())
    text = render_template(t, {"name": "test"}, "要件")
    assert validate_document(text, t) == []
    assert validate_document(text.replace("## 目的", "## other"), t)
    assert validate_document(text + "\n{{unknown}}", t)
    with pytest.raises(ValueError):
        validate_template(dict(t, id="../oops"))
    with pytest.raises(ValueError):
        validate_template(dict(t, body="{{execute.code}}"))


def test_table_validation():
    t = {"required_headings": ["一覧"], "required_tables": [{"under_heading": "一覧", "columns": ["ID", "内容"]}]}
    assert not validate_document("## 1. 一覧\n\n| ID | 内容 |\n|---|---|\n| A | B |", t)
    assert validate_document("## 一覧\n\n| 内容 | ID |\n|---|---|", t)


def test_preview_scripts_inert():
    html = MD.render('<script>alert(1)</script>\n\n[bad](javascript:alert(1))')
    assert "<script>" not in html
    assert 'href="javascript:' not in html


def test_builtin_read_and_search(tmp_path):
    write_files(tmp_path, [{"path": "docs/a.md", "content": "要件\n受入条件\n"}])
    assert "受入条件" in read_file(tmp_path, "docs/a.md")
    assert search_files(tmp_path, "受入")[0]["line"] == 2
    with pytest.raises(ValueError):
        read_file(tmp_path, "../secret")


def test_agent_uses_builtin_read_tool(tmp_path):
    class ToolLLM:
        def __init__(self):
            self.calls = 0
        def complete(self, prompt, system):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"tool_requests": [{"name": "read_file", "path": "context.txt"}]})
            assert "verified context" in prompt
            return json.dumps({"questions": [], "markdown": "# 要件\n\n## 目的\n目的\n\n## 対象範囲\n範囲\n\n## 要件一覧\n要件\n\n## 未決事項\nなし"})
        def close(self):
            pass
    s = Service(tmp_path, llm_factory=lambda _: ToolLLM())
    p = new_project(s)
    t = p["tasks"][0]
    write_files(s.workdir(t), [{"path": "context.txt", "content": "verified context"}])
    s.start(t["id"], {})
    assert wait(s, t["id"])["state"] == "waiting_approval"


def test_agent_cannot_run_unapproved_tool(tmp_path):
    s = Service(tmp_path, llm_factory=lambda _: FakeLLM({"tool_requests": [{"name": "run_command", "argv": ["bad"]}]}))
    p = new_project(s)
    t = p["tasks"][0]
    s.start(t["id"], {})
    assert wait(s, t["id"])["state"] == "failed"


@pytest.mark.parametrize("path", ["../outside", "C:/outside", ".git/config", "a/../../outside", "a\\b", "nul", "/absolute"])
def test_path_escape(tmp_path, path):
    with pytest.raises(ValueError):
        safe_path(tmp_path, path)


def test_write_prevalidates_all_paths(tmp_path):
    with pytest.raises(ValueError):
        write_files(tmp_path, [{"path": "ok.txt", "content": "ok"}, {"path": "../bad", "content": "bad"}])
    assert not (tmp_path / "ok.txt").exists()


def test_three_way_merge():
    base = {"a": {"x": 1}, "b": {"x": 1}}
    assert merge_records(base, dict(base, a={"x": 2}), dict(base, b={"x": 2})) == {"a": {"x": 2}, "b": {"x": 2}}
    with pytest.raises(Conflict):
        merge_records(base, dict(base, a={"x": 2}), dict(base, a={"x": 3}))


def review(state="APPROVED", uid=2, sha="head", rid=1, kind="User"):
    return {"id": rid, "user": {"id": uid, "type": kind}, "state": state, "commit_id": sha,
            "submitted_at": "2026-10-24T00:00:00Z", "html_url": "https://github.com/example/repo/pull/1#review"}


@pytest.fixture
def pr():
    return {"head": {"sha": "head"}, "user": {"id": 1}, "state": "open"}


def test_valid_leader(pr):
    assert evaluate_reviews(pr, [review()], 2)[0]
    assert evaluate_reviews(pr, [review(), review("COMMENTED", rid=2)], 2)[0]


@pytest.mark.parametrize("r", [review(uid=3), review(sha="old"), review("DISMISSED"), review("COMMENTED"), review(kind="Bot")])
def test_invalid_leader(pr, r):
    assert not evaluate_reviews(pr, [r], 2)[0]


def test_change_request_and_dismissal(pr):
    assert not evaluate_reviews(pr, [review(), review("CHANGES_REQUESTED", uid=3, rid=2)], 2)[0]
    assert not evaluate_reviews(pr, [review(), review("DISMISSED", rid=2)], 2)[0]
    assert not evaluate_reviews(pr, [review(uid=1)], 1)[0]
    assert evaluate_reviews(pr, [review("CHANGES_REQUESTED", uid=3), review("APPROVED", uid=3, rid=2), review(rid=3)], 2)[0]


def test_llm_json_parser():
    assert clean_json('<think>reason</think>```json\n{"questions": []}\n```') == {"questions": []}
    with pytest.raises(ValueError):
        clean_json("not json")


class FakeLLM:
    def __init__(self, response):
        self.response = response
    def complete(self, *args):
        return json.dumps(self.response, ensure_ascii=False)
    def close(self):
        pass


def new_project(service, workflow="waterfall"):
    return service.create_project({"name": "テスト", "intent": "テストのための業務ツール", "workflow": workflow})


def wait(service, task_id):
    service.jobs[task_id].join(10)
    assert not service.jobs[task_id].is_alive()
    return service.store.get("tasks", task_id)


def test_question_answer_and_resume(tmp_path):
    s = Service(tmp_path, llm_factory=lambda p: FakeLLM({"questions": ["利用者は誰ですか？"]}))
    p = new_project(s)
    t = p["tasks"][0]
    s.start(t["id"], {})
    t = wait(s, t["id"])
    assert t["state"] == "waiting_input"
    with pytest.raises(Conflict):
        s.answer(p["id"], {"task_id": t["id"], "body": "担当者", "expected_version": 1})
    s.answer(p["id"], {"task_id": t["id"], "body": "担当者", "expected_version": t["version"]})
    s.llm_factory = lambda profile: FakeLLM({"questions": [], "markdown": "# 要件\n\n## 目的\n担当者向け\n\n## 対象範囲\n業務\n\n## 要件一覧\n一覧\n\n## 未決事項\nなし"})
    s.start(t["id"], {})
    t = wait(s, t["id"])
    assert t["state"] == "waiting_approval"
    s.approve(t["id"], {"approve": True, "expected_version": t["version"]})
    assert s.tasks(p["id"])[1]["state"] == "ready"
    assert Service(tmp_path).tasks(p["id"])[0]["state"] == "succeeded"


def test_leader_cannot_be_ui_approved(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    t = p["tasks"][-1]
    with pytest.raises(PermissionError):
        s.approve(t["id"], {"approve": True, "expected_version": t["version"]})
    assert s.project(p["id"])["state"] != "completed"


def test_restart_running_task_blocks(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    s.store.change("tasks", p["tasks"][0]["id"], state="running")
    assert Service(tmp_path).tasks(p["id"])[0]["state"] == "blocked"


def test_document_validation_blocks_approval(tmp_path):
    s = Service(tmp_path, llm_factory=lambda _: FakeLLM({"questions": [], "markdown": "# 不正な書式"}))
    p = new_project(s)
    tid = p["tasks"][0]["id"]
    s.start(tid, {})
    t = wait(s, tid)
    assert t["state"] == "waiting_approval"
    assert s.store.list("artifacts")[0]["errors"]
    with pytest.raises(ValueError):
        s.approve(tid, {"approve": True, "expected_version": t["version"]})


def test_command_requires_explicit_permission(tmp_path):
    s = Service(tmp_path)
    t = new_project(s)["tasks"][0]
    tool = s.store.put("tools", {"transport": "command", "argv": ["python", "-V"]})
    with pytest.raises(PermissionError):
        s.test_command(t["id"], tool["id"])


def test_answer_rejects_second_stale_submission(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    t = p["tasks"][0]
    body = {"task_id": t["id"], "body": "回答", "expected_version": t["version"]}
    s.answer(p["id"], body)
    with pytest.raises(Conflict):
        s.answer(p["id"], body)


def test_parallel_task_added_and_final_invalidated(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    final = p["tasks"][-1]
    s.store.change("tasks", final["id"], state="succeeded", approval_evidence={"test": 1})
    t = s.add_task(p["id"], {"title": "独立実装", "role": "coding", "kind": "implementation", "depends_on": []})
    assert t["state"] == "ready"
    final = s.store.get("tasks", final["id"])
    assert t["id"] in final["depends_on"]
    assert final["state"] == "planned" and not final["approval_evidence"]


def test_verification_needs_evidence(tmp_path):
    s = Service(tmp_path)
    p = new_project(s)
    t = next(t for t in p["tasks"] if t["kind"] == "verification")
    a = s.store.put("artifacts", {"task_id": t["id"], "project_id": p["id"], "errors": []})
    t = s.store.change("tasks", t["id"], state="waiting_approval", artifacts=[a["id"]])
    with pytest.raises(ValueError):
        s.approve(t["id"], {"approve": True, "expected_version": t["version"]})
    assert s.approve(t["id"], {"approve": True, "expected_version": t["version"], "comment": "担当者が手動で検証済み。"})["state"] == "succeeded"


def test_secrets_encrypted_and_not_in_state(tmp_path):
    import os
    if os.name != "nt":
        pytest.skip("DPAPI is Windows-only")
    s = Service(tmp_path)
    s.secrets.set("github", "test-only-secret")
    assert s.secrets.get("github") == "test-only-secret"
    assert "test-only-secret" not in (tmp_path / "secrets.dpapi.json").read_text()
    assert "test-only-secret" not in json.dumps(s.settings())
