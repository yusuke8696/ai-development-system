import os
import signal
import sys
import time

import pytest

from ai_system.tools import run_command


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 0x102
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def assert_child_stopped(pid_path, heartbeat):
    assert pid_path.exists(), "The test child must start before cleanup is checked."
    pid = int(pid_path.read_text())
    try:
        deadline = time.monotonic() + 2
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.025)
        assert not alive(pid), "The command's descendant survived cleanup."
        previous = heartbeat.read_text()
        time.sleep(0.15)
        assert heartbeat.read_text() == previous
    finally:
        # Bound the test's footprint if an implementation regression leaves a child.
        if alive(pid):
            os.kill(pid, signal.SIGTERM)


def spawn_command(tmp_path, parent_exit=False):
    child = (
        "import os,pathlib,time\n"
        "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
        "for i in range(500):\n"
        " pathlib.Path('heartbeat.txt').write_text(str(i))\n"
        " time.sleep(0.02)\n"
    )
    parent = (
        "import pathlib,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while not pathlib.Path('heartbeat.txt').exists(): time.sleep(0.01)\n"
        "print('child started', flush=True)\n"
        + ("" if parent_exit else "time.sleep(30)\n")
    )
    return [sys.executable, "-c", parent]


def test_command_timeout_stops_descendants_and_keeps_output(tmp_path):
    result = run_command(spawn_command(tmp_path), tmp_path, timeout=1)
    assert result["exit_code"] == -1
    assert "child started" in result["stdout"]
    assert "実行時間上限" in result["stderr"]
    assert_child_stopped(tmp_path / "child.pid", tmp_path / "heartbeat.txt")


def test_parent_exit_also_cleans_up_background_children(tmp_path):
    result = run_command(spawn_command(tmp_path, parent_exit=True), tmp_path, timeout=5)
    assert result["exit_code"] == 0
    assert "child started" in result["stdout"]
    assert_child_stopped(tmp_path / "child.pid", tmp_path / "heartbeat.txt")


def test_command_still_returns_error_status_and_unicode_output(tmp_path):
    result = run_command([sys.executable, "-c", "import sys; print('document'); print('problem',file=sys.stderr); sys.exit(7)"],
                         tmp_path, timeout=5)
    assert result == {"exit_code": 7, "stdout": "document\n", "stderr": "problem\n"}


def test_command_launch_failure_is_reported_without_leaking_process(tmp_path):
    with pytest.raises(OSError):
        run_command([str(tmp_path / "missing-executable")], tmp_path, timeout=1)
