# AI Development Workspace

各自のWindows PCでexeを起動し、ブラウザからAIと対話して開発を進めるシステムです。

実装はレビュー用ブランチ `feature/local-ai-workspace` で管理します。リーダー本人のレビュー・承認を経て統合してください。

## 起動

配布ZIPを展開し、`AIWorkspace/AIWorkspace.exe` を実行します。`_internal` フォルダーを含めて配置してください。ブラウザが開きます。exe自身の起動に外部Pythonは不要です。

初期設定:

1. 「接続・環境設定」でGitHubリポジトリと自分のアクセストークンを設定します。GitHubで事前にリポジトリと基準ブランチを用意してください。
2. 「LLMモデル」で接続テストを行います。初期値は `http://127.0.0.1:8080/v1`、モデルID `qwythos-9B-v2` です。
3. プロジェクトを作成し、開発手法・目的・リーダーのGitHubユーザー名を指定します。
4. ローカル草案は「AIで作成」で生成できます。共同作業する場合は「工程・進捗」でGitHub同期を行い、タスクの担当を取得します。
5. AIの質問に回答し、再開します。文書の検証結果と原文を確認して次へ進めます。
6. 「GitHubへ公開」でタスクブランチとPRを作成します。通常の操作はブラウザUIで行えます。
7. 最後に「最終レビューPR作成」を実行し、指定リーダーがGitHubでApproveします。画面の「承認を確認」または定期同期で完了を判定します。

終了は画面右上の「終了」を使います。ブラウザを閉じただけではexeは停止しません。通常の保存先は `%LOCALAPPDATA%/AIWorkspace/` です。資格情報はWindows DPAPIで暗号化してこのPC・ユーザーに紐付けて保存し、GitHubへ送信しません。

指定されたLLMの起動例（本アプリからモデルを自動ダウンロードしません）:

```powershell
llama-server -hf empero-ai/Qwythos-9B-v2-GGUF:Q4_K_M --host 127.0.0.1 --port 8080 -ngl 99 -c 16384 -n 8192 --jinja -fa on --verbose --alias qwythos-9B-v2
```

## GitHub設定と共同作業

初期版はGitHub.comに対応します。Fine-grained PATは対象リポジトリに限定し、Contents・Issues・Pull requestsのRead/Write、Checks・Commit statusesのReadを設定します。組織側の承認・SSOが必要な場合はその設定にも従ってください。トークンをURLや文書へ記載しないでください。

作成者は共有プロジェクトIDを他の参加者へ伝えます。IDは進捗画面に表示されます。参加画面の「共有プロジェクトを検索」でも取得できます。参加者は自分のPCでexeを起動し、「GitHubから参加」で同じリポジトリとIDを入力します。

- `ai-system/<project-id>`: 計画・共有進捗・対話・テンプレートの状態。mainを直接更新しません。
- `ai-work/<task-id>`: 担当記録と成果物のタスク別ブランチ。同名の新規参照作成で同時着手を検知します。
- Issues: タスクと最終レビュー依頼。Issueを閉じるだけでは承認になりません。
- PR: 文書・コード・最終完了報告のレビュー。

「タスクを追加」で先行タスクと役割を指定すると、独立したタスクを複数人で並行して進められます。各PCの詳細ログやLLMの生成途中の文字列は共有しません。他PCの進捗は通常30秒間隔でGitHubから取得し、API制限時は遅れる場合があります。

GitHubのPR作者は自分のPRを承認できないため、最終レビューには別の人のリーダーが必要です。リーダーの認証をAIへ渡す必要はありません。アジャイルの各反復、AI-DLCの各サイクルの承認後、継続は「次の反復・サイクル」、全体を終了するときは「全体の最終承認へ」を選びます。

## 文書の書式

「文書テンプレート」でMarkdown本文を直接編集するか、`.md`をアップロードします。IDを変更して保存すると別テンプレートになります。文書種別、必須見出し、必須表の列を設定でき、タスク画面で生成に使う書式を選べます。

変数は `{{project.name}}`、`{{document.title}}`、`{{document.version}}`。AIは章構成を維持して生成し、必須項目を機械検証します。書式エラーがある文書は承認できません。生成に使用した本文とハッシュを成果物に保存し、テンプレート変更で既存成果物を変更しません。

## モデル・Skills・Tools

複数モデルをOpenAI互換API/Anthropic Messages形式で登録し、general・design・coding・reviewに割り当てられます。初期版のエージェントはタスクごとに1モデルで文書/コードを生成し、必要時に質問して停止します。文書の書式修正は1回試行します。

ponytailは `DietrichGebert/ponytail` のcommit `09c434ab73ca43761c5d8b0c5d341926c2fba710` を同梱しています。MITライセンスは同梱ディレクトリにあります。独自skillはブラウザへ本文を入力して追加・有効化できます。

Toolsは登録コマンドと、JSON応答形式のMCP Streamable HTTPの接続試験・手動tool呼出しに対応します。コマンドの例:

```json
["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
```

`{python}` と `{git}` は環境設定のパスへ展開します。テスト実行時は「このPCでの登録コマンド実行を許可」を有効にして、対象タスクで手動実行します。検証タスクのコマンドは同じ反復の実装ディレクトリで実行します。自動テスト結果または人の手動検証記録がない検証タスクは承認できません。

## 開発・試験・exeビルド

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe launcher.py
.\.venv\Scripts\python.exe scripts/build.py
```

成果物は `dist/AIWorkspace/`。実接続試験は起動済みの指定LLMに接続します:

```powershell
.\.venv\Scripts\python.exe scripts/live_llm_smoke.py
.\.venv\Scripts\python.exe scripts/live_code_smoke.py
.\.venv\Scripts\python.exe scripts/exe_smoke.py
```

テスト用の草案とログは `.runtime/`、exe試験のデータは `.exe-user-*/` に保存されます。これらと資格情報はGit管理対象外です。

## 現時点の範囲と設計との差分

- UIはReactではなく同梱のJavaScriptで実装し、外部CDNを不要にしました。
- 作業はタスク別ローカルディレクトリで分離し、GitHub APIでcommit・ブランチ・PRを作成します。ローカルGit worktree方式と既存ソースツリーの自動checkoutは未実装です。既存の大規模リポジトリを自動探索・編集する用途は追加実装が必要です。
- エージェントは構造化出力でファイルを生成します。エージェントは作業ファイルの読取・検索を最大8ステップ自動実行できます。追加した外部toolの自律実行、MCP stdio/SSE専用サーバー、OSレベルの隔離ランナーは未実装です。コマンドは信頼できるコードに限り、ユーザーが手動で実行してください。
- GitHub Enterprise、Projects、CI保護ルールの完全な自動判定、署名付き配布、自動更新、DBの自動バックアップ/マイグレーションは未実装です。
- 対話・テンプレート等は共有同期操作でGitHubに送ります。機密情報を対話や文書へ入力する場合は共有対象を確認してください。共有状態を全体で同時編集すると競合として停止し、ローカルデータを保持します。競合の手動統合UIは追加実装が必要です。
- 実際の2台のPCと複数GitHubアカウントを用いた受入、実際の人間のリーダー承認は未実施です。自動試験では2つの独立DBとGitHub APIの模擬応答で検証します。

全設計項目の完成版ではなく、上記範囲の動作する初期実装です。検証結果は [テスト報告](docs/test-report.md)、設計は [詳細設計](docs/detailed-design.md) を参照してください。
