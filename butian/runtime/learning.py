from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from typing import Any


class LearningStore:
    """Reproducible source-knowledge ledger; sources never become facts directly."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_sources (
            source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, locator TEXT NOT NULL,
            title TEXT NOT NULL, content_hash TEXT NOT NULL, retrieved_at REAL NOT NULL,
            metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate'
        );
        CREATE TABLE IF NOT EXISTS source_claims (
            claim_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, claim TEXT NOT NULL,
            conditions TEXT NOT NULL, limitations TEXT NOT NULL, confidence REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate', created_at REAL NOT NULL,
            predictions TEXT NOT NULL DEFAULT '[]', falsifiers TEXT NOT NULL DEFAULT '[]'
        );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(source_claims)")}
        for name in ("predictions", "falsifiers"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE source_claims ADD COLUMN {name} TEXT NOT NULL DEFAULT '[]'")
        source_columns = {row[1] for row in self.db.execute("PRAGMA table_info(knowledge_sources)")}
        if "verified_hash" not in source_columns:
            self.db.execute("ALTER TABLE knowledge_sources ADD COLUMN verified_hash TEXT")
        if "verified_at" not in source_columns:
            self.db.execute("ALTER TABLE knowledge_sources ADD COLUMN verified_at REAL")
        self.db.execute("""
        CREATE TABLE IF NOT EXISTS learning_runs (
            run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, locator TEXT NOT NULL,
            source_id TEXT, status TEXT NOT NULL, details TEXT NOT NULL,
            started_at REAL NOT NULL, finished_at REAL
        )
        """)
        self.db.execute("""
        CREATE TABLE IF NOT EXISTS learning_queue (
            queue_id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT,
            source_type TEXT NOT NULL DEFAULT 'web', timeout REAL NOT NULL DEFAULT 10,
            max_bytes INTEGER NOT NULL DEFAULT 1000000, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, source_id TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )
        """)

    def _run(self, mode: str, locator: str) -> tuple[str, float]:
        run_id = f"learnrun_{uuid.uuid4().hex}"
        started = time.time()
        self.db.execute("INSERT INTO learning_runs(run_id,mode,locator,source_id,status,details,started_at,finished_at) VALUES (?,?,?,?,?,?,?,NULL)", (run_id, mode, locator, None, "running", "{}", started))
        return run_id, started

    def _finish(self, run_id: str, status: str, details: dict[str, Any], source_id: str | None = None) -> None:
        self.db.execute("UPDATE learning_runs SET source_id=?,status=?,details=?,finished_at=? WHERE run_id=?", (source_id, status, json.dumps(details, ensure_ascii=False, sort_keys=True), time.time(), run_id))

    @staticmethod
    def _public_host(host: str) -> None:
        if not host or host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("learning_fetch_host_forbidden")
        try:
            addresses = {ipaddress.ip_address(host)}
        except ValueError:
            try:
                addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
            except OSError as exc:
                raise ValueError("learning_fetch_dns_failed") from exc
        if not addresses or any(addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved or addr.is_unspecified for addr in addresses):
            raise ValueError("learning_fetch_host_forbidden")

    def capture_url(self, url: str, *, title: str | None = None, source_type: str = "web", timeout: float = 10.0, max_bytes: int = 1_000_000, user_agent: str = "butian-learning/1") -> dict[str, Any]:
        """Fetch an explicitly requested public URL and register only hash-verified bytes."""
        if source_type not in {"web", "paper", "standard", "manual", "user"} or not 0 < timeout <= 60 or not 0 < max_bytes <= 10_000_000:
            raise ValueError("invalid_learning_fetch_limits")
        run_id, _ = self._run("url_capture", url)
        try:
            current = url
            class SafeRedirects(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    parsed = urlparse(newurl)
                    if parsed.scheme not in {"http", "https"}:
                        raise ValueError("learning_fetch_scheme_forbidden")
                    LearningStore._public_host(parsed.hostname or "")
                    return super().redirect_request(req, fp, code, msg, headers, newurl)
            for _ in range(6):
                parsed = urlparse(current)
                if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                    raise ValueError("learning_fetch_url_forbidden")
                self._public_host(parsed.hostname or "")
                request = Request(current, headers={"User-Agent": user_agent, "Accept": "text/plain,text/html,application/xhtml+xml"})
                response = build_opener(SafeRedirects).open(request, timeout=timeout)
                final = response.geturl()
                if final == current:
                    break
                current = final
            else:
                raise ValueError("learning_fetch_too_many_redirects")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError("learning_fetch_too_large")
            chunks, total = [], 0
            while True:
                chunk = response.read(min(65536, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("learning_fetch_too_large")
                chunks.append(chunk)
            raw = b"".join(chunks)
            digest = hashlib.sha256(raw).hexdigest()
            source_id = self.add_source(source_type, current, title or current, digest, metadata={"capture_run_id": run_id, "http_status": getattr(response, "status", None), "content_type": response.headers.get("Content-Type"), "bytes": len(raw)})
            verification = self.verify_source_content(source_id, raw)
            details = {"source_id": source_id, "final_locator": current, "bytes": len(raw), "sha256": digest, "verification": verification}
            self._finish(run_id, "completed", details, source_id)
            return {"run_id": run_id, **details, "content": raw.decode("utf-8", errors="replace")}
        except Exception as exc:
            self._finish(run_id, "failed", {"error": type(exc).__name__, "message": str(exc)})
            raise

    def list_learning_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM learning_runs ORDER BY started_at DESC LIMIT ?", (max(1, min(int(limit), 500)),))]

    def enqueue_url(self, url: str, *, title: str | None = None, source_type: str = "web", timeout: float = 10.0, max_bytes: int = 1_000_000) -> str:
        """Queue an explicitly authorized public URL; enqueue is idempotent."""
        if not isinstance(url, str) or not url.strip() or source_type not in {"web", "paper", "standard", "manual", "user"}:
            raise ValueError("invalid_learning_queue_item")
        if not 0 < float(timeout) <= 60 or not 0 < int(max_bytes) <= 10_000_000:
            raise ValueError("invalid_learning_fetch_limits")
        now = time.time()
        queue_id = f"learnq_{uuid.uuid4().hex}"
        self.db.execute("INSERT OR IGNORE INTO learning_queue(queue_id,url,title,source_type,timeout,max_bytes,status,attempts,last_error,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,\'pending\',0,NULL,NULL,?,?)", (queue_id, url.strip(), title, source_type, float(timeout), int(max_bytes), now, now))
        row = self.db.execute("SELECT queue_id FROM learning_queue WHERE url=?", (url.strip(),)).fetchone()
        return str(row["queue_id"])

    def list_learning_queue(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM learning_queue ORDER BY created_at LIMIT ?", (max(1, min(int(limit), 500)),))]

    def process_learning_queue(self, *, limit: int = 1) -> dict[str, Any]:
        """Process a bounded set of authorized URLs; never creates claims or memories."""
        if not 0 < int(limit) <= 20:
            raise ValueError("invalid_learning_queue_limit")
        rows = self.db.execute("SELECT * FROM learning_queue WHERE status IN ('pending','failed') ORDER BY created_at LIMIT ?", (int(limit),)).fetchall()
        processed = []
        for row in rows:
            queue_id = row["queue_id"]
            self.db.execute("UPDATE learning_queue SET status='running', attempts=attempts+1, updated_at=? WHERE queue_id=?", (time.time(), queue_id))
            try:
                result = self.capture_url(row["url"], title=row["title"], source_type=row["source_type"], timeout=row["timeout"], max_bytes=row["max_bytes"])
                self.db.execute("UPDATE learning_queue SET status='completed', source_id=?, last_error=NULL, updated_at=? WHERE queue_id=?", (result["source_id"], time.time(), queue_id))
                processed.append({"queue_id": queue_id, "status": "completed", "source_id": result["source_id"], "run_id": result["run_id"]})
            except Exception as exc:
                self.db.execute("UPDATE learning_queue SET status='failed', last_error=?, updated_at=? WHERE queue_id=?", (type(exc).__name__ + ":" + str(exc), time.time(), queue_id))
                processed.append({"queue_id": queue_id, "status": "failed", "error": type(exc).__name__ + ":" + str(exc)})
        return {"status": "completed", "processed": processed, "remaining": self.db.execute("SELECT COUNT(*) FROM learning_queue WHERE status='pending'").fetchone()[0], "claims_created": 0, "memories_created": 0}

    def add_source(self, source_type: str, locator: str, title: str, content_hash: str, *, metadata: dict[str, Any] | None = None) -> str:
        if source_type not in {"paper", "standard", "manual", "web", "user"} or not locator.strip() or not title.strip() or not content_hash.strip():
            raise ValueError("invalid_knowledge_source")
        source_id = f"src_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO knowledge_sources(source_id,source_type,locator,title,content_hash,retrieved_at,metadata,status) VALUES (?,?,?,?,?,?,?,?)", (source_id, source_type, locator, title, content_hash, time.time(), json.dumps(metadata or {}, ensure_ascii=False), "candidate"))
        return source_id

    def verify_source_content(self, source_id: str, content: str | bytes) -> dict[str, Any]:
        row = self.db.execute("SELECT content_hash FROM knowledge_sources WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError("knowledge_source_not_found")
        raw = content.encode("utf-8") if isinstance(content, str) else content
        actual = hashlib.sha256(raw).hexdigest()
        expected = str(row["content_hash"]).lower().removeprefix("sha256:")
        matched = actual == expected
        self.db.execute("UPDATE knowledge_sources SET status=?,verified_hash=?,verified_at=? WHERE source_id=?", ("verified" if matched else "hash_mismatch", actual, time.time(), source_id))
        return {"source_id": source_id, "status": "verified" if matched else "hash_mismatch", "expected_hash": expected, "actual_hash": actual}

    def add_claim(self, source_id: str, claim: str, *, conditions=None, limitations=None, confidence: float = 0.0, predictions=None, falsifiers=None) -> str:
        if not self.db.execute("SELECT 1 FROM knowledge_sources WHERE source_id=?", (source_id,)).fetchone():
            raise ValueError("knowledge_source_not_found")
        if not claim.strip() or not 0 <= confidence <= 1:
            raise ValueError("invalid_source_claim")
        predictions = list(predictions or [])
        falsifiers = list(falsifiers or [])
        if any(not isinstance(item, str) or not item.strip() for item in predictions + falsifiers):
            raise ValueError("invalid_claim_test_criteria")
        claim_id = f"claim_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO source_claims(claim_id,source_id,claim,conditions,limitations,confidence,status,created_at,predictions,falsifiers) VALUES (?,?,?,?,?,?,?,?,?,?)", (claim_id, source_id, claim, json.dumps(conditions or {}, ensure_ascii=False), json.dumps(limitations or [], ensure_ascii=False), confidence, "candidate", time.time(), json.dumps(predictions, ensure_ascii=False), json.dumps(falsifiers, ensure_ascii=False)))
        return claim_id

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT c.*,s.title,s.locator,s.content_hash FROM source_claims c JOIN knowledge_sources s ON s.source_id=c.source_id WHERE c.claim_id=?", (claim_id,)).fetchone()
        if not row:
            raise ValueError("source_claim_not_found")
        result = dict(row)
        result["conditions"] = json.loads(result["conditions"])
        result["limitations"] = json.loads(result["limitations"])
        result["predictions"] = json.loads(result.get("predictions") or "[]")
        result["falsifiers"] = json.loads(result.get("falsifiers") or "[]")
        return result

    def claims_for_source(self, source_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM source_claims WHERE source_id=? ORDER BY created_at", (source_id,))]

    def claim_as_prior(self, claim_id: str, *, source_kind: str = "external") -> dict[str, Any]:
        """Return a reproducible prior payload; it is not a verified fact."""
        claim = self.get_claim(claim_id)
        return {
            "source_kind": source_kind,
            "title": claim["title"],
            "claim": claim["claim"],
            "environment": claim["conditions"],
            "source_refs": [claim["claim_id"], claim["source_id"], claim["content_hash"]],
            "reliability": float(claim["confidence"]),
            "predictions": claim["predictions"],
            "falsifiers": claim["falsifiers"],
        }
