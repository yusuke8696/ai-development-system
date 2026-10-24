"""Keep auxiliary TLS data required by the build interpreter's ssl module."""
from pathlib import Path
import ssl


def ssl_support_data(stdlib_dir=None):
    folder = Path(stdlib_dir) if stdlib_dir is not None else Path(ssl.__file__).resolve().parent
    # The bundled build runtime's SSLContext requires this public certificate
    # support file beside ssl.py. Stock Python does not use it. Preserve the
    # runtime's certificate validation rather than bypassing its SSLContext.
    name = "codex-rejected-cas.pem"
    data = folder / name
    source = folder / "ssl.py"
    if source.is_file() and name in source.read_text("utf-8") and not data.is_file():
        raise RuntimeError("PythonのTLS補助ファイルが不足しています。ビルド用Python環境を確認してください。")
    return [(str(data), ".")] if data.is_file() else []
