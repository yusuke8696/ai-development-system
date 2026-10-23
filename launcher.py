import argparse
import ctypes
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
import uvicorn
from ai_system.server import create_app


def main():
    parser = argparse.ArgumentParser(description="AI駆動開発ワークスペース")
    parser.add_argument("--data-dir", default=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AIWorkspace"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--launch-file", help="自動試験用。ローカル起動URLの保存先")
    args = parser.parse_args()
    root = Path(args.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive local file lock is tied to the process and released after crashes.
    lock = (root / "instance.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            if lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(0, "すでに起動しています。開いているブラウザ画面をご利用ください。", "AI Workspace", 0)
        return
    code = secrets.token_urlsafe(32)
    sock = socket.socket()
    sock.bind(("127.0.0.1", args.port))
    sock.listen(128)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/launch/{code}"
    app = create_app(root, code)
    # Windowed executables have no sys.stdout/stderr for the default colored formatter.
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", log_config=None))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    def browser():
        while not server.started and not server.should_exit:
            time.sleep(0.05)
        if server.started:
            if args.launch_file:
                Path(args.launch_file).write_text(json.dumps({"url": url, "port": port}), "utf-8")
            if not args.no_browser:
                webbrowser.open(url)
    threading.Thread(target=browser, daemon=True).start()
    try:
        server.run(sockets=[sock])
    finally:
        app.state.service.close()
        sock.close()
        lock.close()


if __name__ == "__main__":
    main()
