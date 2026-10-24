"""Read-only GitHub diagnostics. Never serialize secrets or raw upstream errors."""
import re
import socket
import ssl
import time
from urllib.parse import quote

import httpx

from .github import repository
from .store import now


class DiagnosticFailure(Exception):
    def __init__(self, code, message, action, **details):
        self.details = dict(code=code, message=message, action=action, **details)


def _causes(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _transport_failure(exc):
    if isinstance(exc, httpx.TimeoutException):
        return DiagnosticFailure("timeout", "GitHubへの接続がタイムアウトしました。", "回線とプロキシ、api.github.comへのHTTPS通信を確認して再実行してください。")
    if any(isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e) for e in _causes(exc)):
        return DiagnosticFailure("tls_certificate", "HTTPS証明書を検証できませんでした。", "PCの日時、社内プロキシやセキュリティソフトの証明書設定を確認してください。証明書検証は無効にしないでください。")
    if isinstance(exc, httpx.ProxyError):
        return DiagnosticFailure("proxy", "プロキシへの接続または認証に失敗しました。", "このPCのHTTP_PROXY・HTTPS_PROXY等の設定を確認してください。プロキシの資格情報は診断結果に含めません。")
    if any(isinstance(e, socket.gaierror) for e in _causes(exc)):
        return DiagnosticFailure("dns", "api.github.comの名前解決に失敗しました。", "インターネット接続とDNS設定を確認してください。")
    if isinstance(exc, httpx.LocalProtocolError):
        return DiagnosticFailure("invalid_token", "保存したトークンの形式が不正です。", "余分な空白・改行・Bearer等を含めず、トークンの値だけを入力して保存してください。")
    return DiagnosticFailure("network", "GitHubとの通信に失敗しました。", "回線、プロキシ、ファイアウォールでapi.github.comへのHTTPS通信が許可されているか確認してください。")


def _get(gh, path, stage):
    try:
        response = gh.client.get(path, follow_redirects=False)
    except httpx.RequestError as exc:
        raise _transport_failure(exc) from None
    details = {"http_status": response.status_code}
    request_id = response.headers.get("x-github-request-id", "")
    if re.fullmatch(r"[A-Fa-f0-9]{4,}:[A-Fa-f0-9:]{3,120}", request_id):
        details["request_id"] = request_id
    if response.status_code == 401:
        raise DiagnosticFailure("authentication", "GitHubがトークンを受け付けませんでした。", "トークンの有効期限・失効と、入力した値を確認し、必要なら再発行して保存してください。", **details)
    if response.status_code in {403, 429}:
        if (response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0" or
                response.headers.get("retry-after")):
            waits = [60]
            for header, offset in (("retry-after", 0), ("x-ratelimit-reset", time.time())):
                try:
                    waits.append(max(0, int(response.headers.get(header, "")) - int(offset)))
                except ValueError:
                    pass
            delay = min(max(waits), 86400)
            raise DiagnosticFailure("rate_limit", "GitHub APIの利用制限に達しました。", f"少なくとも{delay}秒待ってから再実行してください。連続して接続テストしないでください。", retry_after_seconds=delay, **details)
        if response.headers.get("x-github-sso", "").startswith("required"):
            raise DiagnosticFailure("sso", "組織のSSO認証が必要です。", "GitHubで組織のSSOとトークンの承認状態を確認してください。", **details)
        raise DiagnosticFailure("permission", "GitHubがアクセスを拒否しました。", "トークンの対象リポジトリ・権限・組織の承認状態を確認してください。通信先のプロキシによる拒否の可能性もあります。", **details)
    if response.status_code == 404:
        message = "基準ブランチが見つかりません。" if stage == "branch" else "リポジトリが見つからないか、このトークンに参照権限がありません。"
        action = "GitHubでREADME等を追加して基準ブランチを作成してください。" if stage == "branch" else "owner/repoの入力と、トークンのRepository accessに対象リポジトリが含まれることを確認してください。"
        raise DiagnosticFailure("branch_missing" if stage == "branch" else "repository_access", message, action, **details)
    if response.status_code == 409 and stage == "branch":
        raise DiagnosticFailure("branch_unavailable", "既定ブランチを参照できません。リポジトリが空の場合にも発生します。",
                                "GitHubでREADME等の初回コミットを作成し、既定ブランチがあることを確認してから再実行してください。", **details)
    if response.status_code != 200:
        raise DiagnosticFailure("upstream", "GitHub APIが正常に応答しませんでした。", "HTTPステータスを確認してください。5xxの場合は時間を置いて再実行してください。", **details)
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise DiagnosticFailure("invalid_response", "GitHub APIのJSON応答を読み取れませんでした。", "プロキシ等で応答が置き換えられていないか確認してください。", **details) from None
    return data, details


