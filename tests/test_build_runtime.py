from pathlib import Path
import ssl
import pytest
from scripts.build_runtime import ssl_support_data


def test_stock_python_does_not_require_extra_tls_data(tmp_path):
    (tmp_path / "ssl.py").write_text("# standard ssl module", "utf-8")
    assert ssl_support_data(tmp_path) == []


def test_required_tls_data_is_collected_at_the_runtime_root(tmp_path):
    (tmp_path / "ssl.py").write_text("# requires codex-rejected-cas.pem", "utf-8")
    data = tmp_path / "codex-rejected-cas.pem"
    data.write_bytes(b"public certificate fixture")
    assert ssl_support_data(tmp_path) == [(str(data), ".")]
    assert data.read_bytes() == b"public certificate fixture"


def test_missing_required_tls_support_stops_the_build(tmp_path):
    (tmp_path / "ssl.py").write_text("# requires codex-rejected-cas.pem", "utf-8")
    with pytest.raises(RuntimeError, match="TLS"):
        ssl_support_data(tmp_path)


def test_build_interpreter_ssl_support_can_be_loaded():
    assert ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED
    for source, destination in ssl_support_data():
        assert Path(source).is_file() and destination == "."
