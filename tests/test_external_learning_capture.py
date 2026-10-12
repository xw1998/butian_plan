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
                def close(self): pass
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


def test_learning_queue_idempotency_is_scoped_to_owner():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            first = store.enqueue_url("http://source.example/same", owner_member_id="m1")
            same = store.enqueue_url("http://source.example/same", owner_member_id="m1")
            other = store.enqueue_url("http://source.example/same", owner_member_id="m2")
            assert first == same
            assert other != first
        finally:
            db.close()


def test_authorized_queue_is_idempotent_and_stops_at_candidate_source():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            store._public_host = lambda _host: None
            class FakeResponse:
                def close(self): pass
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
            assert result["claims_created"] == 1
            assert result["memories_created"] == 0
            assert store.list_learning_queue()[0]["status"] == "completed"
            assert db.execute("SELECT COUNT(*) FROM source_claims").fetchone()[0] == 1
        finally:
            db.close()


def test_stale_running_queue_is_reclaimed_deterministically():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            now = [1000.0]
            store = LearningStore(db, clock=lambda: now[0])
            store.enqueue_url("http://source.example/stale")
            queue_id = store.list_learning_queue()[0]["queue_id"]
            db.execute("UPDATE learning_queue SET status='running', updated_at=? WHERE queue_id=?", (now[0] - 301, queue_id))
            result = store.process_learning_queue(limit=1)
            assert result["remaining"] == 1
            assert store.list_learning_queue()[0]["status"] == "failed"
        finally:
            db.close()


def test_fresh_running_queue_is_not_reclaimed():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            now = [1000.0]
            store = LearningStore(db, clock=lambda: now[0])
            store.enqueue_url("http://source.example/fresh")
            queue_id = store.list_learning_queue()[0]["queue_id"]
            db.execute("UPDATE learning_queue SET status='running', updated_at=? WHERE queue_id=?", (now[0] - 299, queue_id))
            result = store.process_learning_queue(limit=1)
            assert result["remaining"] == 1
            assert store.list_learning_queue()[0]["status"] == "running"
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


def test_claim_reads_and_prior_promotion_are_scoped_to_owner():
    with tempfile.TemporaryDirectory() as directory:
        db = sqlite3.connect(Path(directory) / "learning.db")
        try:
            store = LearningStore(db)
            source_id = store.add_source("web", "https://example.test", "Example", "a" * 64, owner_member_id="m1")
            claim_id = store.add_claim(source_id, "claim", owner_member_id="m1")
            assert store.get_claim(claim_id, owner_member_id="m1")["claim"] == "claim"
            for operation in (
                lambda: store.get_claim(claim_id, owner_member_id="m2"),
                lambda: store.claim_as_prior(claim_id, owner_member_id="m2"),
            ):
                try:
                    operation()
                except ValueError as error:
                    assert str(error) == "source_claim_not_found"
                else:
                    raise AssertionError("cross-member claim read was accepted")
        finally:
            db.close()


def test_learning_queue_claim_is_single_owner_under_competing_workers():
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "learning.db"
        db = sqlite3.connect(db_path)
        store = LearningStore(db)
        queue_id = store.enqueue_url("http://source.example/once", owner_member_id="m1")
        db.commit()
        db2 = sqlite3.connect(db_path, timeout=5)
        store2 = LearningStore(db2)
        try:
            first = store._claim_queue_item(queue_id, "worker-a", now=1000.0)
            second = store2._claim_queue_item(queue_id, "worker-b", now=1000.0)
            assert first is not None
            assert second is None
            row = db.execute("SELECT worker_id, lease_token, status FROM learning_queue WHERE queue_id=?", (queue_id,)).fetchone()
            assert tuple(row) == ("worker-a", first["lease_token"], "running")
        finally:
            db2.close()
            db.close()