def diagnose_github(repo, github_factory, token_configured):
    stages = [("configuration", "保存済みリポジトリ設定"), ("credentials", "保存済みトークンの読み込み"),
              ("authentication", "GitHub認証"), ("repository", "リポジトリの参照"), ("branch", "既定ブランチの参照")]
    report = {"ok": False, "checked_at": now(), "repository": "", "steps": [
        {"id": key, "name": name, "state": "not_run"} for key, name in stages],
        "note": "保存済み設定を使用した読み取り専用の診断です。PR作成等の書き込み権限や保護ルールは、このテストでは検証しません。"}
    gh, index = None, 0
    try:
        try:
            repo = repository(repo)
        except (ValueError, TypeError, AttributeError):
            raise DiagnosticFailure("invalid_repository", "リポジトリの指定形式が不正です。", "owner/repo、またはhttps://github.com/owner/repoの形式で設定を保存してください。") from None
        report["repository"] = repo
        report["steps"][index].update(state="passed", message=repo)
        index = 1
        try:
            configured = token_configured()
        except Exception:
            raise DiagnosticFailure("credentials_unreadable", "保存済み資格情報を読み取れませんでした。", "同じWindowsユーザーで起動し、トークンを再入力して設定を保存してください。") from None
        if not configured:
            raise DiagnosticFailure("token_missing", "GitHubアクセストークンが未登録です。", "トークンを入力し「設定を保存」を押してから接続テストを実行してください。")
        try:
            gh = github_factory()
        except UnicodeError:
            raise DiagnosticFailure("invalid_token", "保存したトークンの形式が不正です。", "余分な空白・改行を除き、トークンの値だけを入力して保存してください。") from None
        except ValueError:
            raise DiagnosticFailure("client_configuration", "GitHub通信用の設定を準備できませんでした。", "このPCのプロキシURLや証明書の環境設定を確認してください。トークンには値だけを入力してください。") from None
        except Exception:
            raise DiagnosticFailure("credentials_unreadable", "保存済みトークンを復号できませんでした。", "このWindowsユーザーでトークンを再入力して保存してください。別PCの資格情報ファイルは利用できません。") from None
        report["steps"][index].update(state="passed", message="トークンを読み込みました。値は表示しません。")
        for index, path in ((2, "/user"), (3, f"/repos/{repo}"), (4, None)):
            stage = report["steps"][index]
            if index == 4:
                path = f"/repos/{repo}/git/ref/heads/{quote(report['default_branch'], safe='')}"
            data, details = _get(gh, path, stage["id"])
            if index == 2:
                if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", str(data.get("login", ""))):
                    raise DiagnosticFailure("invalid_response", "GitHubのユーザー情報を取得できませんでした。", "接続先やプロキシの応答を確認してください。", **details)
                report["login"] = data["login"]
                message = "認証ユーザー: " + report["login"]
            elif index == 3:
                branch = data.get("default_branch")
                if data.get("full_name", "").lower() != repo.lower():
                    raise DiagnosticFailure("invalid_response", "対象リポジトリの応答が一致しません。", "保存済みリポジトリ設定と通信先を確認してください。", **details)
                if not isinstance(branch, str) or not 1 <= len(branch) <= 200 or any(ord(c) < 33 or ord(c) == 127 for c in branch):
                    raise DiagnosticFailure("branch_missing", "基準ブランチの情報がありません。", "GitHubでREADME等を追加し、基準ブランチを作成してください。", **details)
                report["default_branch"] = branch
                message = "リポジトリを参照できました。"
            else:
                if data.get("object", {}).get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", data.get("object", {}).get("sha", "")):
                    raise DiagnosticFailure("invalid_response", "基準ブランチのコミットを確認できませんでした。", "GitHubで基準ブランチにコミットがあるか確認してください。", **details)
                message = "基準ブランチ: " + report["default_branch"]
            stage.update(state="passed", message=message, **details)
        report.update(ok=True, message="GitHubへの読み取り接続を確認できました。")
    except DiagnosticFailure as exc:
        stage = report["steps"][index]
        stage.update(state="failed", **exc.details)
        report.update(failed_stage=stage["id"], message=stage["message"])
    except Exception:
        # Unknown parser, OS and transport exceptions can contain paths or credentials.
        report["steps"][index].update(state="failed", code="unexpected", message="診断処理でエラーが発生しました。", action="アプリを再起動して再実行してください。解消しない場合はこの診断結果を共有してください。")
        report.update(failed_stage=report["steps"][index]["id"], message="診断処理でエラーが発生しました。")
    finally:
        if gh:
            try:
                gh.close()
            except Exception:
                pass
    return report
