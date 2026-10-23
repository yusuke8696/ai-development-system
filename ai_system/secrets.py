"""Windows DPAPI encryption; plaintext secrets never enter SQLite or exports."""
import base64
import ctypes
import json
import os
import threading
from pathlib import Path
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("資格情報の永続化はWindows DPAPIに対応しています。")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class Secrets:
    def __init__(self, root):
        self.path = Path(root) / "secrets.dpapi.json"
        self.lock = threading.RLock()

    def _read(self):
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def set(self, key, value):
        with self.lock:
            values = self._read()
            if value:
                values[key] = base64.b64encode(crypt(value.encode())).decode()
            else:
                values.pop(key, None)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values), "utf-8")
            temporary.replace(self.path)

    def get(self, key):
        with self.lock:
            value = self._read().get(key)
        return crypt(base64.b64decode(value), True).decode() if value else ""

    def configured(self, key):
        with self.lock:
            return bool(self._read().get(key))
