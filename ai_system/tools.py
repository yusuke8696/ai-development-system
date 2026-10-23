import json
import os
import subprocess
from pathlib import Path
import httpx


def safe_path(root, relative):
    root = Path(root).resolve()
    if not relative or Path(relative).is_absolute() or ":" in relative or "\\" in relative:
        raise ValueError("作業領域内の相対パスを/区切りで指定してください。")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("作業領域外へのアクセスです。")
    if any(part.startswith(".") for part in Path(relative).parts):
        raise ValueError("隠しファイル・管理ファイルは対象外です。")
    if any(part.lower() in {"con", "nul", "prn", "aux", "com1", "lpt1"} for part in Path(relative).parts):
        raise ValueError("予約済みファイル名です。")
    return path


def write_files(root, files):
    if len(files) > 100 or sum(len(f["content"].encode()) for f in files) > 2 * 1024 * 1024:
        raise ValueError("一度に書けるファイル量を超えています。")
    paths = [(safe_path(root, f["path"]), f["content"]) for f in files]
    for path, content in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def read_file(root, relative):
    path = safe_path(root, relative)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("読取対象は1MiB以内にしてください。")
    return path.read_text(encoding="utf-8")


def search_files(root, query):
    if not query or len(query) > 200:
        raise ValueError("検索文字列は1〜200文字で指定してください。")
    result = []
    for i, path in enumerate(Path(root).rglob("*")):
        if i > 2000 or len(result) >= 100:
            break
        try:
            relative = path.relative_to(root).as_posix()
            if path.is_file() and path.stat().st_size < 1024 * 1024:
                for number, line in enumerate(read_file(root, relative).splitlines(), 1):
                    if query.casefold() in line.casefold():
                        result.append({"path": relative, "line": number, "text": line[:500]})
                        if len(result) >= 100:
                            break
        except (ValueError, OSError, UnicodeError):
            continue
    return result


def run_command(argv, cwd, timeout=60):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("実行コマンドは文字列の配列で指定してください。")
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "PATHEXT", "COMSPEC"}}
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, shell=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return {"exit_code": result.returncode, "stdout": result.stdout[-20000:], "stderr": result.stderr[-20000:]}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "実行時間上限に達しました。"}


def mcp_http(tool, method="tools/list", params=None):
    """Basic MCP Streamable HTTP JSON mode; no subscription or stdio transport."""
    url = tool["url"]
    if not url.startswith(("http://", "https://")):
        raise ValueError("MCP URLはhttp/httpsで指定してください。")
    with httpx.Client(timeout=tool.get("timeout", 30), trust_env=False) as client:
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}
        init = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "AIWorkspace", "version": "0.1"}}})
        init.raise_for_status()
        if init.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = init.headers["mcp-session-id"]
        client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}})
        result.raise_for_status()
        if "application/json" not in result.headers.get("content-type", ""):
            raise ValueError("この版ではMCPのJSON応答に対応しています。SSE専用サーバーは非対応です。")
        return result.json()
