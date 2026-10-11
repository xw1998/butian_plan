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
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            store._public_host = lambda _host: None
            class FakeResponse:
                status = 200
                headers = {"Content-Length": "18", "Content-Type": "text/plain"}
                def __init__(self): self.body = b"public source body"
                def geturl(self): return "http://source.example/source"
                def read(self, size=-1):
                    chunk, self.body = self.body[:size], self.body[size:]
                    return chunk
            import butian.runtime.learning as learning_module
            original_build_opener = learning_module.build_opener
            learning_module.build_opener = lambda *_handlers: type("FakeOpener", (), {"open": lambda _self, *_args, **_kwargs: FakeResponse()})()
            try:
                result = store.capture_url("http://source.example/source")
            finally:
                learning_module.build_opener = original_build_opener
            assert result["verification"]["status"] == "verified"
            assert store.list_learning_runs()[0]["status"] == "completed"
        finally:
            db.close()


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


def test_authorized_queue_is_idempotent_and_stops_at_candidate_source():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            store._public_host = lambda _host: None
            class FakeResponse:
                status = 200
                headers = {"Content-Length": "18", "Content-Type": "text/plain"}
                def __init__(self): self.body = b"public source body"
                def geturl(self): return "http://source.example/source"
                def read(self, size=-1):
                    chunk, self.body = self.body[:size], self.body[size:]
                    return chunk
            import butian.runtime.learning as learning_module
            original = learning_module.build_opener
            learning_module.build_opener = lambda *_handlers: type("FakeOpener", (), {"open": lambda _self, *_args, **_kwargs: FakeResponse()})()
            try:
                first = store.enqueue_url("http://source.example/source", title="source")
                second = store.enqueue_url("http://source.example/source", title="duplicate")
                assert first == second
                result = store.process_learning_queue(limit=1)
            finally:
                learning_module.build_opener = original
            assert result["processed"][0]["status"] == "completed"
            assert result["claims_created"] == 0
            assert result["memories_created"] == 0
            assert store.list_learning_queue()[0]["status"] == "completed"
            assert db.execute("SELECT COUNT(*) FROM source_claims").fetchone()[0] == 0
        finally:
            db.close()


def test_authorized_queue_records_failure_and_can_retry():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            store._public_host = lambda _host: (_ for _ in ()).throw(ValueError("blocked"))
            store.enqueue_url("http://source.example/private")
            result = store.process_learning_queue(limit=1)
            assert result["processed"][0]["status"] == "failed"
            row = store.list_learning_queue()[0]
            assert row["status"] == "failed"
            assert row["attempts"] == 1
            assert "blocked" in row["last_error"]
        finally:
            db.close()
