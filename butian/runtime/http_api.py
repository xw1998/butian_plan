from __future__ import annotations
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from ..version import VERSION
from .service import ButianService
from .experiments import ExperimentStore

class ApiHandler(BaseHTTPRequestHandler):
    service: ButianService | None = None
    web_root: Path | None = None
    experiments: ExperimentStore | None = None
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Butian-Version", VERSION)
        super().end_headers()

    def _send(self, code, data):
        if isinstance(data, dict) and data.get("status") in {"ok", "error"}:
            payload = data
        elif code >= 400:
            details = data if isinstance(data, dict) else {"message": str(data)}
            payload = {"status": "error", **details}
        else:
            payload = {"status": "ok", "data": data}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
    def _member_credentials(self, body=None):
        body = body or {}
        member_id = (self.headers.get("X-Butian-Member") or body.get("actor_member_id")
                     or body.get("current_member_id") or body.get("member_id")
                     or body.get("reviewer_member_id"))
        token = self.headers.get("X-Butian-Token") or body.get("token")
        return member_id, token

    def _local_mode(self):
        # Only a loopback-bound server may trust local identity selection.
        import ipaddress
        try:
            bound = ipaddress.ip_address(self.server.server_address[0])
            peer = ipaddress.ip_address(self.client_address[0])
            return bound.is_loopback and peer.is_loopback
        except ValueError:
            return False

    def _bind_local_actor(self, actor, token=None):
        # 本机免令牌模式下，服务层的成员边界仍要求一个可信的本地代表；
        # 不绑定就会让 goals/host-submit 这类走 call_tool 的接口在“没口令”时失败。
        if not actor or not self._local_mode():
            return
        if token:
            # 显式给了口令就按口令校验：本机免口令不等于“错口令也算对”。
            # 只撤销该成员的本地信任，不要把别的成员并发请求的信任一起清掉。
            self.service.disable_local_actor(actor)
            return
        try:
            self.service.enable_local_actor(actor)
        except ValueError:
            # BUTIAN_MCP_TOKEN_REQUIRED=1 等配置明确要求口令时不越权，交给服务层报错。
            pass

    def _authenticate_actor(self, actor, token):
        if self._local_mode():
            return bool(self.service.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (actor,)).fetchone())
        return self.service.store.authenticate_member(actor, token)

    def _require_member(self, body=None, target_member_id=None):
        actor, token = self._member_credentials(body)
        task_id = (body or {}).get("task_id")
        path = urlparse(self.path).path
        if not task_id and path.startswith("/api/tasks/") and path not in {"/api/tasks/review", "/api/tasks/tree"}:
            task_id = path.split("/")[3]
        if path.startswith("/api/questions/") and path.endswith("/answer"):
            question = self.service.store.db.execute(
                "SELECT task_id FROM questions WHERE question_id=?", (path.split("/")[3],)
            ).fetchone()
            task_id = question["task_id"] if question else None
        # Authentication must come from the caller; resource ownership is checked separately.
        # Inferring an actor from a task/provider session would turn an unauthenticated read
        # or write into an authenticated request.
        target = target_member_id or (body or {}).get("member_id") or actor
        if not actor:
            self._send(401, {"error": "actor_member_id_required"})
            return False
        if target and target != actor:
            self._send(403, {"error": "member_access_denied"})
            return False
        if not self._authenticate_actor(actor, token):
            self._send(401, {"error": "member_authentication_failed"})
            return False
        self._authenticated_member_id = actor
        self._bind_local_actor(actor, token)
        if (body or {}).get("actor_member_id") and (body or {}).get("actor_member_id") != actor:
            self._send(403, {"error": "actor_identity_mismatch"})
            return False
        if (body or {}).get("current_member_id") and (body or {}).get("current_member_id") != actor:
            self._send(403, {"error": "current_identity_mismatch"})
            return False
        if (body or {}).get("reviewer_member_id") and (body or {}).get("reviewer_member_id") != actor:
            self._send(403, {"error": "reviewer_identity_mismatch"})
            return False
        if (body or {}).get("dispatcher_member_id") and path in {"/api/sessions", "/api/goals/submit"} and (body or {})["dispatcher_member_id"] != actor:
            self._send(403, {"error": "dispatcher_identity_mismatch"})
            return False
        task_resource = path.startswith(("/api/tasks", "/api/questions", "/api/goals/", "/api/providers/"))
        if task_resource and task_id and not self.service.store.task_access(task_id, actor):
            self._send(403, {"error": "task_access_denied"})
            return False
        return True

    def _visible_member_id(self):
        actor = getattr(self, "_authenticated_member_id", None)
        if not actor:
            raise ValueError("member_authentication_required")
        return actor

    def _visible_tasks(self, actor):
        return [dict(row) for row in self.service.store.db.execute(
            "SELECT t.* FROM tasks t JOIN sessions s ON s.session_id=t.session_id "
            "WHERE s.dispatcher_member_id=? OR t.assignee_id=? ORDER BY t.rowid",
            (actor, actor),
        )]

    def do_GET(self):
        # All page reads share one SQLite connection with transactional writers.
        # Keep concurrent refresh requests outside another request's transaction.
        if self.service is None:
            return self._do_GET()
        with self.service.store._write_lock:
            return self._do_GET()

    def _do_GET(self):
        if not self.path.startswith("/api/"):
            relative = "index.html" if self.path == "/" else self.path.lstrip("/")
            root = (self.web_root or Path(__file__).resolve().parents[1] / "web").resolve()
            target = (root / relative).resolve()
            if target != root and root not in target.parents:
                self._send(403, {"error": "path_forbidden"}); return
            if target.is_file():
                content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "application/javascript; charset=utf-8" if target.suffix == ".js" else "application/octet-stream"
                raw = target.read_bytes(); self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            self._send(404, {"error": "not_found"}); return
        if self.path == "/api/migration/runs":
            self._send(409, {"error": "historical_identity_migration_out_of_scope"}); return
        public_get = {"/api/status", "/api/auth/options"}
        member_query_get = {"/api/tools", "/api/evolution"}
        if self.path not in public_get and urlparse(self.path).path not in member_query_get:
            if not self._require_member():
                return
        if self.path.startswith("/api/experiments/") and self.path.endswith("/failures"):
            experiment_id = self.path.split("/")[3]
            self._send(200, {"samples": self.experiments.failure_samples(experiment_id)}); return
        if self.path == "/api/experiments":
            self._send(200, {"experiments": [{**dict(row), "configuration": json.loads(row["configuration"])} for row in self.experiments.db.execute("SELECT * FROM experiments ORDER BY created_at DESC")]}); return
        if self.path.startswith("/api/experiments/") and self.path.count("/") == 3:
            experiment_id = self.path.split("/")[3]
            self._send(200, self.experiments.get(experiment_id)); return
        if self.path == "/api/auth/options":
            rows = self.service.store.db.execute(
                "SELECT m.member_id,m.name,m.role,CASE WHEN a.member_id IS NULL THEN 0 ELSE 1 END AS configured "
                "FROM members m LEFT JOIN members_auth a ON a.member_id=m.member_id ORDER BY m.name"
            )
            self._send(200, {"members": [dict(row) for row in rows], "local_mode": self._local_mode()}); return
        if self.path == "/api/status":
            latest = self.service.store.db.execute("SELECT event_type, payload, created_at FROM events WHERE event_type IN ('maintenance_tick','maintenance_failed') ORDER BY created_at DESC LIMIT 1").fetchone()
            latest_maintenance = None
            if latest:
                latest_maintenance = {"event_type": latest["event_type"], "payload": json.loads(latest["payload"]), "created_at": latest["created_at"]}
            self._send(200, {"ok": True, "service": "butian", "startup_recovery": self.service.startup_recovery, "maintenance": {"enabled": self.service._maintenance_enabled, "interval": self.service._maintenance_interval, "auto_dream": self.service._auto_dream, "latest": latest_maintenance, "external_learning": "authorized_url_queue", "rewarm_policy": "evidence_required"}}); return
        if urlparse(self.path).path == "/api/tools/host-authorizations":
            q = parse_qs(urlparse(self.path).query)
            member_id = (q.get("member_id") or [self._visible_member_id()])[0]
            if not member_id or not self._require_member({"member_id": member_id}, target_member_id=member_id):
                return
            self._send(200, {"authorizations": self.service.list_host_authorizations(member_id, status=(q.get("status") or [None])[0])}); return
        if self.path == "/api/tools/executions/running":
            self._send(200, {"executions": self.service.list_running_tool_executions(self._visible_member_id())}); return
        if self.path == "/api/dashboard":
            from .dashboard import snapshot
            self._send(200, snapshot(self.service, self._visible_member_id())); return
        if self.path == "/api/tasks":
            self._send(200, {"tasks": self._visible_tasks(self._visible_member_id())}); return
        if urlparse(self.path).path == "/api/tools":
            q = parse_qs(urlparse(self.path).query)
            member_id = (q.get("member_id") or [""])[0]
            if not member_id or not self._require_member({"member_id": member_id}, target_member_id=member_id):
                return
            status = (q.get("status") or [None])[0]
            self._send(200, {"tools": self.service.list_tools(member_id, status=status), "shares": self.service.list_tool_shares(member_id, status=status)}); return
        if self.path == "/api/questions":
            actor = self._visible_member_id()
            rows = [dict(row) for row in self.service.store.db.execute(
                "SELECT q.* FROM questions q JOIN tasks t ON t.task_id=q.task_id "
                "JOIN sessions s ON s.session_id=t.session_id "
                "WHERE s.dispatcher_member_id=? OR t.assignee_id=? ORDER BY q.created_at DESC",
                (actor, actor),
            )]
            self._send(200, {"questions": rows}); return
        if urlparse(self.path).path == "/api/tasks/tree":
            q = parse_qs(urlparse(self.path).query)
            actor = self._visible_member_id()
            session_id = q["session_id"][0] if "session_id" in q else None
            visible_session_ids = {
                row["session_id"] for row in self.service.store.db.execute(
                    "SELECT session_id FROM sessions WHERE dispatcher_member_id=?", (actor,)
                )
            }
            if session_id and session_id not in visible_session_ids:
                self._send(403, {"error": "session_access_denied"}); return
            if session_id:
                tree = self.service.task_tree(session_id)
            else:
                tree = [task for task in self._visible_tasks(actor)]
            self._send(200, {"tree": tree}); return
        if self.path == "/api/sessions":
            actor = self._visible_member_id()
            sessions = [dict(r) for r in self.service.store.db.execute(
                "SELECT * FROM sessions WHERE dispatcher_member_id=? ORDER BY created_at", (actor,)
            )]
            self._send(200, {"sessions": sessions, "dispatcher_config": self.service.get_dispatcher_config()}); return
        if self.path == "/api/members":
            self._send(200, {"members": [dict(row) for row in self.service.store.db.execute("SELECT * FROM members ORDER BY member_id")]}); return
        if self.path == "/api/migration/runs":
            self._send(409, {"error": "historical_identity_migration_out_of_scope"}); return
        if self.path == "/api/hypotheses":
            actor = self._visible_member_id()
            # These legacy ledgers have no owner column; fail closed rather than leak team data.
            self._send(200, {"hypotheses": []}); return
        if self.path == "/api/knowledge/claims":
            actor = self._visible_member_id()
            rows = [dict(row) for row in self.service.learning.db.execute("SELECT c.*, s.title, s.locator FROM source_claims c JOIN knowledge_sources s ON s.source_id=c.source_id WHERE c.owner_member_id=? ORDER BY c.created_at DESC", (actor,))]
            self._send(200, {"claims": rows}); return
        if self.path == "/api/knowledge/runs":
            self._send(200, {"runs": self.service.learning.list_learning_runs(owner_member_id=self._visible_member_id())}); return
        if self.path == "/api/events":
            actor = self._visible_member_id()
            self._send(200, {"events": [dict(row) for row in self.service.store.db.execute("SELECT e.* FROM events e WHERE e.aggregate_id IN (SELECT t.task_id FROM tasks t JOIN sessions s ON s.session_id=t.session_id WHERE s.dispatcher_member_id=? OR t.assignee_id=?) OR e.aggregate_id IN (SELECT provider_session_id FROM provider_sessions WHERE member_id=?) ORDER BY e.created_at DESC LIMIT 50", (actor, actor, actor))]}); return
        if urlparse(self.path).path == "/api/evolution":
            q = parse_qs(urlparse(self.path).query)
            member_id = (q.get("member_id") or [""])[0]
            if not member_id:
                self._send(400, {"status": "error", "error": "missing_field", "field": "member_id"}); return
            if not self._require_member({"member_id": member_id}, target_member_id=member_id):
                return
            self._send(200, {
                "skills": self.service.list_skills(member_id),
                "skill_events": self.service.list_skill_events(member_id),
                "dream_runs": self.service.list_dream_runs(member_id),
                "dream_candidates": self.service.list_dream_candidates(member_id),
                "dream_hypotheses": self.service.list_dream_hypotheses(member_id),
                "verification_queue": self.service.dream_verification_queue(member_id)["queue"],
                "distillations": self.service.list_dream_distillations(member_id),
                "abstractions": self.service.list_dream_abstractions(member_id),
            }); return
        self._send(404, {"error": "not_found"})
    def do_POST(self):
        try:
            body = self._body(); path = urlparse(self.path).path
            protected = ("/api/memory/", "/api/providers/", "/api/evolution/",
                         "/api/tools", "/api/tasks", "/api/goals/", "/api/sessions/",
                         "/api/evidence", "/api/hypotheses/", "/api/sessions",
                         "/api/knowledge/", "/api/questions", "/api/strategies/", "/api/experiments", "/api/tools/host-authorizations/", "/api/tools/executions/running", "/api/tools/executions/cancel")
            if path.startswith(protected) and not (path == "/api/providers/cancel" and not body.get("provider_session_id")) and not self._require_member(body):
                return
            if path == "/api/members":
                actor, token = self._member_credentials(body)
                if not actor:
                    self._send(401, {"error": "actor_member_id_required"}); return
                if not self._authenticate_actor(actor, token):
                    self._send(401, {"error": "member_authentication_failed"}); return
                if actor != body.get("member_id") and self.service.store.db.execute("SELECT role FROM members WHERE member_id=?", (actor,)).fetchone()["role"] != "bootstrap_admin":
                    self._send(403, {"error": "member_creation_forbidden"}); return
            if path == "/api/tools/issues/report":
                result = self.service.report_tool_issue(body["member_id"], body["tool_id"], body["version"], description=body["description"], execution_id=body.get("execution_id"), severity=body.get("severity", "bug"), environment=body.get("environment"), input_summary=body.get("input_summary"), expected_output=body.get("expected_output"), actual_output=body.get("actual_output"), reproduction_steps=body.get("reproduction_steps"), impact_scope=body.get("impact_scope")); self._send(201, result); return
            if path == "/api/tools/issues":
                self._send(200, {"issues": self.service.list_tool_issues(body["member_id"], body["tool_id"])}); return
            if path == "/api/tools/issues/repair":
                result = self.service.repair_tool_issue(body["member_id"], body["issue_id"], repair_version=body["repair_version"], diagnosis=body["diagnosis"]); self._send(200, result); return
            if path == "/api/tools/issues/confirm":
                result = self.service.confirm_tool_repair(body["member_id"], body["issue_id"], execution_id=body["execution_id"], resolved=body["resolved"]); self._send(200, result); return
            if path == "/api/tools/issues/triage":
                self._send(200, self.service.triage_tool_issue(body["member_id"], body["issue_id"], status=body.get("status", "triaged"), reason=body.get("reason", ""), due_seconds=body.get("due_seconds", 86400))); return
            if path == "/api/tools/issues/request-information":
                self._send(200, self.service.request_tool_issue_information(body["member_id"], body["issue_id"], request=body["request"])); return
            if path == "/api/tools/issues/provide-information":
                self._send(200, self.service.provide_tool_issue_information(body["member_id"], body["issue_id"], information=body["information"])); return
            if path == "/api/tools/issues/link-duplicate":
                self._send(200, self.service.link_duplicate_tool_issue(body["member_id"], body["issue_id"], body["duplicate_of"])); return
            if path == "/api/tools/issues/expire":
                self._send(200, {"issues": self.service.expire_tool_issue_escalations(body["member_id"], now=body.get("now"))}); return
            if path == "/api/tools/issues/escalations":
                self._send(200, {"work_items": self.service.list_tool_issue_escalations(body["member_id"], status=body.get("status"))}); return
            if path == "/api/tools/issues/escalations/claim":
                self._send(200, self.service.claim_tool_issue_escalation(body["member_id"], body["work_item_id"])); return
            if path == "/api/tools/issues/escalations/resolve":
                self._send(200, self.service.resolve_tool_issue_escalation(body["member_id"], body["work_item_id"], status=body["status"], resolution=body.get("resolution", ""))); return
            if path == "/api/tools/issues/pair":
                self._send(200, self.service.record_tool_issue_pair(body["member_id"], body["issue_id"], repair_version=body["repair_version"], environment=body.get("environment"), input_hash=body["input_hash"], before_outcome=body["before_outcome"], after_outcome=body["after_outcome"])); return
            if path == "/api/tools/issues/statistics":
                self._send(200, self.service.tool_issue_statistics(body["member_id"], body["tool_id"], version=body.get("version"), environment=body.get("environment"))); return
            if path == "/api/tools/circuit-breaker/open":
                self._send(200, self.service.open_tool_circuit_breaker(body["member_id"], body["tool_id"], body["version"], scope=body.get("scope", "version"), reason=body.get("reason", "risk"))); return
            if path == "/api/tools/circuit-breaker/recover":
                self._send(200, self.service.recover_tool_circuit_breaker(body["member_id"], body["tool_id"], body["version"], scope=body.get("scope", "version"))); return
            if path == "/api/tools/execute":
                result = self.service.execute_tool(body["member_id"], body["tool_id"], body["version"], body.get("input"), dry_run=body.get("dry_run", False), verification_only=body.get("verification_only", False), task_id=body.get("task_id"), share_id=body.get("share_id"), security_review_issue_id=body.get("security_review_issue_id"), timeout_seconds=body.get("timeout_seconds", 10), max_output_bytes=body.get("max_output_bytes", 65536)); self._send(200, result); return
            if path == "/api/tools/host-authorizations/grant":
                actor, _ = self._member_credentials(body)
                self._send(201, self.service.grant_host_authorization(actor, body["tool_id"], body["version"], capabilities=body["capabilities"], reason=body["reason"])); return
            if path == "/api/tools/host-authorizations/list":
                actor, _ = self._member_credentials(body)
                self._send(200, {"authorizations": self.service.list_host_authorizations(actor, status=body.get("status"))}); return
            if path == "/api/tools/host-authorizations/revoke":
                actor, _ = self._member_credentials(body)
                self._send(200, self.service.revoke_host_authorization(actor, body["authorization_id"], reason=body["reason"])); return
            if path == "/api/tools/executions/running":
                actor, _ = self._member_credentials(body)
                self._send(200, {"executions": self.service.list_running_tool_executions(actor)}); return
            if path == "/api/tools/executions/cancel":
                actor, _ = self._member_credentials(body)
                self._send(200, self.service.cancel_tool_execution(actor, body["execution_id"], reason=body["reason"])); return
            if path == "/api/tools/executions":
                self._send(200, {"executions": self.service.list_tool_executions(body["member_id"], tool_id=body.get("tool_id"), limit=body.get("limit", 50))}); return
            if path == "/api/tools/propose":
                result = self.service.propose_tool(body["member_id"], body["name"], body["description"], reuse_lower=body["reuse_lower"], manual_cost_upper=body["manual_cost_upper"], call_cost_lower=body["call_cost_lower"], build_cost_upper=body["build_cost_upper"], maintenance_cost_upper=body["maintenance_cost_upper"], risk_cost_upper=body["risk_cost_upper"], manifest=body.get("manifest"), manual_cost_lower=body.get("manual_cost_lower"), call_cost_upper=body.get("call_cost_upper"), cost_unit=body.get("cost_unit")); self._send(201, result); return
            if path == "/api/tools/version":
                result = self.service.add_tool_version(body["member_id"], body["tool_id"], body["content"], body["manifest"]); self._send(201, result); return
            if path == "/api/tools/verify":
                result = self.service.verify_tool(body["member_id"], body["tool_id"], body["version"], body["outcome"], independent_key=body["independent_key"], task_id=body.get("task_id"), evidence_ids=body.get("evidence_ids"), environment=body.get("environment"), execution_id=body.get("execution_id"), input_hash=body.get("input_hash"), expected_output=body.get("expected_output")); self._send(200, result); return
            if path == "/api/tools/share":
                result = self.service.share_tool(body["member_id"], body["tool_id"], body["recipient_member_id"], permission=body.get("permission", "team_readonly")); self._send(201, result); return
            if path == "/api/tools/accept":
                self._send(200, self.service.accept_tool_share(body["member_id"], body["share_id"])); return
            if path == "/api/tools/trial":
                self._send(200, self.service.trial_tool(body["member_id"], body["share_id"], body["outcome"], independent_key=body["independent_key"], task_id=body.get("task_id"), environment=body.get("environment"), evidence_ids=body.get("evidence_ids"), execution_id=body.get("execution_id"), input_hash=body.get("input_hash"), expected_output=body.get("expected_output"))); return
            if path == "/api/tools/archive":
                self._send(200, self.service.archive_tool(body["member_id"], body["tool_id"], reason=body.get("reason", "maintenance"))); return
            if path == "/api/tools/restore":
                self._send(200, self.service.restore_tool(body["member_id"], body["tool_id"])); return
            if path == "/api/memory/recall":
                result = self.service.recall_for_decision(body["member_id"], body["query"], decision_id=body["decision_id"], environment=body.get("environment"), limit=body.get("limit", 20))
                self._send(200, result); return
            if path == "/api/memory/recall/outcome":
                self.service.record_recall_outcome(body["member_id"], body["recall_id"], used_memory_ids=body.get("used_memory_ids", []), missed=body.get("missed", False), useful_memory_ids=body.get("useful_memory_ids"), harmful_memory_ids=body.get("harmful_memory_ids"), evidence_ref=body.get("evidence_ref"))
                self._send(200, {"recorded": True}); return
            if path == "/api/memory/practice":
                result = self.service.record_practice_outcome(body["member_id"], memory_id=body["memory_id"], kind=body["kind"], content=body["content"], outcome=body["outcome"], independent_key=body["independent_key"], evidence_ref=body.get("evidence_ref"), environment=body.get("environment"), source_event_ids=body.get("source_event_ids"))
                self._send(201, result); return
            if path == "/api/experiments/compare":
                metrics = body.get("metrics")
                if metrics is not None:
                    self._send(200, self.experiments.batch_compare(body["experiment_ids"], metrics)); return
                self._send(200, {"comparisons": self.experiments.compare(body["experiment_ids"], body["metric"])}); return
            if path == "/api/experiments/batch":
                result = self.experiments.run_batch(body["name"], body["variants"], task_package=body["task_package"], dataset_version=body["dataset_version"], code_commit=body["code_commit"], configuration=body.get("configuration"), environment=body.get("environment"), repeats=body.get("repeats", 1)); self._send(201, result); return
            if path == "/api/experiments":
                experiment_id = self.experiments.create(body["name"], task_package=body["task_package"], dataset_version=body["dataset_version"], code_commit=body["code_commit"], configuration=body.get("configuration", {}), provider_version=body["provider_version"], environment=body.get("environment"))
                self._send(201, {"experiment_id": experiment_id}); return
            if path.startswith("/api/experiments/") and path.endswith("/events"):
                experiment_id = path.split("/")[3]; event_id = self.experiments.add_event(experiment_id, body["event_type"], body.get("payload", {})); self._send(201, {"event_id": event_id}); return
            if path.startswith("/api/experiments/") and path.endswith("/summary"):
                experiment_id = path.split("/")[3]; self._send(200, self.experiments.summarize(experiment_id, body["metric"])); return
            if path == "/api/evidence/verify":
                actor, _ = self._member_credentials(body)
                self._send(200, self.service.verify_evidence(body["evidence_id"], actor_member_id=actor)); return
            if path == "/api/evidence":
                actor, token = self._member_credentials(body)
                if body.get("owner_member_id") not in {None, actor}:
                    self._send(403, {"error": "member_access_denied"}); return
                evidence_id = self.service.record_evidence(body["path"], owner_member_id=actor, actor_member_id=actor, token=token, root=body.get("root"))
                self._send(201, {"evidence_id": evidence_id}); return
            if path == "/api/hypotheses/propose":
                source_kinds = body.get("source_kinds") or ["unknown"]
                assessment = self.service.assess_hypothesis(source_kinds, environment_match=float(body.get("environment_match", 0.5)), independent_sources=int(body.get("independent_sources", 0)), counterexamples=int(body.get("counterexamples", 0)))
                hypothesis_id = self.service.propose_hypothesis(body["kind"], body["statement"], prior_ids=body.get("prior_ids", []), rationale=body.get("rationale", ""), conditions=body.get("conditions"), predictions=body.get("predictions"), falsifiers=body.get("falsifiers"), assessment=assessment, owner_member_id=self._visible_member_id())
                self._send(201, {"hypothesis_id": hypothesis_id, "assessment": {"reliability": assessment.reliability, "rationale": assessment.rationale}}); return
            if path == "/api/knowledge/sources":
                source_id = self.service.learning.add_source(body["source_type"], body["locator"], body["title"], body["content_hash"], owner_member_id=self._visible_member_id(), metadata=body.get("metadata")); self._send(201, {"source_id": source_id}); return
            if path == "/api/knowledge/capture":
                result = self.service.learning.capture_url(body["url"], owner_member_id=self._visible_member_id(), title=body.get("title"), source_type=body.get("source_type", "web"), timeout=body.get("timeout", 10), max_bytes=body.get("max_bytes", 1_000_000))
                result.pop("content", None)
                self._send(201, result); return
            if path == "/api/knowledge/sources/verify":
                result = self.service.learning.verify_source_content(body["source_id"], body["content"], owner_member_id=self._visible_member_id()); self._send(200, result); return
            if path == "/api/knowledge/claims":
                claim_id = self.service.learning.add_claim(body["source_id"], body["claim"], owner_member_id=self._visible_member_id(), conditions=body.get("conditions"), limitations=body.get("limitations"), confidence=body.get("confidence", 0.0), predictions=body.get("predictions"), falsifiers=body.get("falsifiers")); self._send(201, {"claim_id": claim_id}); return
            if path == "/api/hypotheses/from-claim":
                self._send(201, self.service.promote_claim_to_prior(body["claim_id"], owner_member_id=self._visible_member_id())); return
            if path == "/api/hypotheses/prepare-claim-practice":
                self._send(201, self.service.prepare_claim_practice(body["claim_id"], owner_member_id=self._visible_member_id(), description=body["description"], kind=body.get("kind", "practice_hypothesis"), cost=float(body.get("cost", 1.0)), risk=float(body.get("risk", 0.0)), information_gain=float(body.get("information_gain", 1.0)))); return
            if path == "/api/hypotheses/authorize":
                action_id = self.service.authorize_hypothesis(body["hypothesis_id"], body["description"], cost=body["cost"], risk=body["risk"], information_gain=body["information_gain"], owner_member_id=self._visible_member_id()); self._send(201, {"action_id": action_id}); return
            if path == "/api/hypotheses/grant":
                actor = self._visible_member_id()
                if body.get("actor_member_id") and body["actor_member_id"] != actor:
                    self._send(403, {"error": "actor_identity_mismatch"}); return
                self.service.grant_hypothesis_authorization(body["action_id"], actor_member_id=actor, reason=body["reason"], authorized=body["authorized"], owner_member_id=actor); self._send(200, {"ok": True}); return
            if path == "/api/hypotheses/start":
                self.service.start_hypothesis_action(body["action_id"], owner_member_id=self._visible_member_id()); self._send(200, {"ok": True}); return
            if path == "/api/hypotheses/get":
                self._send(200, self.service.get_hypothesis(body["hypothesis_id"], owner_member_id=self._visible_member_id())); return
            if path == "/api/hypotheses/observe":
                self._send(200, self.service.observe_hypothesis(body["action_id"], body["outcome"], body.get("evidence_ids", []), independent_key=body["independent_key"], notes=body.get("notes", ""), environment=body.get("environment"), actor_member_id=body.get("actor_member_id"), member_id=body.get("member_id"), owner_member_id=self._visible_member_id())); return
            if path == "/api/members":
                self.service.add_member(body["member_id"], body["name"], body.get("role", "member"), body.get("responsibilities"), body.get("provider_id"), body.get("model_id")); self._send(201, {"member_id": body["member_id"]}); return
            if path == "/api/migration/preflight":
                self._send(409, {"error": "historical_identity_migration_out_of_scope"}); return
            if path == "/api/migration/import":
                self._send(409, {"error": "historical_identity_migration_out_of_scope"}); return
            if path == "/api/goals/submit":
                self._send(201, self.service.submit_goal(body["goal"], mode=body.get("mode", "solo"), member_id=body.get("member_id"), dispatcher_member_id=body.get("dispatcher_member_id"), requested_model=body.get("requested_model"), capabilities=body.get("capabilities"), environment=body.get("environment"), member_ids=body.get("member_ids"), subtasks=body.get("subtasks"), problem=body.get("problem"))); return
            if path in {"/api/goals/host-submit", "/api/goals/problem/get", "/api/goals/problem/update", "/api/goals/problem/resume"}:
                from ..mcp_server import call_tool
                tool = {"/api/goals/host-submit": "butian_goal_submit", "/api/goals/problem/get": "butian_problem_get", "/api/goals/problem/update": "butian_problem_update", "/api/goals/problem/resume": "butian_problem_resume"}[path]
                actor, token = self._member_credentials(body)
                self._send(200, call_tool(self.service, tool, {**body, "actor_member_id": actor, "token": token or ""})); return
            if path in {"/api/executions/export", "/api/executions/submit"}:
                from ..mcp_server import call_tool
                actor, token = self._member_credentials(body)
                tool = "butian_execution_export" if path.endswith("/export") else "butian_execution_submit"
                self._send(200, call_tool(self.service, tool, {**body, "actor_member_id": actor, "token": token or ""})); return
            if path == "/api/goals/run-solo":
                self._send(200, self.service.run_solo_task(body["task_id"], actor_member_id=self._member_credentials(body)[0])); return
            if path == "/api/goals/run-team":
                if not self.service.store.task_access(body["task_id"], self._member_credentials(body)[0]):
                    self._send(403, {"error": "task_access_denied"}); return
                self._send(200, self.service.run_team_goal(body["task_id"])); return
            if path == "/api/providers/start":
                actor, token = self._member_credentials(body)
                if not actor or not self._authenticate_actor(actor, token):
                    self._send(401, {"error": "member_authentication_failed"}); return
                target_member_id = body.get("member_id")
                if not target_member_id or target_member_id != actor:
                    self._send(403, {"error": "member_access_denied"}); return
                if body.get("task_id") and not self.service.store.task_access(body["task_id"], actor):
                    self._send(403, {"error": "task_access_denied"}); return
                self._send(201, self.service.start_provider_task(target_member_id, body["prompt"], task_id=body.get("task_id"), provider_id=body.get("provider_id"), model=body.get("model"))); return
            if path == "/api/providers/consume":
                actor = self._member_credentials(body)[0]
                if not self.service.store.provider_session_access(body["provider_session_id"], actor):
                    self._send(403, {"error": "provider_session_access_denied"}); return
                self._send(200, self.service.consume_provider_task(body["provider_session_id"], body.get("prompt"), actor_member_id=actor)); return
            if path == "/api/evolution/compose":
                self._send(201, self.service.compose_memories(body["member_id"], body["result_memory_id"], body["component_memory_ids"], content=body["content"], kind=body.get("kind", "compound"), environment=body.get("environment"), source_event_ids=body.get("source_event_ids"), composition_bonus=body.get("composition_bonus", 0.0), redundancy_penalty=body.get("redundancy_penalty", 0.0), conflict_penalty=body.get("conflict_penalty", 0.0))); return
            if path == "/api/evolution/skill/update":
                self._send(200, self.service.update_skill(body["member_id"], body["domain"], level=body["level"], marginal=body["marginal"], contradiction=body.get("contradiction", False), novel_environment=body.get("novel_environment", False), independent_successes=body.get("independent_successes", 0), composition_successes=body.get("composition_successes", 0), regression_evidence=body.get("regression_evidence", False))); return
            if path == "/api/evolution/degradation":
                self._send(200, self.service.assess_memory_degradation(body["member_id"], body["memory_id"], now=body.get("now"), half_life_days=body.get("half_life_days", 30.0), current_environment=body.get("current_environment"), consecutive_failures=body.get("consecutive_failures", 0), contradiction_count=body.get("contradiction_count", 0), dependency_drift_score=body.get("dependency_drift_score", 0.0), hard_failure=body.get("hard_failure", False))); return
            if path == "/api/evolution/cool":
                self._send(200, self.service.cool_memory(body["member_id"], body["memory_id"], reason=body.get("reason", "stale"))); return
            if path == "/api/evolution/rewarm":
                self._send(200, self.service.rewarm_memory(body["member_id"], body["memory_id"], success=body["success"], evidence_ref=body.get("evidence_ref"))); return
            if path == "/api/evolution/versions":
                self._send(200, self.service.memory_versions(body["member_id"], body["memory_id"])); return
            if path == "/api/evolution/validation/create":
                self._send(201, self.service.create_validation_batch(body["member_id"], body["memory_id"], sample_count=body["sample_count"], notes=body.get("notes"), batch_id=body.get("batch_id"))); return
            if path == "/api/evolution/validation/update":
                self._send(200, self.service.update_validation_batch(body["member_id"], body["batch_id"], body["state"], pass_count=body.get("pass_count", 0), fail_count=body.get("fail_count", 0), evidence_refs=body.get("evidence_refs"), notes=body.get("notes"))); return
            if path == "/api/evolution/validation/list":
                self._send(200, self.service.list_validation_batches(body["member_id"], body["memory_id"])); return
            if path == "/api/evolution/dream/run":
                self._send(200, self.service.dream_run(body["member_id"], min_fragments=body.get("min_fragments", 3), min_interval=body.get("min_interval", 3600), budget=body.get("budget", 10), lock_timeout=body.get("lock_timeout", 7200))); return
            if path == "/api/evolution/dream/distill":
                self._send(201, self.service.dream_distill(body["member_id"], body["memory_ids"], summary=body["summary"], merged_items=body.get("merged_items"), conflicts=body.get("conflicts"), discarded_items=body.get("discarded_items"), information_loss=body.get("information_loss", 0.0))); return
            if path == "/api/evolution/dream/abstract":
                self._send(201, self.service.dream_abstract(body["member_id"], body["memory_ids"], pattern=body["pattern"], supports=body.get("supports"), counterexamples=body.get("counterexamples"), applicability=body.get("applicability"), failure_conditions=body.get("failure_conditions"), abstraction_level=body.get("abstraction_level", 1))); return
            if path == "/api/evolution/dream/propose":
                self._send(201, self.service.dream_propose_hypothesis(body["member_id"], body["candidate_id"], body["statement"], prior_reliability=body["prior_reliability"], argument=body["argument"], evidence_refs=body.get("evidence_refs"))); return
            if path == "/api/evolution/dream/schedule":
                self._send(201, self.service.dream_schedule_verification(body["member_id"], body["hypothesis_id"], body["description"], cost=body["cost"], risk=body["risk"], expected_information_gain=body["expected_information_gain"], authorized=body.get("authorized", False))); return
            if path == "/api/evolution/dream/start":
                self._send(200, self.service.dream_start_verification(body["member_id"], body["action_id"])); return
            if path == "/api/evolution/dream/verify":
                self._send(200, self.service.dream_record_verification(body["member_id"], body["hypothesis_id"], action_id=body.get("action_id"), outcome=body["outcome"], evidence_refs=body.get("evidence_refs", []), new_memory_id=body.get("new_memory_id"), new_content=body.get("new_content"))); return
            if path == "/api/sessions":
                self._send(201, self.service.create_session(body.get("mode", "solo"), body.get("dispatcher_member_id"), body.get("requested_model"), body.get("capabilities"), current_member_id=body.get("current_member_id"))); return
            if path == "/api/sessions/switch":
                actor = self._visible_member_id()
                session = self.service.store.session(body["session_id"])
                if session.get("dispatcher_member_id") != actor:
                    self._send(403, {"error": "session_access_denied"}); return
                self._send(200, self.service.switch_session_mode(body["session_id"], body["mode"], current_member_id=actor)); return
            if path == "/api/sessions/dispatcher":
                role = self.service.store.db.execute("SELECT role FROM members WHERE member_id=?", (self._visible_member_id(),)).fetchone()
                if not role or role["role"] != "bootstrap_admin":
                    self._send(403, {"error": "admin_authorization_required"}); return
                self.service.set_dispatcher_config(body["member_id"]); self._send(200, {"dispatcher_member_id": body["member_id"]}); return
            if path == "/api/tasks/review":
                actor, _ = self._member_credentials(body)
                if not actor or body.get("reviewer_member_id") not in {None, actor}:
                    self._send(403, {"error": "reviewer_identity_mismatch"}); return
                self._send(200, self.service.review_task(body["task_id"], body["revision"], body["verdict"], reviewer_member_id=actor, actor_member_id=actor, notes=body.get("notes", ""), rework_limit=body.get("rework_limit", 3))); return
            if path == "/api/tasks":
                actor, _ = self._member_credentials(body)
                session = self.service.store.session(body["session_id"])
                if actor != session.get("dispatcher_member_id"):
                    self._send(403, {"error": "task_create_forbidden"}); return
                task_id = self.service.store.create_task(body["session_id"], body["title"], body.get("assignee_id"), body.get("parent_id"), body.get("payload")); self._send(201, {"task_id": task_id}); return
            if path.startswith("/api/tasks/") and path.endswith("/claim"):
                task_id = path.split("/")[3]; self._send(200, self.service.store.claim_task(task_id, body["member_id"], body.get("lease_seconds", 300), body.get("token"))); return
            if path.startswith("/api/tasks/") and path.endswith("/release"):
                task_id = path.split("/")[3]; self.service.store.release_lease(task_id, body["lease_id"]); self._send(200, {"ok": True}); return
            if path.startswith("/api/tasks/") and path.endswith("/heartbeat"):
                task_id = path.split("/")[3]; self.service.store.heartbeat(task_id, body["lease_id"], body.get("lease_seconds", 300)); self._send(200, {"ok": True}); return
            if path.startswith("/api/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[3]; self._send(200, self.service.cancel_task(task_id, body["revision"], body.get("operation_key"))); return
            if path == "/api/providers/cancel":
                actor = self._member_credentials(body)[0]
                if not self.service.store.provider_session_access(body["provider_session_id"], actor):
                    self._send(403, {"error": "provider_session_access_denied"}); return
                self._send(200, self.service.cancel_provider_task(body["provider_session_id"], actor_member_id=actor)); return
            if path == "/api/questions":
                qid = self.service.ask_user(body["task_id"], body["revision"], body["member_id"], body["question"]); self._send(201, {"question_id": qid}); return
            if path == "/api/strategies/recommend":
                self._send(200, {"recommendations": self.service.recommend_strategies(body["task_features"])}); return
            if path == "/api/strategies/compose":
                self._send(200, self.service.compose_strategies(body["member_ids"], body["task_features"])); return
            if path.startswith("/api/tasks/") and path.endswith("/strategy"):
                task_id = path.split("/")[3]; self._send(200, self.service.assign_strategy(task_id, body["strategy_id"], body.get("stages"), body.get("reason"), body.get("decision_by"))); return
            if path.startswith("/api/tasks/") and path.endswith("/strategy-step"):
                task_id = path.split("/")[3]; step_id = self.service.store.record_strategy_step(task_id, body["revision"], body["strategy_id"], body["stage"], member_id=body.get("member_id"), status=body.get("status", "completed"), reason=body.get("reason"), output_ref=body.get("output_ref")); self._send(201, {"step_id": step_id}); return
            if path.startswith("/api/questions/") and path.endswith("/answer"):
                qid = path.split("/")[3]; self._send(200, {"task_id": self.service.answer_user(qid, body["answer"])}); return
            self._send(404, {"error": "not_found"})
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"status": "error", "error": "invalid_json"})
        except KeyError as exc:
            self._send(400, {"status": "error", "error": "missing_field", "field": str(exc).strip("'")})
        except ValueError as exc:
            self._send(400, {"status": "error", "error": str(exc)})
        except Exception as exc:
            self._send(500, {"status": "error", "error": type(exc).__name__})

def serve(home=None, host="127.0.0.1", port=8765):
    # Bind before opening the runtime so an old instance cannot trigger initialization.
    try:
        server = ThreadingHTTPServer((host, port), ApiHandler)
    except OSError as exc:
        raise RuntimeError(f"Cannot start Butian {VERSION} at {host}:{port}: port unavailable. Close the previous console process, then run the same command again; updating files does not reload a running server.") from exc
    service = ButianService(home); ApiHandler.service = service
    ApiHandler.experiments = ExperimentStore(Path(home or service.home) / "experiments.db")
    ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    try:
        print(f"Butian {VERSION} console: http://{host}:{port} (loopback requires member selection only)", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
        if ApiHandler.experiments:
            ApiHandler.experiments.close()
