"""Read saved settings without modifying the DB; print a sanitized GitHub report."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_system.diagnostics import diagnose_github
from ai_system.github import GitHub
from ai_system.secrets import Secrets


def main():
    parser = argparse.ArgumentParser(description="保存済み設定でGitHub接続を診断します。トークンを表示しません。")
    parser.add_argument("--data-dir", default=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AIWorkspace"))
    args = parser.parse_args()
    root = Path(args.data_dir).resolve()
    try:
        with sqlite3.connect((root / "workspace.db").as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT body FROM records WHERE kind='settings' AND id='local'").fetchone()
            repo = json.loads(row[0])["repo"]
        secrets = Secrets(root)
        report = diagnose_github(repo, lambda token: GitHub(token), lambda: secrets.configured("github"), lambda: secrets.get("github"))
    except Exception:
        report = {"ok": False, "failed_stage": "configuration", "message": "保存済み設定を読み取れませんでした。",
                  "steps": [{"id": "configuration", "name": "保存済み設定", "state": "failed", "code": "settings_unreadable",
                             "message": "保存済み設定を読み取れませんでした。", "action": "アプリと同じWindowsユーザーで実行し、データ保存先を確認してください。"}]}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
