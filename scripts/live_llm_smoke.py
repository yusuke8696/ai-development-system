"""Opt-in integration check against the user's running llama-server."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_system.service import Service

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".runtime/live-llm")
s = Service(root)
p = s.create_project({"name": "LLM接続受入試験", "workflow": "waterfall", "intent":
    "社内担当者1名が自分のPCで使うメモ管理ツール。Python標準ライブラリのCLIとする。"
    "メモの追加・一覧表示・削除が必要。UTF-8 JSONファイルに保存。外部通信、認証、公開配布は不要。"
    "削除前に確認する。受入条件は追加後の再起動で残ることと、削除を取消すと残ること。"
    "初版は上記だけを対象とし、追加情報がなくても要件定義を作成してください。"})
t = p["tasks"][0]
started = time.monotonic()
s.start(t["id"], {})
s.jobs[t["id"]].join(240)
for attempt in range(2):
    current = s.store.get("tasks", t["id"])
    if current["state"] != "waiting_input":
        break
    s.answer(p["id"], {"task_id": t["id"], "expected_version": current["version"], "body":
        "テスト仕様を補足します。保存先は実行ディレクトリのnotes.json。メモはID・本文のみ。本文1000文字以内、最大100件。"
        "IDは自動連番。日本語CLIで動くWindowsローカルアプリ。検索・編集・同期・認証は対象外。"
        "日時は不要。文字列の空入力はエラー、保存失敗はエラー表示。その他の軽微な詳細は合理的な仮定として明記してください。"})
    s.start(t["id"], {"instructions": "回答を反映して要件定義書を作成してください。追加の必須質問はありません。"})
    s.jobs[t["id"]].join(240)
t = s.store.get("tasks", t["id"])
artifacts = s.store.list("artifacts")
report = {"model": "qwythos-9B-v2", "elapsed_seconds": round(time.monotonic() - started, 2),
          "task_state": t["state"], "artifact_count": len(artifacts), "error": t.get("error"),
          "validation_errors": artifacts[-1]["errors"] if artifacts else [],
          "questions": [x["body"] for x in s.store.list("dialogue")]}
root.mkdir(parents=True, exist_ok=True)
(root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
print(json.dumps(report, ensure_ascii=True))
if artifacts:
    (root / "generated-requirements.md").write_text(artifacts[-1]["body"], "utf-8")
assert t["state"] == "waiting_approval" and artifacts and not artifacts[-1]["errors"], "Live generation failed"