def test_learning_queue_backoff_dead_letter_lease_and_audit(tmp_path):
    now = [1000.0]
    store = LearningStore(sqlite3.connect(tmp_path / "learning.db"), clock=lambda: now[0])
    try:
        queue_id = store.enqueue_url("http://127.0.0.1:1/unreachable", owner_member_id="m1")
        first = store.process_learning_queue(worker_id="w1")
        assert first["processed"][0]["status"] == "failed"
        row = store.db.execute("SELECT status,retry_at,attempts FROM learning_queue WHERE queue_id=?", (queue_id,)).fetchone()
        assert row["status"] == "failed" and row["retry_at"] > now[0] and row["attempts"] == 1
        assert store.process_learning_queue(worker_id="w1")["processed"] == []
        now[0] = row["retry_at"]
        for expected_attempt in (2, 3):
            result = store.process_learning_queue(worker_id="w1")
            assert result["processed"][0]["status"] == ("failed" if expected_attempt == 2 else "dead_letter")
            row = store.db.execute("SELECT status,retry_at,attempts FROM learning_queue WHERE queue_id=?", (queue_id,)).fetchone()
            if expected_attempt == 2:
                assert row["retry_at"] > now[0]
                now[0] = row["retry_at"]
        assert store.db.execute("SELECT status FROM learning_queue WHERE queue_id=?", (queue_id,)).fetchone()[0] == "dead_letter"
        events = store.db.execute("SELECT event_type FROM learning_queue_events WHERE queue_id=? ORDER BY created_at", (queue_id,)).fetchall()
        assert [item[0] for item in events][:2] == ["enqueued", "claimed"]
        assert events[-1][0] == "dead_letter"

        lease_id = store.enqueue_url("http://127.0.0.1:1/lease", owner_member_id="m1")
        claimed = store._claim_queue_item(lease_id, "w1", now=now[0], lease_seconds=10)
        renewed = store.renew_learning_queue_lease(lease_id, "w1", claimed["lease_token"], lease_seconds=30)
        assert renewed["lease_until"] == now[0] + 30
    finally:
        store.db.close()


def test_learning_queue_lost_lease_does_not_publish_claim(tmp_path):
    db = sqlite3.connect(tmp_path / "learning.db")
    store = LearningStore(db)
    store._public_host = lambda _host: None
    queue_id = store.enqueue_url("http://source.example/fenced", owner_member_id="m1")
    import butian.runtime.learning as learning_module
    original = learning_module.build_opener
    class FakeResponse:
        def close(self): pass
        status = 200
        headers = {"Content-Length": "18", "Content-Type": "text/plain"}
        def __init__(self): self.body = b"public source body"
        def geturl(self): return "http://source.example/fenced"
        def read(self, size=-1):
            chunk, self.body = self.body[:size], self.body[size:]
            return chunk
    def open_and_fence(*_args, **_kwargs):
        row = db.execute("SELECT lease_token FROM learning_queue WHERE queue_id=?", (queue_id,)).fetchone()
        db.execute("UPDATE learning_queue SET worker_id='new-worker', lease_token='new-token', lease_until=999999 WHERE queue_id=?", (queue_id,))
        db.commit()
        return FakeOpenerResponse()
    class FakeOpenerResponse:
        def open(self, *_args, **_kwargs): return FakeResponse()
    learning_module.build_opener = lambda *_handlers: FakeOpenerResponse()
    original_claim = store.add_claim
    def claim_then_fence(*args, **kwargs):
        db.execute("UPDATE learning_queue SET worker_id='new-worker', lease_token='new-token', lease_until=999999 WHERE queue_id=?", (queue_id,))
        db.commit()
        return original_claim(*args, **kwargs)
    store.add_claim = claim_then_fence
    try:
        result = store.process_learning_queue(worker_id="old-worker")
    finally:
        learning_module.build_opener = original
    assert result["claims_created"] == 0
    assert db.execute("SELECT COUNT(*) FROM source_claims").fetchone()[0] == 0
    assert tuple(db.execute("SELECT worker_id, lease_token FROM learning_queue WHERE queue_id=?", (queue_id,)).fetchone()) == ("new-worker", "new-token")
    db.close()


def test_learning_queue_lost_lease_failure_does_not_publish_failure(tmp_path):
    db = sqlite3.connect(tmp_path / "learning.db")
    store = LearningStore(db, clock=lambda: 1000.0)
    queue_id = store.enqueue_url("https://source.example/failure-fenced", owner_member_id="m1")
    def fail_after_takeover(*args, **kwargs):
        db.execute("UPDATE learning_queue SET worker_id='new-worker', lease_token='new-token', lease_until=2000 WHERE queue_id=?", (queue_id,))
        db.commit()
        raise OSError("old worker network failure")
    store.capture_url = fail_after_takeover
    try:
        result = store.process_learning_queue(worker_id="old-worker")
        assert result["processed"][0]["status"] == "lost_lease"
        assert tuple(db.execute("SELECT status,worker_id,lease_token FROM learning_queue WHERE queue_id=?", (queue_id,)).fetchone()) == ("running", "new-worker", "new-token")
        assert db.execute("SELECT count(*) FROM learning_queue_events WHERE queue_id=? AND event_type IN ('failed','dead_letter')", (queue_id,)).fetchone()[0] == 0
    finally:
        db.close()


