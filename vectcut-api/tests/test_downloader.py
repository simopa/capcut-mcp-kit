# Added in capcut-mcp-kit (2026): remote downloads are http(s) only, size-capped and never left partial.
# See NOTICE at the repository root.
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from downloader import download_file


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"x" * 5000
        self.send_response(200)
        if self.path != "/nolength":
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_download_works(server, tmp_path):
    dest = tmp_path / "a.bin"
    assert download_file(server + "/ok", str(dest)) is True
    assert dest.stat().st_size == 5000


@pytest.mark.parametrize("path", ["/ok", "/nolength"])
def test_too_large_is_refused_and_leaves_nothing(server, tmp_path, monkeypatch, path):
    monkeypatch.setenv("CAPCUT_MAX_DOWNLOAD_BYTES", "1000")
    dest = tmp_path / "big.bin"
    with pytest.raises(ValueError, match="larger"):
        download_file(server + path, str(dest))
    assert list(tmp_path.iterdir()) == []


def test_other_schemes_are_refused(tmp_path):
    with pytest.raises(ValueError, match="http"):
        download_file("file:///etc/hosts", str(tmp_path / "x"))


def test_failed_download_raises_and_leaves_nothing(tmp_path):
    dest = tmp_path / "x.bin"
    with pytest.raises(RuntimeError):
        download_file("http://127.0.0.1:9/never", str(dest), max_retries=1, timeout=2)
    assert list(tmp_path.iterdir()) == []
