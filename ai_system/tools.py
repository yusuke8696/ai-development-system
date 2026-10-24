import json
import os
import subprocess
import signal
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
        path.write_bytes(content.encode("utf-8"))


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


class _WindowsJob:
    """Keep a command and its descendants in one kill-on-close Windows job."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; breakaway is disabled.
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, process):
        import ctypes
        from ctypes import wintypes

        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

        # Popen closes CreateProcess's primary thread handle. Find that thread
        # while the command is suspended, before any user code can create children.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("thread_id", wintypes.DWORD),
                        ("process_id", wintypes.DWORD), ("base_priority", wintypes.LONG),
                        ("delta_priority", wintypes.LONG), ("flags", wintypes.DWORD)]

        self.api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.api.Thread32First.restype = wintypes.BOOL
        self.api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.api.Thread32Next.restype = wintypes.BOOL
        self.api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenThread.restype = wintypes.HANDLE
        self.api.ResumeThread.argtypes = [wintypes.HANDLE]
        self.api.ResumeThread.restype = wintypes.DWORD
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.process_id == process.pid:
                    thread = self.api.OpenThread(0x2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        self.api.CloseHandle(thread)
                    return
                available = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("コマンドの実行スレッドが見つかりません。")
        finally:
            self.api.CloseHandle(snapshot)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_command(argv, cwd, timeout=60):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("実行コマンドは文字列の配列で指定してください。")
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "PATHEXT", "COMSPEC"}}
    job = _WindowsJob() if os.name == "nt" else None
    process = None

    def stop_tree():
        if job:
            job.close()
        elif process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", shell=False,
                                   creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if job else 0,
                                   start_new_session=os.name != "nt")  # CREATE_SUSPENDED
        if job:
            job.attach_and_resume(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return {"exit_code": process.returncode, "stdout": stdout[-20000:], "stderr": stderr[-20000:]}
        except subprocess.TimeoutExpired:
            stop_tree()
            stdout, stderr = process.communicate(timeout=5)
            return {"exit_code": -1, "stdout": stdout[-20000:],
                    "stderr": stderr[-19900:] + "\n実行時間上限に達しました。子プロセスも停止しました。"}
    finally:
        stop_tree()
        if process:
            if process.poll() is None:
                process.kill()  # Includes failure before a suspended process entered the job.
            try:
                process.communicate(timeout=5)
            finally:
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


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
