import hashlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import requests

from uv_wheelhouse.cli import fetch
from uv_wheelhouse.select import Download

PAYLOAD = b"not really a wheel"


@pytest.fixture
def server():
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = PAYLOAD if self.path != "/corrupt.whl" else b"corrupted"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", hits
    httpd.shutdown()


def item(base: str, filename: str = "p-1-py3-none-any.whl") -> Download:
    return Download("p", filename, f"{base}/{filename}", hashlib.sha256(PAYLOAD).hexdigest())


def test_download_then_cache_then_present(server, tmp_path: Path):
    base, hits = server
    dest, store = tmp_path / "dest", tmp_path / "store"
    dest.mkdir()
    s = requests.Session()
    assert fetch(s, item(base), dest, store) == "download"
    assert (dest / "p-1-py3-none-any.whl").read_bytes() == PAYLOAD

    (dest / "p-1-py3-none-any.whl").unlink()  # e.g. fresh agent, restored uv cache dir
    assert fetch(s, item(base), dest, store) == "cache"
    assert fetch(s, item(base), dest, store) == "present"
    assert len(hits) == 1


def test_hash_mismatch_is_rejected_and_not_cached(server, tmp_path: Path):
    base, _ = server
    dest, store = tmp_path / "dest", tmp_path / "store"
    dest.mkdir()
    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetch(requests.Session(), item(base, "corrupt.whl"), dest, store)
    assert not list(store.rglob("*")) or not any(p.is_file() for p in store.rglob("*"))
    assert not (dest / "corrupt.whl").exists()
