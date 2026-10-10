from __future__ import annotations
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from .service import ButianService
from .experiments import ExperimentStore

class ApiHandler(BaseHTTPRequestHandler):
    service: ButianService | None = None
    web_root: Path | None = None
    experiments: ExperimentStore | None = None
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
    def do_GET(self):
        if not self.path.startswith("/api/"):
            relative = "index.html" if self.path == "/" else self.path.lstrip("/")
            root = (self.web_root or Path(__file__).resolve().parents[2] / "web").resolve()
            target = (root / relative).resolve()
            if target != root and root not in target.parents:
                self._send(403, {"error": "path_forbidden"}); return
            if target.is_file():
                content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "application/javascript; charset=utf-8" if target.suffix == ".js" else "application/octet-stream"
                raw = target.read_bytes(); self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            self._send(404, {"error": "not_found"}); return
        if self.path.startswith("/api/experiments/") and self.path.endswith("/failures"):
            experiment_id = self.path.split("/")[3]
            self._send(200, {"samples": self.experiments.failure_samples(experiment_id)}); return
        if self.path == "/api/experiments":
            self._send(200, {"experiments": [{**dict(row), "configuration": json.loads(row["configuration"])} for row in self.experiments.db.execute("SELECT * FROM experiments ORDER BY created_at DESC")]}); return
        if self.path.startswith("/api/experiments/") and self.path.count("/") == 3:
            experiment_id = self.path.split("/")[3]
            self._send(200, self.experiments.get(experiment_id)); return
        if self.path == "/api/status":
            latest = self.service.store.db.execute("SELECT event_type, payload, created_at FROM events WHERE event_type IN ('maintenance_tick','maintenance_failed') ORDER BY created_at DESC LIMIT 1").fetchone()
            latest_maintenance = None
            if latest:
                latest_maintenance = {"event_type": latest["event_type"], "payload": json.loads(latest["payload"]), "created_at": latest["created_at"]}
            self._send(200, {"ok": True, "service": "butian", "startup_recovery": self.service.startup_recovery, "maintenance": {"enabled": self.service._maintenance_enabled, "interval": self.service._maintenance_interval, "latest": latest_maintenance, "external_learning": "url_capture_with_audit_ledger", "rewarm_policy": "evidence_required"}}); return
        if self.path == "/api/tasks":
            self._send(200, {"tasks": self.service.store.list_tasks()}); return
        if self.path == "/api/questions":
            rows = [dict(row) for row in self.service.store.db.execute("SELECT * FROM questions ORDER BY created_at DESC")]
            self._send(200, {"questions": rows}); return
        if urlparse(self.path).path == "/api/tasks/tree":
            q = parse_qs(urlparse(self.path).query)
            self._send(200, {"tree": self.service.task_tree(q["session_id"][0] if "session_id" in q else None)}); return
        if self.path == "/api/sessions":
            self._send(200, {"sessions": [dict(r) for r in self.service.store.db.execute("SELECT * FROM sessions ORDER BY created_at")], "dispatcher_config": self.service.get_dispatcher_config()}); return
        if self.path == "/api/members":
            self._send(200, {"members": [dict(row) for row in self.service.store.db.execute("SELECT * FROM members ORDER BY member_id")]}); return
        if self.path == "/api/migration/runs":
            self._send(200, {"runs": self.service.list_migration_runs()}); return
        if self.path == "/api/hypotheses":
            self._send(200, {"hypotheses": [dict(row) for row in self.service.store.db.execute("SELECT * FROM practice_hypotheses ORDER BY updated_at DESC")]}); return
        if self.path == "/api/knowledge/claims":
            self._send(200, {"claims": [dict(row) for row in self.service.store.db.execute("SELECT * FROM source_claims ORDER BY created_at DESC")]}); return
        if self.path == "/api/knowledge/runs":
            self._send(200, {"runs": self.service.learning.list_learning_runs()}); return
        if self.path == "/api/events":
            self._send(200, {"events": [dict(row) for row in self.service.store.db.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 50")]}); return
        if urlparse(self.path).path == "/api/evolution":
            q = parse_qs(urlparse(self.path).query)
            member_id = (q.get("member_id") or [""])[0]
            if not member_id:
                self._send(400, {"status": "error", "error": "missing_field", "field": "member_id"}); return
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
            if path == "/api/evidence":
                evidence_id = self.service.record_evidence(body["path"], owner_member_id=body.get("owner_member_id"), actor_member_id=body.get("actor_member_id"), token=body.get("token"), root=body.get("root"))
                self._send(201, {"evidence_id": evidence_id}); return
            if path == "/api/hypotheses/propose":
                source_kinds = body.get("source_kinds") or ["unknown"]
                assessment = self.service.assess_hypothesis(source_kinds, environment_match=float(body.get("environment_match", 0.5)), independent_sources=int(body.get("independent_sources", 0)), counterexamples=int(body.get("counterexamples", 0)))
                hypothesis_id = self.service.propose_hypothesis(body["kind"], body["statement"], prior_ids=body.get("prior_ids", []), rationale=body.get("rationale", ""), conditions=body.get("conditions"), predictions=body.get("predictions"), falsifiers=body.get("falsifiers"), assessment=assessment)
                self._send(201, {"hypothesis_id": hypothesis_id, "assessment": {"reliability": assessment.reliability, "rationale": assessment.rationale}}); return
            if path == "/api/knowledge/sources":
                source_id = self.service.learning.add_source(body["source_type"], body["locator"], body["title"], body["content_hash"], metadata=body.get("metadata")); self._send(201, {"source_id": source_id}); return
            if path == "/api/knowledge/capture":
                result = self.service.learning.capture_url(body["url"], title=body.get("title"), source_type=body.get("source_type", "web"), timeout=float(body.get("timeout", 10)), max_bytes=int(body.get("max_bytes", 1_000_000)))
                result.pop("content", None)
                self._send(201, result); return
            if path == "/api/knowledge/sources/verify":
                result = self.service.learning.verify_source_content(body["source_id"], body["content"]); self._send(200, result); return
            if path == "/api/knowledge/claims":
                claim_id = self.service.learning.add_claim(body["source_id"], body["claim"], conditions=body.get("conditions"), limitations=body.get("limitations"), confidence=body.get("confidence", 0.0), predictions=body.get("predictions"), falsifiers=body.get("falsifiers")); self._send(201, {"claim_id": claim_id}); return
            if path == "/api/hypotheses/from-claim":
                self._send(201, self.service.promote_claim_to_prior(body["claim_id"])); return
            if path == "/api/hypotheses/authorize":
                action_id = self.service.authorize_hypothesis(body["hypothesis_id"], body["description"], cost=body["cost"], risk=body["risk"], information_gain=body["information_gain"]); self._send(201, {"action_id": action_id}); return
            if path == "/api/hypotheses/grant":
                self.service.grant_hypothesis_authorization(body["action_id"], actor_member_id=body["actor_member_id"], reason=body["reason"], authorized=body["authorized"]); self._send(200, {"ok": True}); return
            if path == "/api/hypotheses/start":
                self.service.start_hypothesis_action(body["action_id"]); self._send(200, {"ok": True}); return
            if path == "/api/hypotheses/get":
                self._send(200, self.service.get_hypothesis(body["hypothesis_id"])); return
            if path == "/api/hypotheses/observe":
                self._send(200, self.service.observe_hypothesis(body["action_id"], body["outcome"], body.get("evidence_ids", []), independent_key=body["independent_key"], notes=body.get("notes", ""), environment=body.get("environment"), actor_member_id=body.get("actor_member_id"), member_id=body.get("member_id"))); return
            if path == "/api/members":
                self.service.add_member(body["member_id"], body["name"], body.get("role", "member"), body.get("responsibilities"), body.get("provider_id"), body.get("model_id")); self._send(201, {"member_id": body["member_id"]}); return
            if path == "/api/migration/import":
                result = self.service.import_members(body["source_path"], roles_dir=body.get("roles_dir"), update_existing=body.get("update_existing", False), import_memories=body.get("import_memories", True)); self._send(201, result); return
            if path == "/api/providers/start":
                self._send(201, self.service.start_provider_task(body["member_id"], body["prompt"], task_id=body.get("task_id"), provider_id=body.get("provider_id"), model=body.get("model"))); return
            if path == "/api/providers/consume":
                self._send(200, self.service.consume_provider_task(body["provider_session_id"], body["prompt"])); return
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
            if path == "/api/evolution/dream/verify":
                self._send(200, self.service.dream_record_verification(body["member_id"], body["hypothesis_id"], outcome=body["outcome"], evidence_refs=body.get("evidence_refs", []), new_memory_id=body.get("new_memory_id"), new_content=body.get("new_content"))); return
            if path == "/api/sessions":
                self._send(201, self.service.create_session(body.get("mode", "solo"), body.get("dispatcher_member_id"), body.get("requested_model"), body.get("capabilities"), current_member_id=body.get("current_member_id"))); return
            if path == "/api/sessions/switch":
                self._send(200, self.service.switch_session_mode(body["session_id"], body["mode"], current_member_id=body.get("current_member_id"))); return
            if path == "/api/sessions/dispatcher":
                self.service.set_dispatcher_config(body["member_id"]); self._send(200, {"dispatcher_member_id": body["member_id"]}); return
            if path == "/api/tasks/review":
                self._send(200, self.service.review_task(body["task_id"], body["revision"], body["verdict"], reviewer_member_id=body.get("reviewer_member_id"), notes=body.get("notes", ""), rework_limit=body.get("rework_limit", 3))); return
            if path == "/api/tasks":
                task_id = self.service.store.create_task(body["session_id"], body["title"], body.get("assignee_id"), body.get("parent_id"), body.get("payload")); self._send(201, {"task_id": task_id}); return
            if path.startswith("/api/tasks/") and path.endswith("/claim"):
                task_id = path.split("/")[3]; self._send(200, self.service.store.claim_task(task_id, body["member_id"], body.get("lease_seconds", 300), body.get("token"))); return
            if path.startswith("/api/tasks/") and path.endswith("/release"):
                task_id = path.split("/")[3]; self.service.store.release_lease(task_id, body["lease_id"]); self._send(200, {"ok": True}); return
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
    service = ButianService(home); ApiHandler.service = service
    ApiHandler.experiments = ExperimentStore(Path(home or service.home) / "experiments.db")
    ApiHandler.web_root = Path(__file__).resolve().parents[2] / "web"
    try: ThreadingHTTPServer((host, port), ApiHandler).serve_forever()
    finally:
        service.close()
        if ApiHandler.experiments:
            ApiHandler.experiments.close()