def test_legacy_duplicate_queue_concurrent_enqueue_is_idempotent(tmp_path):
    path = tmp_path / "legacy-queue.db"
    db = sqlite3.connect(path)
    store = LearningStore(db)
    first = store.enqueue_url("https://example.com/legacy", owner_member_id="owner")
    db.execute("DROP INDEX ux_learning_queue_owner_url_v2")
    db.execute("INSERT INTO learning_queue SELECT 'legacy-duplicate',owner_member_id,url,title,source_type,timeout,max_bytes,status,attempts,last_error,source_id,worker_id,lease_token,lease_until,created_at,updated_at,retry_at,max_attempts FROM learning_queue WHERE queue_id=?", (first,))
    db.commit()
    db.close()
    barrier = threading.Barrier(4)
    results, errors = [], []
    def enqueue():
        connection = sqlite3.connect(path, timeout=10)
        try:
            learning = LearningStore(connection)
            connection.commit()
            barrier.wait(timeout=10)
            results.append(learning.enqueue_url("https://example.com/new", owner_member_id="owner"))
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()
    workers = [threading.Thread(target=enqueue) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
    assert not any(worker.is_alive() for worker in workers)
    assert not errors, errors
    assert len(set(results)) == 1
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT COUNT(*) FROM learning_queue WHERE url='https://example.com/legacy'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM learning_queue WHERE url='https://example.com/new'").fetchone()[0] == 1
    finally:
        db.close()


def test_learning_enqueue_waits_for_shared_store_transaction(tmp_path):
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    errors, results = [], []
    entered = threading.Event()
    def enqueue():
        entered.set()
        try:
            results.append(service.learning.enqueue_url("https://source.example/shared", owner_member_id="owner"))
        except Exception as exc:
            errors.append(exc)
    try:
        with service.store._write_lock:
            service.store.db.execute("BEGIN IMMEDIATE")
            thread = threading.Thread(target=enqueue)
            thread.start()
            assert entered.wait(2)
            thread.join(timeout=0.1)
            service.store.db.commit()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not errors, errors
        assert len(results) == 1
    finally:
        service.close()


def test_slow_http_queue_does_not_hold_shared_database_lock(tmp_path, monkeypatch):
    from butian.runtime.service import ButianService
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            assert release.wait(5)
            body = b'Candidate from a real slow HTTP socket'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = HTTPServer(('127.0.0.1', 0), SlowHandler)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    service = ButianService(tmp_path)
    # Local fixture exercises real sockets; public-host security is tested separately.
    monkeypatch.setattr(service.learning, '_public_host', lambda _host: None)
    service.learning.enqueue_url(f'http://127.0.0.1:{server.server_port}/slow', owner_member_id='worker')
    results, errors = [], []
    def fetch():
        try:
            results.append(service.learning.process_learning_queue())
        except Exception as exc:
            errors.append(exc)
    def write():
        service.store.create_session('solo')
        written.set()
    fetching = threading.Thread(target=fetch)
    writing = threading.Thread(target=write)
    try:
        fetching.start()
        assert entered.wait(3)
        writing.start()
        assert written.wait(1), 'network fetch held shared database lock'
    finally:
        release.set()
        fetching.join(6)
        writing.join(3)
        server.shutdown()
        serving.join(3)
        server.server_close()
        service.close()
    assert not errors
    assert results[0]['processed'][0]['status'] == 'completed'
    assert results[0]['claims_created'] == 1
    assert results[0]['memories_created'] == 0


def test_capture_closes_response_on_success_and_size_failure(tmp_path, monkeypatch):
    import pytest
    import butian.runtime.learning as learning_module
    db = sqlite3.connect(tmp_path / 'response-close.db')
    store = LearningStore(db)
    monkeypatch.setattr(store, '_public_host', lambda host: None)
    responses = []
    class Response:
        status = 200
        headers = {'Content-Length': '4'}
        def __init__(self):
            self.closed = False
            self.body = b'data'
            responses.append(self)
        def geturl(self): return 'https://source.example/data'
        def read(self, size):
            chunk, self.body = self.body[:size], self.body[size:]
            return chunk
        def close(self): self.closed = True
    monkeypatch.setattr(learning_module, 'build_opener', lambda *handlers: type('Opener', (), {'open': lambda *args, **kwargs: Response()})())
    try:
        store.capture_url('https://source.example/data')
        assert responses[-1].closed
        with pytest.raises(ValueError, match='learning_fetch_too_large'):
            store.capture_url('https://source.example/data', max_bytes=1)
        assert responses[-1].closed
    finally:
        db.close()
