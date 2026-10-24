"""Real llama-server coding and generated-code tests with isolated GitHub fixtures.

GitHub format registration, leader approval and merge are simulated using the
dedicated regression-test helper. Planning is pre-completed by a local fixture.
Production preflight is exercised; this is not a full lifecycle or real human
approval test, and this script performs no real GitHub writes.
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from ai_system.service import Service
from ai_system.store import Conflict
from test_project_templates import FormatGitHub, prepare_verified

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".runtime/live-code")
gh_fixture = FormatGitHub()
s = Service(root, github_factory=lambda: gh_fixture)
p = s.create_project({"name": "コード生成受入試験", "leader_login": "leader", "intent":
    "Python標準ライブラリだけを使う計算モジュール。calc.pyのadd(a,b)は2つの数値の和を返す。"
    "tests/test_calc.pyはunittestで正数・負数・ゼロを検証する。GUI・外部通信・追加機能不要。"})
try:
    s.start(s.tasks(p["id"])[0]["id"], {})
    raise AssertionError("Unprepared project was allowed to start")
except Conflict:
    pass
preflight = prepare_verified(s, p, gh_fixture)
for t in s.tasks(p["id"]):
    if t["kind"] in {"requirements", "architecture", "design"}:
        s.store.change("tasks", t["id"], state="succeeded")
s.refresh_ready(p["id"])
t = next(t for t in s.tasks(p["id"]) if t["kind"] == "implementation")
if not t["artifacts"]:
    s.start(t["id"], {"instructions": "仕様は確定済み。calc.pyとtests/test_calc.pyを生成してください。質問は不要です。"})
    s.jobs[t["id"]].join(240)
t = s.store.get("tasks", t["id"])
artifacts = s.store.list("artifacts")
report = {"model": "qwythos-9B-v2", "task_state": t["state"], "error": t.get("error"),
          "scope": "real local llama-server coding and generated unittest; simulated GitHub registration/leader approval/merge and pre-completed planning, not a full lifecycle",
          "preflight": {"source": "FormatGitHub + prepare_verified test fixture", "ready": preflight["ready"],
                        "pinned_commit": preflight["commit"], "review_id": preflight["review_id"], "real_github_mutations": False}}
if artifacts:
    s.store.change("settings", "local", python=sys.executable, allow_commands=True)
    history = []
    for attempt in range(3):
        result = s.test_command(t["id"], "python-unittest")
        history.append({k: result[k] for k in ["exit_code", "stdout", "stderr"]})
        if result["exit_code"] == 0:
            break
        if attempt == 2:
            break
        current = s.store.get("tasks", t["id"])
        s.answer(p["id"], {"task_id": t["id"], "expected_version": current["version"], "body":
            "作業ルートは . です。tests/の親がルートです。calc.pyはルート直下に配置してください。"
            "tests/test_calc.pyはfrom calc import addを使います。コマンドはルートでpython -m unittest discover -s tests -v。"
            "src/calc.pyは以後使いません。追加の判断は不要です。"})
        s.start(t["id"], {"instructions": "回答とテスト失敗を反映し、ファイルパスcalc.pyとtests/test_calc.pyの2ファイルを生成してください。テストを削除・無効化しないでください。"})
        s.jobs[t["id"]].join(240)
    artifacts = s.store.list("artifacts")
    t = s.store.get("tasks", t["id"])
    report["task_state"] = t["state"]
    report["files"] = [f["path"] for f in artifacts[-1]["files"]]
    report["test"] = history[-1]
    report["test_history"] = history
root.mkdir(parents=True, exist_ok=True)
(root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
print(json.dumps(report, ensure_ascii=True))
s.close()
assert t["state"] == "waiting_approval" and report.get("test", {}).get("exit_code") == 0
