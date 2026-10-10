from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .service import ButianService

class ApiHandler(BaseHTTPRequestHandler):
    service: ButianService | None = None
    def _send(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
    def do_GET(self):
        if self.path == "/api/status":
            self._send(200, {"ok": True, "service": "butian"}); return
        if self.path == "/api/tasks":
            self._send(200, {"tasks": self.service.store.list_tasks()}); return
        if self.path == "/api/questions":
            rows = [dict(row) for row in self.service.store.db.execute("SELECT * FROM questions ORDER BY created_at DESC")]
            self._send(200, {"questions": rows}); return
        self._send(404, {"error": "not_found"})
    def do_POST(self):
        try:
            body = self._body(); path = urlparse(self.path).path
            if path == "/api/members":
                self.service.add_member(body["member_id"], body["name"], body.get("role", "member"), body.get("responsibilities"), body.get("provider_id"), body.get("model_id")); self._send(201, {"ok": True}); return
            if path == "/api/sessions":
                self._send(201, self.service.create_session(body.get("mode", "solo"), body.get("dispatcher_member_id"), body.get("requested_model"), body.get("capabilities"))); return
            if path == "/api/tasks":
                task_id = self.service.store.create_task(body["session_id"], body["title"], body.get("assignee_id"), body.get("parent_id"), body.get("payload")); self._send(201, {"task_id": task_id}); return
            if path.startswith("/api/tasks/") and path.endswith("/claim"):
                task_id = path.split("/")[3]; self._send(200, self.service.store.claim_task(task_id, body["member_id"], body.get("lease_seconds", 300), body.get("token"))); return
            if path.startswith("/api/tasks/") and path.endswith("/release"):
                task_id = path.split("/")[3]; self.service.store.release_lease(task_id, body["lease_id"]); self._send(200, {"ok": True}); return
            if path == "/api/questions":
                qid = self.service.ask_user(body["task_id"], body["revision"], body["member_id"], body["question"]); self._send(201, {"question_id": qid}); return
            if path.startswith("/api/questions/") and path.endswith("/answer"):
                qid = path.split("/")[3]; self._send(200, {"task_id": self.service.answer_user(qid, body["answer"])}); return
            self._send(404, {"error": "not_found"})
        except (KeyError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": type(exc).__name__})

def serve(home=None, host="127.0.0.1", port=8765):
    service = ButianService(home); ApiHandler.service = service
    try: ThreadingHTTPServer((host, port), ApiHandler).serve_forever()
    finally: service.close()
