import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from butian.runtime.learning import LearningStore


class _SourceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"public source body"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_capture_url_registers_verified_source_and_run():
    with tempfile.TemporaryDirectory() as directory:
        server = HTTPServer(("127.0.0.1", 0), _SourceHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            result = store.capture_url(f"http://127.0.0.1:{server.server_port}/source")
            assert result["verification"]["status"] == "verified"
            assert store.list_learning_runs()[0]["status"] == "completed"
        finally:
            db.close()
            server.shutdown()


def test_capture_url_rejects_loopback_and_audits_failure():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            try:
                store.capture_url("http://127.0.0.1/private")
            except ValueError as error:
                assert str(error) == "learning_fetch_host_forbidden"
            else:
                raise AssertionError("loopback URL was accepted")
            assert store.list_learning_runs()[0]["status"] == "failed"
        finally:
            db.close()
