import asyncio
import json
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .service import Service, ASSETS
from .store import Conflict
from .templates import validate_template, validate_document, MD
from .tools import mcp_http, read_file, search_files
from .workflow import WORKFLOWS


def create_app(root, bootstrap=None, service=None, polling=True):
    service = service or Service(root)
    bootstrap = bootstrap or secrets.token_urlsafe(32)
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    consumed = False

    @asynccontextmanager
    async def lifespan(app):
        if polling:
            threading.Thread(target=service.polling, daemon=True).start()
        yield
        service.close()

    app = FastAPI(title="AI Development Workspace", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.bootstrap = bootstrap

    @app.middleware("http")
    async def security(request, call_next):
        hostname = request.url.hostname
        if hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"message": "Invalid host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"message": "Invalid origin"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.cookies.get("ai_session", ""), session):
                return JSONResponse({"message": "exeからブラウザを開き直してください。"}, status_code=401)
            if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                return JSONResponse({"message": "Invalid CSRF token"}, status_code=403)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 4 * 1024 * 1024:
            return JSONResponse({"message": "入力サイズ上限を超えています。"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def error(request, exc):
        status = 409 if isinstance(exc, Conflict) else 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, KeyError) else 400 if isinstance(exc, (ValueError, TypeError)) else 500
        message = str(exc) if status in {400, 403, 409} else "対象が見つかりません。" if status == 404 else "処理に失敗しました。接続先・設定・同期状態を確認してください。"
        return JSONResponse({"message": message[:1500], "code": type(exc).__name__}, status_code=status)

    @app.get("/launch/{code}")
    def launch(code):
        nonlocal consumed
        if consumed or not secrets.compare_digest(code, bootstrap):
            raise HTTPException(403)
        consumed = True
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("ai_session", session, httponly=True, samesite="strict")
        return response

    @app.get("/api/state")
    def state():
        return {"csrf": csrf, "settings": service.settings(), "workflows": WORKFLOWS,
                "projects": [dict(service.project(p["id"]), template_preparation=service.template_preparation(p["id"])) for p in service.store.list("projects")],
                **{kind: service.store.list(kind) for kind in ("models", "templates", "project_templates", "skills", "tools", "artifacts", "dialogue", "test_runs")}}

    @app.post("/api/settings")
    def settings(body: dict):
        old = service.settings()
        if old["version"] != body.get("expected_version"):
            raise Conflict("設定が変更されています。再取得してください。")
        if "github_token" in body:
            service.secrets.set("github", body.pop("github_token"))
        data = {k: v for k, v in body.items() if k in {"repo", "python", "git", "workspace", "poll_seconds", "allow_commands"}}
        if "poll_seconds" in data:
            data["poll_seconds"] = max(30, min(3600, int(data["poll_seconds"])))
        old.pop("github_token_configured", None)
        return service.store.put("settings", dict(old, **data), body.get("expected_version"))

    @app.post("/api/models")
    def models(body: dict):
        secret = body.pop("secret", None)
        expected = body.pop("expected_version", None)
        body = {k: v for k, v in body.items() if k in {"id", "name", "provider", "endpoint", "model_id", "roles", "max_tokens", "timeout"}}
        if not body.get("name") or not body.get("model_id") or not body.get("endpoint", "").startswith(("http://", "https://")):
            raise ValueError("表示名・モデルID・API URLが必要です。")
        if body.get("provider") not in {"openai", "anthropic"}:
            raise ValueError("API方式を選択してください。")
        body["max_tokens"] = max(64, min(8192, int(body.get("max_tokens", 4096))))
        body["timeout"] = max(10, min(600, int(body.get("timeout", 180))))
        if not isinstance(body.get("roles"), list):
            raise ValueError("rolesは配列です。")
        if body.get("id"):
            old = service.store.get("models", body["id"])
            if old["version"] != expected:
                raise Conflict("モデル設定が変更されています。再取得してください。")
        # A role has exactly one active model, even after adding a second profile.
        for prior in service.store.list("models"):
            if prior["id"] != body.get("id") and set(prior.get("roles", [])) & set(body["roles"]):
                service.store.change("models", prior["id"], roles=[r for r in prior["roles"] if r not in body["roles"]])
        saved = service.store.put("models", body, expected)
        if secret is not None:
            service.secrets.set("model:" + saved["id"], secret)
        return saved

    @app.post("/api/models/{model_id}/test")
    def model_test(model_id):
        from .llm import LLM
        model = service.store.get("models", model_id)
        llm = LLM(model, service.secrets.get("model:" + model_id))
        try:
            return llm.test()
        finally:
            llm.close()

    @app.post("/api/github/test")
    def github_test():
        gh = service.github()
        try:
            user = gh.user()
            return {"login": user["login"], "id": user["id"], "repo": gh.meta(service.settings()["repo"])["full_name"]}
        finally:
            gh.close()

    @app.post("/api/projects")
    def project_create(body: dict):
        return service.create_project(body)

    @app.post("/api/discover")
    def discover(body: dict):
        from .github import repository
        gh = service.github()
        try:
            return {"ids": gh.discover(repository(body["repo"]))}
        finally:
            gh.close()

    @app.post("/api/join")
    def join(body: dict):
        return service.join(body["repo"], body["project_id"])

    @app.post("/api/projects/{pid}/sync")
    def sync(pid):
        return service.sync(pid)

    @app.post("/api/projects/{pid}/issues")
    def issues(pid):
        return service.create_issues(pid)

    @app.post("/api/projects/{pid}/next")
    def next_scope(pid):
        return service.next_scope(pid)

    @app.post("/api/projects/{pid}/finish")
    def finish_project(pid):
        return service.finish_project(pid)

    @app.post("/api/shutdown")
    def shutdown():
        service.close()
        if hasattr(app.state, "shutdown"):
            threading.Timer(0.2, app.state.shutdown).start()
        return {"message": "終了します。"}

    @app.post("/api/projects/{pid}/configure")
    def configure(pid, body: dict):
        old = service.store.get("projects", pid)
        return service.store.put("projects", dict(old, leader_login=body.get("leader_login", old.get("leader_login", "")),
                                                 leader_id=None), body.get("expected_version"))

    @app.get("/api/projects/{pid}/preparation")
    def preparation(pid):
        return service.template_preparation(pid)

    @app.post("/api/projects/{pid}/templates/prepare")
    def prepare_templates(pid, body: dict):
        return service.prepare_templates(pid, body.get("bindings"))

    @app.post("/api/projects/{pid}/templates/confirm")
    def confirm_templates(pid):
        return service.confirm_templates(pid)

    @app.post("/api/projects/{pid}/answer")
    def answer(pid, body: dict):
        return service.answer(pid, body)

    @app.post("/api/projects/{pid}/tasks")
    def add_task(pid, body: dict):
        return service.add_task(pid, body)

    @app.post("/api/tasks/{tid}/start")
    def start(tid, body: dict):
        return service.start(tid, body)

    @app.post("/api/tasks/{tid}/cancel")
    def cancel(tid):
        with service.lock:
            task = service.store.get("tasks", tid)
            service.require_local_owner(task)
            if task["state"] != "running":
                raise Conflict("実行中のタスクだけ停止できます。")
            service.cancelled.add(tid)
            return service.store.change("tasks", tid, state="cancelling")

    @app.post("/api/tasks/{tid}/approve")
    def approve(tid, body: dict):
        return service.approve(tid, body)

    @app.post("/api/tasks/{tid}/claim")
    def claim(tid):
        return service.claim(tid)

    @app.post("/api/tasks/{tid}/handover")
    def handover(tid, body: dict):
        return service.handover(tid, body["target_login"], body["target_device"])

    @app.post("/api/tasks/{tid}/publish")
    def publish(tid):
        return service.publish(tid)

    @app.post("/api/tasks/{tid}/prepare-final")
    def prepare_final(tid):
        return service.prepare_final(tid)

    @app.post("/api/tasks/{tid}/refresh")
    def refresh(tid):
        return service.refresh_approval(tid)

    @app.post("/api/tasks/{tid}/test")
    def test(tid, body: dict):
        return service.test_command(tid, body["tool_id"])

    @app.post("/api/tasks/{tid}/file")
    def workspace_read(tid, body: dict):
        task = service.store.get("tasks", tid)
        return {"content": read_file(service.workdir(task), body["path"])}

    @app.post("/api/tasks/{tid}/search")
    def workspace_search(tid, body: dict):
        task = service.store.get("tasks", tid)
        return {"matches": search_files(service.workdir(task), body["query"])}

    @app.post("/api/templates")
    def templates(body: dict):
        expected = body.pop("expected_version", None)
        data = validate_template(body)
        return service.store.put("templates", data, expected)

    @app.post("/api/templates/validate")
    def template_validate(body: dict):
        return {"errors": validate_document(body["markdown"], service.store.get("templates", body["template_id"]))}

    @app.post("/api/preview")
    def preview(body: dict):
        return {"html": MD.render(body.get("markdown", "")[:1024 * 1024])}

    @app.post("/api/skills")
    def skills(body: dict):
        import hashlib
        expected = body.pop("expected_version", None)
        if not body.get("name") or not isinstance(body.get("body"), str) or len(body["body"]) > 100000:
            raise ValueError("skill名と本文を指定してください（100KB以内）。")
        data = {k: v for k, v in body.items() if k in {"id", "name", "body", "enabled", "source", "commit"}}
        data["hash"] = hashlib.sha256(data["body"].encode()).hexdigest()
        return service.store.put("skills", data, expected)

    @app.post("/api/tools")
    def tools(body: dict):
        expected = body.pop("expected_version", None)
        if body.get("transport") not in {"command", "mcp-http"}:
            raise ValueError("commandまたはmcp-httpを指定してください。")
        if body["transport"] == "command" and not isinstance(body.get("argv"), list):
            raise ValueError("argvは配列で指定してください。")
        return service.store.put("tools", body, expected)

    @app.post("/api/tools/{tid}/test")
    def tool_test(tid):
        tool = service.store.get("tools", tid)
        if tool["transport"] == "builtin":
            return {"message": tool["description"], "available": True}
        if tool["transport"] == "mcp-http":
            return mcp_http(tool)
        return {"message": "コマンドはタスク画面から作業領域を指定して実行します。"}

    @app.post("/api/tools/{tid}/call")
    def tool_call(tid, body: dict):
        tool = service.store.get("tools", tid)
        if tool["transport"] != "mcp-http" or not isinstance(body.get("arguments", {}), dict) or not body.get("name"):
            raise ValueError("MCPのtool名と引数オブジェクトを指定してください。")
        return mcp_http(tool, "tools/call", {"name": body["name"], "arguments": body.get("arguments", {})})

    @app.get("/api/events")
    async def events(request: Request):
        after = int(request.headers.get("last-event-id", "0"))
        async def stream():
            nonlocal after
            while not await request.is_disconnected() and not service.closing.is_set():
                rows = service.store.events(after)
                for row in rows:
                    after = row["seq"]
                    yield f"id: {after}\nevent: changed\ndata: {json.dumps(row)}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/")
    def index():
        return FileResponse(ASSETS / "web/index.html")

    app.mount("/static", StaticFiles(directory=ASSETS / "web"), name="static")
    return app
