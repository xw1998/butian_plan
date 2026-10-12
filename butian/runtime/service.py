from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from .memory import MemoryStore
from .store import Store
from .hypothesis import HypothesisStore
from .learning import LearningStore
from .provider_runtime import ProviderRuntime
from ..adapters.config import load_registry_from_env
from .dream import DreamRunner
from .tools import ToolStore
from .backup import create_backup, verify_backup, restore_backup

class ButianService:
    def __init__(self, home: str | Path | None = None):
        self.home = Path(home or os.environ.get("BUTIAN_HOME", Path.home() / ".butian"))
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "team.db")
        # 启动先收口崩溃前的 prepared 操作，再回收过期租约；结果保留供状态页和审计使用。
        self.startup_recovery = {
            "recovered_operations": self.store.recover(),
            "reclaimed_task_ids": self.store.reclaim_expired_leases(),
        }
        self.hypotheses = HypothesisStore(self.store.db)
        self.learning = LearningStore(self.store.db)
        self.tools = ToolStore(self.store, self.home)
        self.bootstrap_personas()
        # Provider 从 BUTIAN_PROVIDERS_FILE 或 <home>/providers.json 加载；无效条目记录错误、不静默丢弃
        self.providers, self.provider_config_errors, self.provider_config_path = load_registry_from_env(self.home)
        self.provider_runtime = ProviderRuntime(self.store, self.providers)
        self._maintenance_stop = threading.Event()
        self._maintenance_interval = max(1.0, float(os.environ.get("BUTIAN_MAINTENANCE_INTERVAL", "3600")))
        self._maintenance_enabled = os.environ.get("BUTIAN_MAINTENANCE_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
        self._maintenance_thread = None
        if self._maintenance_enabled:
            self._maintenance_thread = threading.Thread(target=self._maintenance_loop, name="butian-maintenance", daemon=True)
            self._maintenance_thread.start()

    def create_backup(self, destination: str | Path):
        return create_backup(self.home, destination)

    def verify_backup(self, backup: str | Path):
        return verify_backup(backup)

    def restore_backup(self, backup: str | Path, destination: str | Path):
        return restore_backup(backup, destination)

    def close(self):
        self._maintenance_stop.set()
        if self._maintenance_thread is not None and self._maintenance_thread is not threading.current_thread():
            self._maintenance_thread.join(timeout=min(5.0, self._maintenance_interval + 0.1))
        self.store.close()

    def _maintenance_loop(self):
        while not self._maintenance_stop.wait(self._maintenance_interval):
            try:
                self.maintenance_tick()
            except Exception as exc:
                try:
                    self.store.append_event(None, "maintenance_failed", {"error": type(exc).__name__ + ":" + str(exc)}, "maintenance_failed_" + uuid.uuid4().hex)
                except Exception:
                    pass

    def maintenance_tick(self):
        """Run bounded, evidence-preserving maintenance for every member."""
        results = []
        rows = self.store.db.execute("SELECT member_id FROM members ORDER BY member_id").fetchall()
        for row in rows:
            member_id = row["member_id"]
            try:
                memory_result = self._maintain_member_memories(member_id)
                dream_result = self.dream_run(member_id, min_fragments=3, min_interval=self._maintenance_interval, budget=10, lock_timeout=max(7200.0, self._maintenance_interval * 2))
                result = {"member_id": member_id, **dream_result, "memories": memory_result, "dream": dream_result}
                results.append(result)
                self.store.append_event(member_id, "maintenance_tick", {"result": result}, "maintenance_" + uuid.uuid4().hex)
            except Exception as exc:
                result = {"member_id": member_id, "status": "failed", "reason": type(exc).__name__ + ":" + str(exc)}
                results.append(result)
                self.store.append_event(member_id, "maintenance_failed", result, "maintenance_" + uuid.uuid4().hex)
        owners = [row[0] for row in self.store.db.execute("SELECT DISTINCT triage_owner_member_id FROM tool_issues WHERE triage_owner_member_id IS NOT NULL").fetchall()]
        escalation_result = []
        for owner in owners:
            try:
                escalation_result.extend(self.expire_tool_issue_escalations(owner))
            except Exception as exc:
                escalation_result.append({"status": "failed", "owner_member_id": owner, "reason": type(exc).__name__ + ":" + str(exc)})
        try:
            external_learning = self.learning.process_learning_queue(limit=1)
            learning_error = None
        except Exception as exc:
            external_learning = None
            learning_error = type(exc).__name__ + ":" + str(exc)
        return {"status": "completed", "members": results, "issue_escalations": escalation_result, "external_learning": "authorized_url_queue", "external_learning_result": external_learning, "external_learning_error": learning_error}

    def _maintain_member_memories(self, member_id: str) -> dict[str, Any]:
        """Assess active memories and schedule review for cold ones without inventing evidence."""
        memory = self.memory(member_id)
        try:
            assessed = cooled = review_due = 0
            for row in memory.db.execute("SELECT memory_id, status FROM memories WHERE status NOT IN ('invalidated','archived') ORDER BY updated_at LIMIT 100"):
                if row["status"] == "cold":
                    now = time.time()
                    key = f"memory-review:{member_id}:{row['memory_id']}:{int(now // max(1.0, self._maintenance_interval))}"
                    event_id = self.store.append_event(member_id, "memory_review_due", {"memory_id": row["memory_id"], "requires_evidence": True}, key)
                    memory.db.execute("INSERT INTO memory_maintenance VALUES (?,?,?,?,?)", (f"maint_{uuid.uuid4().hex}", row["memory_id"], "review_due", json.dumps({"event_id": event_id, "requires_evidence": True}), now))
                    review_due += 1
                    continue
                assessment = memory.assess_memory_degradation(row["memory_id"], half_life_days=30.0)
                assessed += 1
                if assessment.state in {"degraded", "disputed"}:
                    if memory.cool(row["memory_id"], reason=assessment.state):
                        cooled += 1
            return {"assessed": assessed, "cooled": cooled, "review_due": review_due, "rewarmed": 0, "rewarm_policy": "evidence_required"}
        finally:
            memory.close()

    def start_provider_task(self, member_id: str, prompt: str, *, task_id: str | None = None, provider_id: str | None = None, model: str | None = None, environment: dict | None = None):
        """Start execution using the member binding unless explicitly overridden.

        每次执行自动组装 L0–L4 上下文（当前指令、任务状态包、近期事件、独立记忆召回、身份与职责），
        并把组装报告写入事件流供审计与反馈回写。
        """
        member = self.store.db.execute("SELECT provider_id, model_id FROM members WHERE member_id=?", (member_id,)).fetchone()
        if not member:
            raise ValueError("member_not_found")
        selected_provider = self.providers.get(provider_id or member["provider_id"])
        context_window = selected_provider.capabilities().context_window
        if context_window is None:
            raise ValueError("provider_context_window_unknown")
        composed, report = self.compose_context(member_id, prompt, task_id=task_id, environment=environment, budgets={"WINDOW": context_window}, include_tools=True)
        operation_key = f"ctx_{report.decision_id}"
        if task_id:
            self.store.append_event(task_id, "provider_context_composed", report.as_dict(), operation_key)
        result = self.provider_runtime.start(member_id, composed, task_id=task_id, provider_id=provider_id or member["provider_id"], model=model or member["model_id"])
        if isinstance(result, dict):
            result["context_report"] = report.as_dict()
        return result

    def compose_context(self, member_id: str, instruction: str, *, task_id: str | None = None, environment=None, budgets: dict | None = None, include_tools: bool = False) -> tuple[str, Any]:
        if task_id and not self.store.task_access(task_id, member_id):
            raise ValueError("member_task_access_denied")
        from .context import ContextComposer
        composer = ContextComposer(self.store, self.tools)
        try:
            memory = self.memory(member_id)
        except ValueError:
            memory = None
        try:
            return composer.compose(member_id, instruction, task_id=task_id, memory=memory, environment=environment, budgets=budgets, include_tools=include_tools)
        finally:
            if memory is not None:
                memory.close()

    def _provider_share_id(self, member_id: str, tool_id: str, version: int) -> str | None:
        rows = self.store.db.execute("SELECT share_id FROM tool_shares WHERE tool_id=? AND version=? AND recipient_member_id=? AND status IN ('active','trial') AND permission='team_execute' AND (expires_at IS NULL OR expires_at>?)", (tool_id, version, member_id, time.time())).fetchall()
        return rows[0]["share_id"] if len(rows) == 1 else None

    def authorize_provider_session(self, provider_session_id: str, member_id: str) -> None:
        if not self.store.provider_session_access(provider_session_id, member_id):
            raise ValueError("provider_session_access_denied")

    def authorize_task(self, task_id: str, member_id: str) -> None:
        if not self.store.task_access(task_id, member_id):
            raise ValueError("task_access_denied")

    def consume_provider_task(self, provider_session_id: str, prompt: str | None = None, *, actor_member_id: str | None = None):
        if actor_member_id is not None:
            self.authorize_provider_session(provider_session_id, actor_member_id)
        """Consume a Provider session and move its running task to review when complete."""
        session = self.store.provider_session(provider_session_id)
        provider = self.providers.get(session["provider_id"])
        if hasattr(provider, "set_session_tools"):
            provider.set_session_tools(provider_session_id, self.tools.context_cards(session["member_id"], prompt or session.get("prompt") or "", limit=32))
        result = self.provider_runtime.consume(
            provider_session_id,
            provider,
            prompt,
            tool_executor=lambda member_id, tool_id, version, input_data, share_id=None: self.execute_tool(
                member_id, tool_id, version, input_data, task_id=session.get("task_id"), share_id=(share_id or self._provider_share_id(member_id, tool_id, version))
            ),
        )
        if result.get("status") == "completed" and result.get("text", "").strip() and session.get("task_id"):
            task = self.store.db.execute("SELECT * FROM tasks WHERE task_id=?", (session["task_id"],)).fetchone()
            if task and task["status"] == "running" and not task["parent_id"]:
                artifact_dir = self.home / "artifacts" / "solo" / task["task_id"]
                artifact_dir.mkdir(parents=True, exist_ok=True)
                artifact_path = artifact_dir / "result.txt"
                artifact_path.write_text(result["text"], encoding="utf-8")
                evidence_id = self.store.record_evidence(artifact_path, artifact_dir, owner_member_id=task["assignee_id"], actor_member_id=task["assignee_id"], internal=True)
                self.store.transition_task(task["task_id"], task["revision"], "review", f"solo:{task['task_id']}:review:{task['revision']}", payload={"provider_status": "completed", "provider_output": result["text"], "provider_session_id": provider_session_id, "artifact_path": str(artifact_path), "evidence_id": evidence_id})
                revision = task["revision"] + 1
                self.submit_handoff({"task_id": task["task_id"], "task_revision": revision, "member_id": task["assignee_id"], "status": "done", "summary": result["text"][:2000], "evidence_ids": [evidence_id], "next_action": "审核任务结果", "confidence": 0.7})
                result.update({"task_status": "review", "evidence_id": evidence_id})
            elif task:
                result["task_status"] = task["status"]
        return result

    def cancel_provider_task(self, provider_session_id: str) -> dict[str, Any]:
        session = self.store.provider_session(provider_session_id)
        terminal = {"completed", "cancelled", "provider_lost", "unknown"}
        if session["status"] in terminal:
            return {"provider_session_id": provider_session_id, "status": session["status"], "reason": session.get("reason")}
        provider = self.providers.get(session["provider_id"])
        result = provider.cancel(session.get("remote_session_id") or provider_session_id)
        if result.status in {"cancelled", "provider_lost", "unknown", "cancelling"}:
            self.store.finish_provider_session(provider_session_id, result.status, result.reason)
        return {"provider_session_id": provider_session_id, "status": result.status, "reason": result.reason}

    def cancel_task(self, task_id: str, revision: int, operation_key: str | None = None) -> dict[str, Any]:
        key = operation_key or f"cancel:{task_id}:{revision}"
        self.store.cancel_task(task_id, revision, key)
        task_state = self.store.db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        row = self.store.db.execute("SELECT provider_session_id FROM provider_sessions WHERE task_id=? AND status IN ('running','cancelling') ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone() if task_state and task_state["status"] == "cancelling" else None
        provider_result = self.cancel_provider_task(row["provider_session_id"]) if row else None
        current = self.store.db.execute("SELECT status,revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if current and current["status"] == "cancelling" and not row:
            self.store.transition_task(task_id, current["revision"], "cancelled", f"cancel-no-provider:{task_id}:{current['revision']}")
            current = self.store.db.execute("SELECT status,revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if current and current["status"] == "cancelling" and provider_result and provider_result["status"] in {"cancelled", "provider_lost", "unknown"}:
            target = "cancelled" if provider_result["status"] == "cancelled" else provider_result["status"]
            self.store.transition_task(task_id, current["revision"], target, f"cancel-final:{task_id}:{current['revision']}")
            current = self.store.db.execute("SELECT status,revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return {"task_id": task_id, "status": current["status"], "revision": current["revision"], "provider": provider_result}

    def record_evidence(self, path, *, owner_member_id=None, expires_at=None, actor_member_id=None, token=None, root=None):
        return self.store.record_evidence(path, root, owner_member_id=owner_member_id, expires_at=expires_at, actor_member_id=actor_member_id, token=token)

    def verify_evidence(self, evidence_id: str, *, actor_member_id=None):
        return self.store.verify_evidence(evidence_id, actor_member_id=actor_member_id)

    def grant_evidence_access(self, evidence_id: str, member_id: str, *, actor_member_id: str, token=None):
        return self.store.grant_evidence_access(evidence_id, member_id, actor_member_id=actor_member_id, token=token)

    def revoke_evidence_access(self, evidence_id: str, member_id: str, *, actor_member_id: str, token=None):
        return self.store.revoke_evidence_access(evidence_id, member_id, actor_member_id=actor_member_id, token=token)

    def authorize_memory_action(self, member_id: str, action: str, *, token, authorized: bool, reason: str):
        return self.store.authorize_memory_action(member_id, action, token=token, authorized=authorized, reason=reason)

    def record_prior_knowledge(self, source_kind: str, title: str, claim: str, **kwargs):
        return self.hypotheses.add_prior(source_kind, title, claim, **kwargs)

    def assess_hypothesis(self, source_kinds: list[str], **kwargs):
        return self.hypotheses.assess(source_kinds, **kwargs)

    def propose_hypothesis(self, kind: str, statement: str, **kwargs):
        return self.hypotheses.propose(kind, statement, **kwargs)

    def promote_claim_to_prior(self, claim_id: str, **kwargs):
        payload = self.learning.claim_as_prior(claim_id, **kwargs)
        prior_id = self.hypotheses.add_prior(**{key: payload[key] for key in ("source_kind", "title", "claim", "environment", "source_refs", "reliability")})
        return {"prior_id": prior_id, "claim_id": claim_id, "status": "candidate"}

    def authorize_hypothesis(self, hypothesis_id: str, description: str, *, cost: float, risk: float, information_gain: float):
        return self.hypotheses.authorize(hypothesis_id, description, cost=cost, risk=risk, information_gain=information_gain)

    def grant_hypothesis_authorization(self, action_id: str, *, actor_member_id: str, reason: str, authorized: bool):
        return self.hypotheses.grant_authorization(action_id, actor_member_id=actor_member_id, reason=reason, authorized=authorized)

    def start_hypothesis_action(self, action_id: str):
        return self.hypotheses.start(action_id)

    def get_hypothesis(self, hypothesis_id: str):
        return self.hypotheses.get(hypothesis_id)

    def observe_hypothesis(self, action_id: str, outcome: str, evidence_ids: list[str], *, independent_key: str, notes: str = "", environment=None, actor_member_id: str | None = None, member_id: str | None = None):
        if outcome in {"supported", "rejected"}:
            if not evidence_ids:
                raise ValueError("evidence_required_for_conclusive_outcome")
            for evidence_id in evidence_ids:
                verification = self.store.verify_evidence(evidence_id, actor_member_id=actor_member_id)
                if verification["status"] != "valid":
                    raise ValueError("hypothesis_evidence_not_valid")
        if member_id and not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("member_not_found")
        observation_id = f"obs_{uuid.uuid4().hex}"
        outbox = None
        if outcome == "supported" and member_id:
            action = self.store.db.execute("SELECT hypothesis_id FROM hypothesis_actions WHERE action_id=?", (action_id,)).fetchone()
            if not action:
                raise ValueError("verification_action_not_found")
            hypothesis = self.hypotheses.get(action["hypothesis_id"])
            outbox = {"member_id": member_id, "memory_id": f"mem_hyp_{observation_id}", "kind": hypothesis["kind"], "content": hypothesis["statement"]}
        observation_id = self.hypotheses.observe(action_id, outcome, evidence_ids, independent_key=independent_key, notes=notes, environment=environment, outbox=outbox, observation_id=observation_id)
        result = {"observation_id": observation_id, "outcome": outcome}
        if outbox is not None:
            memory_id = outbox["memory_id"]
            result.update({"candidate_memory_id": memory_id, "memory_status": "pending"})
            memory = self.memory(member_id)
            try:
                if memory.get(memory_id) is None:
                    memory.put(memory_id, outbox["kind"], outbox["content"], confidence=0.2, weight=0.2, environment=environment or {}, source_event_ids=[observation_id], status="candidate", knowledge_source="verified_hypothesis")
            finally:
                memory.close()
            result["memory_status"] = "candidate"
        return result

    def drain_member_memory_outbox(self, *, fail_after_write: bool = False) -> list[dict[str, Any]]:
        results = []
        for item in self.store.claim_member_memory_outbox():
            memory = None
            try:
                memory = self.memory(item["member_id"])
                existing = memory.get(item["memory_id"])
                if existing is None:
                    version_id = memory.put(item["memory_id"], item["kind"], item["content"], confidence=0.2, weight=0.2, environment=json.loads(item["environment"]), source_event_ids=json.loads(item["source_event_ids"]), status="candidate", knowledge_source="verified_hypothesis")
                else:
                    version_id = memory.list_versions(item["memory_id"])[-1]["version_id"]
                if fail_after_write:
                    raise RuntimeError("simulated_crash_after_member_write")
                self.store.mark_member_memory_outbox(item["outbox_id"], "committed")
                results.append({"outbox_id": item["outbox_id"], "status": "committed", "version_id": version_id})
            except Exception as exc:
                if fail_after_write:
                    self.store.db.execute("UPDATE member_memory_outbox SET last_error=?,updated_at=? WHERE outbox_id=?", (type(exc).__name__ + ":" + str(exc), time.time(), item["outbox_id"]))
                else:
                    self.store.mark_member_memory_outbox(item["outbox_id"], "processing", error=type(exc).__name__ + ":" + str(exc))
                results.append({"outbox_id": item["outbox_id"], "status": "processing", "error": str(exc)})
            finally:
                if memory:
                    memory.close()
        return results

    def add_member(self, member_id: str, name: str, role="member", responsibilities=None, provider_id=None, model_id=None, default_strategy_id="experience_reuse", strategy_version=1, strategy_stages=None, *, persona=None):
        self.store.add_member(member_id, name, role, responsibilities, provider_id, model_id, default_strategy_id, strategy_version, strategy_stages, persona=persona)
        MemoryStore(self.home / "members" / member_id / "memory.db").close()

    def bootstrap_personas(self) -> list[str]:
        """Create the six built-in Butian identities with fresh empty memories."""
        from .personas import seed_default_personas
        return seed_default_personas(self)

    def preflight_members(self, source_path: str, *, roles_dir: str | Path | None = None) -> dict:
        raise ValueError("historical_identity_migration_out_of_scope")

    def import_members(self, source_path: str, *, roles_dir: str | Path | None = None, update_existing: bool = False, import_memories: bool = False) -> dict:
        raise ValueError("historical_identity_migration_out_of_scope")

    def list_migration_runs(self, *, limit: int = 50) -> list[dict]:
        return []

    def require_member_access(self, member_id: str, *, actor_member_id: str | None, token: str | None = None) -> None:
        """Enforce an explicit actor boundary for external member-scoped calls."""
        if not actor_member_id:
            raise ValueError("actor_member_id_required")
        if actor_member_id != member_id:
            raise ValueError("member_memory_access_denied")
        if not self.store.authenticate_member(actor_member_id, token):
            raise ValueError("member_authentication_failed")

    def memory(self, member_id: str, *, actor_member_id: str | None = None) -> MemoryStore:
        if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("member_not_found")
        if actor_member_id is not None and actor_member_id != member_id:
            raise ValueError("member_memory_access_denied")
        return MemoryStore(self.home / "members" / member_id / "memory.db")

    def recall_for_decision(self, member_id: str, query: str, *, decision_id: str, environment=None, limit=20):
        if not decision_id.strip() or not query.strip():
            raise ValueError("decision_id_and_query_required")
        memory = self.memory(member_id)
        try:
            items = memory.recall_for_decision(query, limit=limit, environment=environment, decision_id=decision_id)
            recall_id = f"recall_{uuid.uuid4().hex}"
            memory.record_decision_recall(recall_id, decision_id, query, "recalled" if items else "no_match", False)
            for item in items:
                reference = json.loads(item["environment"] or "{}")
                current = environment or {}
                common = reference.keys() & current.keys()
                item["environment_match"] = None if not common else all(reference[key] == current[key] for key in common)
                item["needs_revalidation"] = item["status"] in {"cold", "candidate", "disputed"} or item["environment_match"] is False
                item["recall_confidence"] = "low" if item["needs_revalidation"] else "unassessed"
            return {"recall_id": recall_id, "decision_id": decision_id, "query": query, "memories": items, "no_match": not bool(items)}
        finally:
            memory.close()

    def record_practice_outcome(self, member_id: str, *, memory_id: str, kind: str, content: str, outcome: str, independent_key: str, evidence_ref: str | None = None, environment=None, source_event_ids=None):
        memory = self.memory(member_id)
        try:
            result = memory.record_practice_outcome(memory_id=memory_id, kind=kind, content=content, outcome=outcome, independent_key=independent_key, evidence_ref=evidence_ref, environment=environment, source_event_ids=source_event_ids)
            self.store.append_event(member_id, "practice_observed", {**result, "outcome": outcome, "independent_key": independent_key}, f"practice_{member_id}_{memory_id}_{independent_key}")
            return result
        finally:
            memory.close()

    def record_recall_outcome(self, member_id: str, recall_id: str, *, used_memory_ids: list[str], missed: bool, useful_memory_ids: list[str] | None = None, harmful_memory_ids: list[str] | None = None, evidence_ref: str | None = None):
        memory = self.memory(member_id)
        try:
            row = memory.db.execute("SELECT * FROM memory_recall_decisions WHERE recall_id=?", (recall_id,)).fetchone()
            if not row:
                raise ValueError("recall_decision_not_found")
            used = set(used_memory_ids)
            useful = set(useful_memory_ids or [])
            harmful = set(harmful_memory_ids or [])
            if useful & harmful:
                raise ValueError("memory_feedback_outcomes_conflict")
            if not useful.issubset(used) or not harmful.issubset(used):
                raise ValueError("feedback_memory_must_be_marked_used")
            recalled = {item[0] for item in memory.db.execute("SELECT DISTINCT memory_id FROM memory_recall_log WHERE decision_id=? AND query=?", (row["decision_id"], row["query"]))}
            if not (used | useful | harmful).issubset(recalled):
                raise ValueError("feedback_memory_not_in_recall")
            if (useful or harmful) and not evidence_ref:
                raise ValueError("usage_feedback_evidence_required")
            with memory.db:
                memory.db.execute("UPDATE memory_recall_log SET used=0 WHERE decision_id=? AND query=?", (row["decision_id"], row["query"]))
                for memory_id in used:
                    memory.db.execute("UPDATE memory_recall_log SET used=1 WHERE decision_id=? AND memory_id=? AND query=?", (row["decision_id"], memory_id, row["query"]))
                for memory_id in useful:
                    memory.record_usage_feedback(memory_id, outcome="useful", evidence_ref=evidence_ref)
                for memory_id in harmful:
                    memory.record_usage_feedback(memory_id, outcome="harmful", evidence_ref=evidence_ref)
                memory.db.execute("UPDATE memory_recall_decisions SET outcome=?, missed=? WHERE recall_id=?", ("missed" if missed else "complete", int(missed), recall_id))
            return True
        finally:
            memory.close()

    def recall_audit(self, member_id: str):
        memory = self.memory(member_id)
        try:
            return memory.recall_audit()
        finally:
            memory.close()

    def set_member_strategy(self, member_id: str, strategy_id: str, version=1, stages=None):
        return self.store.set_member_strategy(member_id, strategy_id, version, stages)

    def strategy_for_member(self, member_id: str):
        return self.store.strategy_for_member(member_id)

    def assign_strategy(self, task_id: str, strategy_id: str, stages=None, reason=None, decision_by=None):
        return self.store.assign_strategy(task_id, strategy_id, stages=stages, reason=reason, decision_by=decision_by)

    def recommend_strategies(self, task_features: dict[str, Any]):
        from .strategy_dispatch import recommend
        return [item.__dict__ for item in recommend(task_features)]

    def compose_strategies(self, member_ids: list[str], task_features: dict[str, Any]):
        from .strategy_dispatch import compose
        members = []
        for member_id in member_ids:
            strategy = self.store.strategy_for_member(member_id)
            members.append({"member_id": member_id, **strategy})
        return compose(members, task_features)

    def _resolve_dispatcher(self, mode: str, dispatcher_member_id: str | None, current_member_id: str | None) -> str | None:
        """派活者默认规则：显式指定 > 后台配置 > 当前对话数字人；解析失败宁可报错不猜。"""
        if mode == "team":
            candidates = [dispatcher_member_id, self.store.get_setting("dispatcher_member_id"), current_member_id]
            for member_id in candidates:
                if not member_id:
                    continue
                if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
                    raise ValueError(f"dispatcher_member_not_found:{member_id}")
                return member_id
            raise ValueError("dispatcher_required")
        return dispatcher_member_id or current_member_id or None

    def set_dispatcher_config(self, member_id: str) -> None:
        if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("dispatcher_member_not_found")
        self.store.set_setting("dispatcher_member_id", member_id)

    def get_dispatcher_config(self) -> str | None:
        return self.store.get_setting("dispatcher_member_id")

    def create_session(self, mode: str, dispatcher_member_id: str | None = None, requested_model: str | None = None, capabilities=None, current_member_id: str | None = None):
        if mode not in {"solo", "team"}:
            raise ValueError("invalid_session_mode")
        caps = capabilities or {}
        resolved = self._resolve_dispatcher(mode, dispatcher_member_id, current_member_id)
        if resolved and not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (resolved,)).fetchone():
            raise ValueError(f"dispatcher_member_not_found:{resolved}")
        session_id = self.store.create_session(mode, resolved, requested_model, caps)
        return {"session_id": session_id, "mode": mode, "dispatcher_member_id": resolved, "requested_model": requested_model, "effective_model": None, "selection_status": "unknown", "capabilities": caps}

    def _preflight_provider(self, member_id: str, requested_model: str | None = None) -> tuple[str, str | None]:
        member = self.store.db.execute("SELECT provider_id,model_id FROM members WHERE member_id=?", (member_id,)).fetchone()
        if not member:
            raise ValueError(f"member_not_found:{member_id}")
        provider_id = member["provider_id"]
        provider = self.providers.get(provider_id)
        selection = provider.select_model(requested_model or member["model_id"])
        if selection.selection_status in {"unsupported", "unknown"}:
            raise ValueError(f"provider_unavailable:{member_id}:{provider_id or 'unbound'}:{selection.fallback_reason or selection.selection_status}")
        return provider_id, selection.effective_model

    def submit_goal(self, goal: str, *, mode: str = "solo", member_id: str | None = None, dispatcher_member_id: str | None = None, requested_model: str | None = None, capabilities=None, environment=None, member_ids: list[str] | None = None, subtasks: list[str] | None = None) -> dict[str, Any]:
        """把自然语言目标接入执行主链；所有输入及Provider绑定先验验证后再创建任务。"""
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal_required")
        if mode not in {"solo", "team"}:
            raise ValueError("invalid_session_mode")
        if member_id and not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
            raise ValueError("member_not_found")
        resolved_dispatcher = self._resolve_dispatcher(mode, dispatcher_member_id, member_id)
        assignee = member_id or resolved_dispatcher
        if not assignee:
            raise ValueError("member_required")
        workers: list[str] = []
        plans: list[str] = []
        if mode == "team":
            workers = self._team_workers(assignee, member_ids)
            plans = self._team_subtask_plan(goal.strip(), subtasks, len(workers))
            for worker in workers:
                self._preflight_provider(worker)
        else:
            self._preflight_provider(assignee, requested_model)
        session = self.create_session(mode, dispatcher_member_id=resolved_dispatcher, requested_model=requested_model, capabilities=capabilities, current_member_id=member_id)
        payload = {"goal": goal.strip(), "mode": mode, "environment": environment or {}}
        root_id = self.store.create_task(session["session_id"], goal.strip(), assignee_id=assignee, payload=payload)
        if mode == "solo":
            self.store.transition_task(root_id, 1, "ready", f"goal:{root_id}:ready")
            lease = self.store.claim_task(root_id, assignee, lease_seconds=300, internal=True)
            self.store.transition_task(root_id, lease["revision"], "running", f"goal:{root_id}:running")
            execution = self.start_provider_task(assignee, goal.strip(), task_id=root_id, environment=environment, model=requested_model)
            return {"session": session, "task_id": root_id, "assignee_id": assignee, "execution": execution}
        children = []
        for worker, instruction in zip(workers, plans):
            child_id = self.store.create_task(session["session_id"], instruction, assignee_id=worker, parent_id=root_id, payload={"goal": goal.strip(), "instruction": instruction, "mode": "team", "environment": environment or {}})
            self.store.transition_task(child_id, 1, "ready", f"goal:{root_id}:ready:{child_id}")
            lease = self.store.claim_task(child_id, worker, lease_seconds=300, internal=True)
            self.store.transition_task(child_id, lease["revision"], "running", f"goal:{root_id}:running:{child_id}")
            execution = self.start_provider_task(worker, instruction, task_id=child_id, environment=environment)
            children.append({"task_id": child_id, "member_id": worker, "instruction": instruction, "execution": execution})
        self.store.append_event(root_id, "team_goal_dispatched", {"worker_count": len(children), "child_task_ids": [x["task_id"] for x in children]}, f"goal:{root_id}:dispatched")
        return {"session": session, "task_id": root_id, "assignee_id": assignee, "mode": "team", "children": children, "status": "dispatched"}

    def run_team_goal(self, task_id: str) -> dict[str, Any]:
        root = self.store.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not root:
            raise ValueError("task_not_found")
        session = self.store.session(root["session_id"])
        if session["mode"] != "team":
            raise ValueError("team_session_required")
        children = self.store.db.execute("SELECT * FROM tasks WHERE parent_id=? ORDER BY rowid", (task_id,)).fetchall()
        if not children:
            raise ValueError("team_subtasks_required")
        results = []
        for child in children:
            if child["status"] in {"review", "done"}:
                child_payload = json.loads(child["payload"] or "{}")
                results.append({"task_id": child["task_id"], "status": child["status"], "text": child_payload.get("provider_output", ""), "evidence_ids": [child_payload["evidence_id"]] if child_payload.get("evidence_id") else []})
                continue
            provider_row = self.store.db.execute("SELECT provider_session_id,status FROM provider_sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (child["task_id"],)).fetchone()
            if not provider_row:
                results.append({"task_id": child["task_id"], "status": "missing_provider_session"})
                continue
            if provider_row["status"] == "running":
                consumed = self.consume_provider_task(provider_row["provider_session_id"])
                status = consumed["status"]
                text = consumed.get("text", "")
            else:
                consumed = {"status": provider_row["status"], "text": ""}
                status = provider_row["status"]
                output_rows = self.store.db.execute("SELECT text FROM provider_outputs WHERE provider_session_id=? ORDER BY created_at, provider_event_id", (provider_row["provider_session_id"],)).fetchall()
                text = "".join(row["text"] for row in output_rows)
            current = self.store.db.execute("SELECT revision,status FROM tasks WHERE task_id=?", (child["task_id"],)).fetchone()
            if status == "completed" and text.strip():
                artifact_dir = self.home / "artifacts" / "team" / task_id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                artifact_path = artifact_dir / f"{child['task_id']}.txt"
                artifact_path.write_text(text, encoding="utf-8")
                evidence_id = self.record_evidence(artifact_path, root=artifact_dir, owner_member_id=child["assignee_id"], actor_member_id=child["assignee_id"])
                dispatcher = session.get("dispatcher_member_id") or root.get("assignee_id")
                if dispatcher and dispatcher != child["assignee_id"]:
                    self.store.grant_evidence_access(evidence_id, dispatcher, actor_member_id=child["assignee_id"], internal=True)
                self.store.transition_task(child["task_id"], current["revision"], "review", f"team:{task_id}:review:{child['task_id']}:{current['revision']}", payload={"provider_status": status, "provider_output": text, "provider_session_id": provider_row["provider_session_id"], "artifact_path": str(artifact_path), "evidence_id": evidence_id})
                latest_child = self.store.db.execute("SELECT lease_id FROM tasks WHERE task_id=?", (child["task_id"],)).fetchone()
                if latest_child and latest_child["lease_id"]:
                    self.store.release_lease(child["task_id"], latest_child["lease_id"])
                child_revision = current["revision"] + 1
                self.submit_handoff({"task_id": child["task_id"], "task_revision": child_revision, "member_id": child["assignee_id"], "status": "done", "summary": text[:2000], "evidence_ids": [evidence_id], "next_action": "派活者审核子任务结果", "confidence": 0.7})
                results.append({"task_id": child["task_id"], "status": "review", "text": text, "provider_session_id": provider_row["provider_session_id"], "evidence_ids": [evidence_id]})
            else:
                target = "failed" if status in {"cancelled", "provider_lost"} else "unknown"
                if current["status"] == "cancelling" and status == "completed":
                    target = "unknown"
                if current["status"] == "running":

                    self.store.transition_task(child["task_id"], current["revision"], target, f"team:{task_id}:{target}:{child['task_id']}:{current['revision']}", payload={"provider_status": status, "reason": consumed.get("reason")})
                results.append({"task_id": child["task_id"], "status": target, "reason": consumed.get("reason")})
        refreshed = self.store.db.execute("SELECT task_id,status,revision,assignee_id,payload FROM tasks WHERE parent_id=? ORDER BY rowid", (task_id,)).fetchall()
        if refreshed and all(row["status"] == "done" for row in refreshed):
            root_now = self.store.db.execute("SELECT revision,status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if root_now["status"] == "todo":
                self.store.transition_task(task_id, root_now["revision"], "ready", f"team:{task_id}:ready")
                root_now = self.store.db.execute("SELECT revision,status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if root_now["status"] == "ready":
                self.store.transition_task(task_id, root_now["revision"], "claimed", f"team:{task_id}:claimed")
                root_now = self.store.db.execute("SELECT revision,status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if root_now["status"] == "claimed":
                self.store.transition_task(task_id, root_now["revision"], "running", f"team:{task_id}:running")
                root_now = self.store.db.execute("SELECT revision,status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if root_now["status"] == "running":
                outputs = [{"task_id": row["task_id"], "member_id": row["assignee_id"], "output": json.loads(row["payload"]).get("provider_output", ""), "evidence_id": json.loads(row["payload"]).get("evidence_id")} for row in refreshed]
                aggregate = {"goal": root["title"], "results": outputs}
                artifact_dir = self.home / "artifacts" / "team" / task_id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                aggregate_path = artifact_dir / "aggregate.json"
                aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
                root_owner = root["assignee_id"] or session.get("dispatcher_member_id")
                aggregate_evidence = self.record_evidence(aggregate_path, root=artifact_dir, owner_member_id=root_owner, actor_member_id=root_owner)
                self.store.transition_task(task_id, root_now["revision"], "review", f"team:{task_id}:aggregate", payload={**aggregate, "artifact_path": str(aggregate_path), "evidence_id": aggregate_evidence})
                root_revision = root_now["revision"] + 1
                self.submit_handoff({"task_id": task_id, "task_revision": root_revision, "member_id": root_owner, "status": "done", "summary": f"已汇总 {len(outputs)} 个子任务结果", "evidence_ids": [aggregate_evidence] + [item["evidence_id"] for item in outputs if item.get("evidence_id")], "next_action": "最终审核并交付", "confidence": 0.7})
                results.append({"task_id": task_id, "status": "review", "aggregate": outputs, "evidence_ids": [aggregate_evidence]})
        current_root = self.store.db.execute("SELECT status,payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if current_root["status"] == "review":
            aggregate = json.loads(current_root["payload"] or "{}").get("results", [])
            return {"task_id": task_id, "status": "review", "children": results, "aggregate": aggregate}
        child_states = [row["status"] for row in self.store.db.execute("SELECT status FROM tasks WHERE parent_id=?", (task_id,)).fetchall()]
        return {"task_id": task_id, "status": "awaiting_child_reviews" if any(state == "review" for state in child_states) else current_root["status"], "children": results}

    def _team_workers(self, dispatcher: str, member_ids: list[str] | None) -> list[str]:
        if member_ids is not None and (not isinstance(member_ids, list) or not member_ids or any(not isinstance(mid, str) or not mid.strip() for mid in member_ids)):
            raise ValueError("invalid_member_ids")
        requested = member_ids or []
        if requested:
            workers = list(dict.fromkeys(mid.strip() for mid in requested))
            missing = [mid for mid in workers if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (mid,)).fetchone()]
            if missing:
                raise ValueError(f"member_not_found:{missing[0]}")
            return workers
        rows = self.store.db.execute("SELECT member_id FROM members ORDER BY member_id").fetchall()
        workers = [row["member_id"] for row in rows if row["member_id"] != dispatcher]
        return workers or [dispatcher]

    @staticmethod
    def _team_subtask_plan(goal: str, subtasks: list[str] | None, count: int) -> list[str]:
        """Create a deterministic plan when the caller supplies only a team goal.

        Explicit plans remain authoritative. The generated plan is deliberately
        procedural rather than pretending that a provider has reasoned about the
        domain: each worker gets a bounded investigation/implementation slice and
        the parent task remains the final review and integration gate.
        """
        if count <= 0:
            raise ValueError("team_workers_required")
        if subtasks is not None:
            if not isinstance(subtasks, list) or not subtasks:
                raise ValueError("invalid_subtasks")
            if any(not isinstance(item, str) or not item.strip() for item in subtasks):
                raise ValueError("invalid_subtask")
            plans = [item.strip() for item in subtasks]
            if len(plans) != count:
                raise ValueError("subtasks_member_count_mismatch")
            return plans
        return [
            f"围绕目标“{goal}”完成第 {index + 1} 个独立工作切片：检查现状、提出可验证结果，记录证据、风险和未完成项；不要宣称未实际验证的成功。"
            for index in range(count)
        ]

    def switch_session_mode(self, session_id: str, mode: str, *, current_member_id: str | None = None) -> dict[str, Any]:
        """模式切换：不丢任务/记忆，只影响后续编排；团队→单人不动运行中任务。"""
        if mode not in {"solo", "team"}:
            raise ValueError("invalid_session_mode")
        session = self.store.session(session_id)
        if mode == "team" and session["mode"] != "team":
            # solo→team：当前单人数字人默认成为派活者（除非后台配置了固定派活者）
            existing = self.store.get_setting("dispatcher_member_id")
            resolved = existing or current_member_id or session.get("dispatcher_member_id")
            if not resolved:
                raise ValueError("dispatcher_required")
            if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (resolved,)).fetchone():
                raise ValueError(f"dispatcher_member_not_found:{resolved}")
            with self.store.db:
                self.store.db.execute("UPDATE sessions SET dispatcher_member_id=? WHERE session_id=?", (resolved, session_id))
        result = self.store.switch_session_mode(session_id, mode)
        running = [t for t in self.store.list_tasks() if t.get("session_id") == session_id and t["status"] not in {"done", "cancelled", "failed"}]
        result["running_tasks_preserved"] = len(running)
        result["note"] = "运行中任务保留，未取消" if (session["mode"] == "team" and mode == "solo" and running) else None
        if result["changed"]:
            self.store.append_event(session_id, "session_mode_switched", {"from": session["mode"], "to": mode, "running_tasks": len(running)}, f"mode_switch_{session_id}_{mode}_{int(time.time())}")
        return result

    def task_tree(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.task_tree(session_id)

    def review_task(self, task_id: str, revision: int, verdict: str, *, reviewer_member_id: str | None = None, actor_member_id: str | None = None, notes: str = "", rework_limit: int = 3) -> dict[str, Any]:
        """审核入口：approve→done（受 handoff 门槛保护）；rework→ready（受返工次数上限保护）。"""
        if verdict not in {"approve", "rework"}:
            raise ValueError("invalid_review_verdict")
        if reviewer_member_id and not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (reviewer_member_id,)).fetchone():
            raise ValueError("reviewer_member_not_found")
        if actor_member_id and reviewer_member_id != actor_member_id:
            raise ValueError("reviewer_identity_mismatch")
        task = next((t for t in self.store.list_tasks() if t["task_id"] == task_id), None)
        if not task:
            raise ValueError("task_not_found")
        session = self.store.session(task["session_id"])
        allowed_reviewers = {session.get("dispatcher_member_id"), task.get("assignee_id")}
        if reviewer_member_id and reviewer_member_id not in allowed_reviewers:
            raise ValueError("reviewer_not_authorized")
        handoff = self.store.db.execute("SELECT payload FROM handoffs WHERE task_id=? AND revision=?", (task_id, revision)).fetchone()
        if verdict == "approve":
            self.store.transition_task(task_id, revision, "done", f"review_approve_{task_id}_{revision}_{reviewer_member_id}", payload={**self._task_payload(task), "review_verdict": "approve", "reviewer_member_id": reviewer_member_id, "review_notes": notes})
            learning = self._record_review_learning(task, revision, "approve", notes, handoff)
            return {"task_id": task_id, "verdict": "approve", "status": "done", "learning": learning}
        rework_count = self.store.record_rework(task_id, revision, reviewer_member_id=reviewer_member_id or "", notes=notes, rework_limit=rework_limit)
        self.store.transition_task(task_id, revision, "ready", f"review_rework_{task_id}_{revision}_{rework_count}", payload={**self._task_payload(task), "review_verdict": "rework", "reviewer_member_id": reviewer_member_id, "review_notes": notes, "rework_count": rework_count})
        refreshed = next(item for item in self.store.list_tasks() if item["task_id"] == task_id)
        instruction = self._task_payload(refreshed).get("instruction") or refreshed.get("title") or "请根据审核意见重新执行任务"
        execution = {"status": "ready", "reason": "provider_unavailable"}
        if refreshed.get("assignee_id"):
            member = self.store.db.execute("SELECT provider_id FROM members WHERE member_id=?", (refreshed["assignee_id"],)).fetchone()
            if member and member["provider_id"]:
                next_revision = refreshed["revision"]
                self.store.transition_task(task_id, next_revision, "claimed", f"review_rework_claim_{task_id}_{next_revision}")
                claimed = next(item for item in self.store.list_tasks() if item["task_id"] == task_id)
                self.store.transition_task(task_id, claimed["revision"], "running", f"review_rework_run_{task_id}_{claimed['revision']}")
                execution = self.start_provider_task(refreshed["assignee_id"], instruction, task_id=task_id, environment=self._task_payload(refreshed).get("environment"))
        learning = self._record_review_learning(task, revision, "rework", notes, handoff)
        return {"task_id": task_id, "verdict": "rework", "status": execution["status"], "rework_count": rework_count, "rework_limit": rework_limit, "execution": execution, "learning": learning}

    def _record_review_learning(self, task: dict[str, Any], revision: int, verdict: str, notes: str, handoff_row) -> dict[str, Any]:
        """Turn an audited review into a deduplicated candidate; never promotes it."""
        if not handoff_row:
            return {"status": "skipped", "reason": "handoff_not_found"}
        handoff = json.loads(handoff_row["payload"])
        member_id = handoff.get("member_id")
        evidence_ids = handoff.get("evidence_ids") or []
        if not member_id or not evidence_ids:
            return {"status": "skipped", "reason": "handoff_evidence_not_sufficient"}
        for evidence_id in evidence_ids:
            if not self.store.db.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
                return {"status": "skipped", "reason": "handoff_evidence_not_found"}
        operation_key = f"review-learning:{task['task_id']}:{revision}:{verdict}"
        event_id = self.store.append_event(member_id, "review_learning_observed", {"task_id": task["task_id"], "task_revision": revision, "verdict": verdict, "evidence_ids": evidence_ids, "notes": notes}, operation_key)
        memory_id = f"mem_review_{task['task_id']}_{revision}_{verdict}"
        content = json.dumps({"task_id": task["task_id"], "task_revision": revision, "summary": handoff.get("summary", ""), "review_verdict": verdict, "review_notes": notes, "evidence_ids": evidence_ids, "verification_required": True}, ensure_ascii=False)
        outbox_id = self.store.enqueue_member_memory(operation_key, member_id, memory_id, "task_review_candidate", content, {"source": "review", "verdict": verdict, "evidence_ids": evidence_ids}, [event_id])
        self.drain_member_memory_outbox()
        row = next(item for item in self.store.list_member_memory_outbox() if item["outbox_id"] == outbox_id)
        return {"status": row["status"], "memory_id": memory_id, "candidate": True, "event_id": event_id}

    @staticmethod
    def _task_payload(task: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(task.get("payload") or "{}")
        except (TypeError, ValueError):
            return {}

    def select_model(self, session_id: str, requested_model: str | None, capabilities: dict[str, Any], available_models: list[str] | None = None):
        status, effective, reason = "unknown", None, "provider did not report model selection"
        if requested_model and capabilities.get("can_select_model") and (not available_models or requested_model in available_models):
            status, effective, reason = "applied", requested_model, None
        elif requested_model and capabilities.get("can_select_model"):
            status, effective, reason = "fallback", (available_models or [None])[0], "requested model unavailable"
        elif requested_model:
            status, effective, reason = "unsupported", None, "provider cannot select a per-member model"
        self.store.db.execute("UPDATE sessions SET requested_model=?, effective_model=?, selection_status=?, capabilities=? WHERE session_id=?", (requested_model, effective, status, json.dumps({**capabilities, "model_selection_reason": reason}), session_id))
        return {"requested_model": requested_model, "effective_model": effective, "selection_status": status, "fallback_reason": reason}

    def ask_user(self, task_id: str, revision: int, member_id: str, question: str):
        qid = f"q_{uuid.uuid4().hex}"
        self.store.db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?)", (qid, task_id, revision, member_id, question, None, "open", time.time()))
        self.store.transition_task(task_id, revision, "waiting_user", f"question:{qid}", {"question_id": qid})
        return qid

    def answer_user(self, question_id: str, answer: str):
        row = self.store.db.execute("SELECT * FROM questions WHERE question_id=?", (question_id,)).fetchone()
        if not row or row["status"] != "open":
            raise ValueError("question_not_open")
        task = self.store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        if not task or task["revision"] != row["revision"] + 1:
            raise ValueError("question_revision_conflict")
        with self.store.db:
            self.store.db.execute("UPDATE questions SET answer=?, status='answered' WHERE question_id=?", (answer, question_id))
            self.store.db.execute("UPDATE tasks SET status='ready', revision=revision+1, payload=? WHERE task_id=? AND revision=?", (json.dumps({"answer": answer}), row["task_id"], task["revision"]))
        return row["task_id"]

    def report_tool_issue(self, member_id, tool_id, version, **details):
        return self.tools.report_issue(member_id, tool_id, version, **details)

    def list_tool_issues(self, member_id, tool_id):
        return self.tools.list_issues(member_id, tool_id)

    def repair_tool_issue(self, member_id, issue_id, **details):
        return self.tools.repair_issue(member_id, issue_id, **details)

    def confirm_tool_repair(self, member_id, issue_id, **details):
        return self.tools.confirm_repair(member_id, issue_id, **details)

    def review_tool_security_issue(self, member_id, issue_id, **details):
        return self.tools.review_security_issue(member_id, issue_id, **details)

    def triage_tool_issue(self, member_id, issue_id, **details):
        return self.tools.triage_issue(member_id, issue_id, **details)

    def request_tool_issue_information(self, member_id, issue_id, **details):
        return self.tools.request_issue_information(member_id, issue_id, **details)

    def provide_tool_issue_information(self, member_id, issue_id, *, information):
        return self.tools.provide_issue_information(member_id, issue_id, information=information)

    def link_duplicate_tool_issue(self, member_id, issue_id, duplicate_of):
        return self.tools.link_duplicate_issue(member_id, issue_id, duplicate_of)

    def expire_tool_issue_escalations(self, member_id, **details):
        return self.tools.expire_issue_escalations(member_id, **details)

    def list_tool_issue_escalations(self, member_id, **details):
        return self.tools.list_issue_escalations(member_id, **details)

    def claim_tool_issue_escalation(self, member_id, work_item_id):
        return self.tools.claim_issue_escalation(member_id, work_item_id)

    def resolve_tool_issue_escalation(self, member_id, work_item_id, **details):
        return self.tools.resolve_issue_escalation(member_id, work_item_id, **details)

    def record_tool_issue_pair(self, member_id, issue_id, **details):
        return self.tools.record_issue_pair(member_id, issue_id, **details)

    def tool_issue_statistics(self, member_id, tool_id, **details):
        return self.tools.issue_statistics(member_id, tool_id, **details)

    def open_tool_circuit_breaker(self, member_id, tool_id, version, **details):
        return self.tools.open_circuit_breaker(member_id, tool_id, version, **details)

    def recover_tool_circuit_breaker(self, member_id, tool_id, version, **details):
        return self.tools.recover_circuit_breaker(member_id, tool_id, version, **details)

    def propose_tool(self, owner_member_id: str, name: str, description: str, **costs):
        return self.tools.propose(owner_member_id, name, description, **costs)

    def add_tool_version(self, owner_member_id: str, tool_id: str, content: bytes | str, manifest: dict[str, Any]):
        return self.tools.add_version(owner_member_id, tool_id, content, manifest)

    def verify_tool(self, owner_member_id: str, tool_id: str, version: int, outcome: str, **details):
        return self.tools.verify(owner_member_id, tool_id, version, outcome, **details)

    def list_tools(self, owner_member_id: str, *, status: str | None = None):
        return self.tools.list(owner_member_id, status=status)

    def tool_stats(self, tool_id: str, version: int):
        return self.tools.stats(tool_id, version)

    def execute_tool(self, actor_member_id: str, tool_id: str, version: int, input_data=None, **options):
        return self.tools.execute(actor_member_id, tool_id, version, input_data, **options)

    def list_tool_executions(self, actor_member_id: str, *, tool_id: str | None = None, limit: int = 50):
        return self.tools.list_executions(actor_member_id, tool_id=tool_id, limit=limit)

    def share_tool(self, owner_member_id: str, tool_id: str, recipient_member_id: str, *, permission: str = "team_readonly"):
        return self.tools.share(owner_member_id, tool_id, recipient_member_id, permission=permission)

    def list_tool_shares(self, member_id: str, *, status: str | None = None):
        return self.tools.shares_for(member_id, status=status)

    def accept_tool_share(self, recipient_member_id: str, share_id: str):
        return self.tools.accept_share(recipient_member_id, share_id)

    def trial_tool(self, recipient_member_id: str, share_id: str, outcome: str, **details):
        return self.tools.trial(recipient_member_id, share_id, outcome, **details)

    def archive_tool(self, owner_member_id: str, tool_id: str, *, reason: str = "maintenance"):
        return self.tools.archive(owner_member_id, tool_id, reason=reason)

    def restore_tool(self, owner_member_id: str, tool_id: str):
        return self.tools.restore(owner_member_id, tool_id)

    def ask_peer(self, asker_id: str, expert_id: str, question: str, *, task_id: str | None = None, comparison: dict[str, Any] | None = None):
        if asker_id == expert_id:
            raise ValueError("peer_learning_requires_two_members")
        for member_id in (asker_id, expert_id):
            if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
                raise ValueError("member_not_found:" + member_id)
        learning_id = f"learn_{uuid.uuid4().hex}"
        event_id = self.store.append_event(learning_id, "peer_learning_asked", {"asker_id": asker_id, "expert_id": expert_id, "task_id": task_id, "question": question}, f"{learning_id}:asked")
        self.store.db.execute("INSERT INTO peer_learning VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (learning_id, asker_id, expert_id, task_id, question, None, json.dumps(comparison or {}, ensure_ascii=False), "asked", None, json.dumps([event_id]), time.time(), None))
        return learning_id

    def answer_peer(self, learning_id: str, answer: str, *, method: str | None = None, evidence_ids: list[str] | None = None):
        row = self.store.db.execute("SELECT * FROM peer_learning WHERE learning_id=?", (learning_id,)).fetchone()
        if not row or row["status"] != "asked":
            raise ValueError("peer_learning_not_open")
        candidate_id = f"mem_{uuid.uuid4().hex}"
        candidate = {"answer": answer, "method": method, "expert_id": row["expert_id"], "learning_id": learning_id, "verification_required": True}
        memory = self.memory(row["asker_id"])
        try:
            answer_event = self.store.append_event(learning_id, "peer_learning_answered", {"answer": answer, "method": method, "expert_id": row["expert_id"]}, f"{learning_id}:answered")
            memory.put(candidate_id, "peer_learning_candidate", json.dumps(candidate, ensure_ascii=False), confidence=0.2, weight=0.2, environment={"source": "peer", "expert_id": row["expert_id"]}, source_event_ids=[answer_event], status="candidate", source_validator=lambda ids: self.store.validate_source_events(ids, member_id=row["asker_id"]))
        finally:
            memory.close()
        ids = evidence_ids or []
        self.store.db.execute("UPDATE peer_learning SET answer=?, status='candidate', candidate_memory_id=?, evidence_ids=? WHERE learning_id=?", (answer, candidate_id, json.dumps(ids), learning_id))
        return {"learning_id": learning_id, "candidate_memory_id": candidate_id, "status": "candidate"}

    def verify_peer_learning(self, learning_id: str, *, success: bool, evidence_ids: list[str] | None = None, notes: str = ""):
        row = self.store.db.execute("SELECT * FROM peer_learning WHERE learning_id=?", (learning_id,)).fetchone()
        if not row or row["status"] not in ("candidate", "verified", "rejected", "inconclusive"):
            raise ValueError("peer_learning_not_candidate")
        status = "verified" if success else "rejected"
        if success:
            self.store.record_verification(row["candidate_memory_id"], f"{learning_id}:verification:{time.time_ns()}", "success", evidence_ids or [], {"peer_learning_id": learning_id})
        else:
            self.store.record_verification(row["candidate_memory_id"], f"{learning_id}:verification:{time.time_ns()}", "failure", evidence_ids or [], {"peer_learning_id": learning_id})
        memory = self.memory(row["asker_id"])
        try:
            mem = memory.db.execute("SELECT * FROM memories WHERE memory_id=?", (row["candidate_memory_id"],)).fetchone()
            if mem:
                successful = memory.db.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id=? AND event_type='verification_success'", (row["candidate_memory_id"],)).fetchone()[0]
                if success:
                    memory.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"mev_{uuid.uuid4().hex}", row["candidate_memory_id"], "verification_success", json.dumps({"learning_id": learning_id, "evidence_ids": evidence_ids or []}), time.time()))
                    successful += 1
                else:
                    memory.db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?)", (f"mev_{uuid.uuid4().hex}", row["candidate_memory_id"], "verification_failure", json.dumps({"learning_id": learning_id, "evidence_ids": evidence_ids or []}), time.time()))
                new_status = "warm" if successful >= 2 else "candidate"
                new_weight = min(5.0, float(mem["weight"]) + (0.5 if success else -0.2))
                new_confidence = min(1.0, float(mem["confidence"]) + (0.1 if success else 0.0))
                memory.db.execute("UPDATE memories SET status=?, weight=?, confidence=?, updated_at=? WHERE memory_id=?", (new_status, new_weight, new_confidence, time.time(), row["candidate_memory_id"]))
        finally:
            memory.close()
        ids = evidence_ids or json.loads(row["evidence_ids"])
        with self.store.db:
            self.store.db.execute("UPDATE peer_learning SET status=?, evidence_ids=?, verified_at=?, comparison=? WHERE learning_id=?", (status, json.dumps(ids), time.time(), json.dumps({**json.loads(row["comparison"]), "verification_notes": notes, "success": success}, ensure_ascii=False), learning_id))
        return {"learning_id": learning_id, "status": status, "candidate_memory_id": row["candidate_memory_id"], "memory_status": new_status if 'new_status' in locals() else "candidate"}

    def list_peer_learning(self, *, status: str | None = None, member_id: str | None = None):
        sql = "SELECT * FROM peer_learning WHERE 1=1"; args: list[Any] = []
        if status: sql += " AND status=?"; args.append(status)
        if member_id: sql += " AND (asker_id=? OR expert_id=?)"; args.extend([member_id, member_id])
        return [dict(row) for row in self.store.db.execute(sql + " ORDER BY created_at DESC", args)]

    def submit_handoff(self, handoff: dict[str, Any]):
        required = {"task_id", "task_revision", "member_id", "status", "summary", "evidence_ids", "next_action", "confidence"}
        missing = required - handoff.keys()
        if missing:
            raise ValueError(f"handoff_missing:{','.join(sorted(missing))}")
        task_id = handoff["task_id"]
        task = self.store.db.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("task_not_found")
        if handoff["task_revision"] != task["revision"]:
            raise ValueError("handoff_stale_revision")
        if not self.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (handoff["member_id"],)).fetchone():
            raise ValueError("member_not_found")
        for field in ("status", "summary", "next_action"):
            if not str(handoff[field] or "").strip():
                raise ValueError(f"handoff_field_empty:{field}")
        if not isinstance(handoff["evidence_ids"], list):
            raise ValueError("handoff_evidence_ids_must_be_list")
        if not isinstance(handoff["confidence"], (int, float)):
            raise ValueError("handoff_confidence_must_be_number")
        self.store.db.execute("INSERT OR REPLACE INTO handoffs VALUES (?,?,?,?,?,?)", (task_id, handoff["task_revision"], handoff["member_id"], handoff["status"], json.dumps(handoff, ensure_ascii=False), time.time()))
        return True



    # ---- 数字人进化：记忆合成 / 技能演化 / 退化与复温 / 版本链 ----
    def compose_memories(self, member_id, result_memory_id, component_memory_ids, *, content, kind="compound", environment=None, source_event_ids=None, composition_bonus=0.0, redundancy_penalty=0.0, conflict_penalty=0.0):
        memory = self.memory(member_id)
        try:
            return memory.compose(result_memory_id, component_memory_ids, content=content, kind=kind, environment=environment, source_event_ids=source_event_ids, composition_bonus=composition_bonus, redundancy_penalty=redundancy_penalty, conflict_penalty=conflict_penalty)
        finally:
            memory.close()

    def update_skill(self, member_id, domain, *, level, marginal, contradiction=False, novel_environment=False, independent_successes=0, composition_successes=0, regression_evidence=False):
        memory = self.memory(member_id)
        try:
            state = memory.update_skill(domain, level=level, marginal=marginal, contradiction=contradiction, novel_environment=novel_environment, independent_successes=independent_successes, composition_successes=composition_successes, regression_evidence=regression_evidence)
            return _skill_state_dict(state)
        finally:
            memory.close()

    def list_skills(self, member_id):
        memory = self.memory(member_id)
        try:
            return [{"domain": row["skill_domain"], "phase": row["phase"], "level": row["level"], "abstraction_level": row["abstraction_level"], "marginal_gain": row["marginal_gain"], "plateau_score": row["plateau_score"], "activation_count": row["activation_count"], "updated_at": row["updated_at"]} for row in memory.db.execute("SELECT * FROM skill_states ORDER BY skill_domain")]
        finally:
            memory.close()

    def list_skill_events(self, member_id, *, limit=50):
        memory = self.memory(member_id)
        try:
            return [{"domain": row["skill_domain"], "event_type": row["event_type"], "payload": json.loads(row["payload"]), "created_at": row["created_at"]} for row in memory.db.execute("SELECT * FROM skill_events ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            memory.close()

    def assess_memory_degradation(self, member_id, memory_id, *, now=None, half_life_days=30.0, current_environment=None, consecutive_failures=0, contradiction_count=0, dependency_drift_score=0.0, hard_failure=False):
        memory = self.memory(member_id)
        try:
            a = memory.assess_memory_degradation(memory_id, now=now, half_life_days=half_life_days, current_environment=current_environment, consecutive_failures=consecutive_failures, contradiction_count=contradiction_count, dependency_drift_score=dependency_drift_score, hard_failure=hard_failure)
            return {"score": a.score, "freshness": a.freshness, "state": a.state, "reasons": list(a.reasons), "model_version": a.model_version}
        finally:
            memory.close()

    def cool_memory(self, member_id, memory_id, *, reason="stale"):
        memory = self.memory(member_id)
        try:
            return {"cooled": memory.cool(memory_id, reason=reason)}
        finally:
            memory.close()

    def rewarm_memory(self, member_id, memory_id, *, success, evidence_ref=None):
        memory = self.memory(member_id)
        try:
            return {"status": memory.rewarm(memory_id, success=success, evidence_ref=evidence_ref)}
        finally:
            memory.close()

    def memory_versions(self, member_id, memory_id):
        memory = self.memory(member_id)
        try:
            return {"versions": memory.list_versions(memory_id), "chain": memory.audit_version_chain(memory_id)}
        finally:
            memory.close()

    def create_validation_batch(self, member_id, memory_id, *, sample_count, notes=None, batch_id=None):
        memory = self.memory(member_id)
        try:
            return {"batch_id": memory.create_validation_batch(memory_id, sample_count=sample_count, notes=notes, batch_id=batch_id)}
        finally:
            memory.close()

    def update_validation_batch(self, member_id, batch_id, state, *, pass_count=0, fail_count=0, evidence_refs=None, notes=None):
        memory = self.memory(member_id)
        try:
            return memory.update_validation_batch(batch_id, state, pass_count=pass_count, fail_count=fail_count, evidence_refs=evidence_refs, notes=notes)
        finally:
            memory.close()

    def list_validation_batches(self, member_id, memory_id):
        memory = self.memory(member_id)
        try:
            return {"batches": memory.list_validation_batches(memory_id)}
        finally:
            memory.close()

    # ---- 数字人进化：梦境自省与验证队列 ----
    def _ensure_dream_tables(self, memory):
        """梦境表由 DreamRunner 懒创建；读取前先确保存在，避免空成员查询报错。"""
        DreamRunner(memory, team_store=self.store, actor_member_id=None)

    def dream_run(self, member_id, *, min_fragments=3, min_interval=3600, budget=10, lock_timeout=7200):
        memory = self.memory(member_id)
        try:
            return DreamRunner(memory, team_store=self.store, actor_member_id=member_id).run(min_fragments=min_fragments, min_interval=min_interval, budget=budget, lock_timeout=lock_timeout)
        finally:
            memory.close()

    def dream_distill(self, member_id, memory_ids, *, summary, merged_items=None, conflicts=None, discarded_items=None, information_loss=0.0):
        memory = self.memory(member_id)
        try:
            return DreamRunner(memory, team_store=self.store, actor_member_id=member_id).distill_memories(memory_ids, summary=summary, merged_items=merged_items, conflicts=conflicts, discarded_items=discarded_items, information_loss=information_loss)
        finally:
            memory.close()

    def dream_abstract(self, member_id, memory_ids, *, pattern, supports=None, counterexamples=None, applicability=None, failure_conditions=None, abstraction_level=1):
        memory = self.memory(member_id)
        try:
            return DreamRunner(memory, team_store=self.store, actor_member_id=member_id).abstract_memories(memory_ids, pattern=pattern, supports=supports, counterexamples=counterexamples, applicability=applicability, failure_conditions=failure_conditions, abstraction_level=abstraction_level)
        finally:
            memory.close()

    def dream_propose_hypothesis(self, member_id, candidate_id, statement, *, prior_reliability, argument, evidence_refs=None):
        memory = self.memory(member_id)
        try:
            hypothesis_id = DreamRunner(memory, team_store=self.store, actor_member_id=member_id).propose_hypothesis(candidate_id, statement, prior_reliability=prior_reliability, argument=argument, evidence_refs=evidence_refs)
            return {"hypothesis_id": hypothesis_id}
        finally:
            memory.close()

    def dream_schedule_verification(self, member_id, hypothesis_id, description, *, cost, risk, expected_information_gain, authorized=False):
        memory = self.memory(member_id)
        try:
            return DreamRunner(memory, team_store=self.store, actor_member_id=member_id).schedule_verification(hypothesis_id, description, cost=cost, risk=risk, expected_information_gain=expected_information_gain, authorized=authorized)
        finally:
            memory.close()

    def dream_record_verification(self, member_id, hypothesis_id, *, outcome, evidence_refs, new_memory_id=None, new_content=None):
        memory = self.memory(member_id)
        try:
            return DreamRunner(memory, team_store=self.store, actor_member_id=member_id).record_verification(hypothesis_id, outcome=outcome, evidence_refs=evidence_refs, new_memory_id=new_memory_id, new_content=new_content)
        finally:
            memory.close()

    def dream_verification_queue(self, member_id, limit=20):
        memory = self.memory(member_id)
        self._ensure_dream_tables(memory)
        try:
            return {"queue": DreamRunner(memory, team_store=self.store, actor_member_id=member_id).verification_queue(limit=limit)}
        finally:
            memory.close()

    def list_dream_runs(self, member_id, *, limit=20):
        memory = self.memory(member_id)
        self._ensure_dream_tables(memory)
        try:
            return [{"run_id": row["run_id"], "started_at": row["started_at"], "status": row["status"], "reason": row["reason"], "candidate_count": row["candidate_count"], "gate": json.loads(row["gate_snapshot"])} for row in memory.db.execute("SELECT * FROM dream_runs ORDER BY started_at DESC LIMIT ?", (limit,))]
        finally:
            memory.close()

    def list_dream_candidates(self, member_id, *, limit=20):
        memory = self.memory(member_id)
        self._ensure_dream_tables(memory)
        try:
            return [{"candidate_id": row["candidate_id"], "run_id": row["run_id"], "memory_ids": json.loads(row["memory_ids"]), "content": row["content"], "status": row["status"], "created_at": row["created_at"]} for row in memory.db.execute("SELECT * FROM dream_candidates ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            memory.close()

    def list_dream_hypotheses(self, member_id, *, limit=20):
        memory = self.memory(member_id)
        self._ensure_dream_tables(memory)
        try:
            return [{"hypothesis_id": row["hypothesis_id"], "candidate_id": row["candidate_id"], "statement": row["statement"], "prior_reliability": row["prior_reliability"], "argument": row["argument"], "status": row["status"], "evidence_refs": json.loads(row["evidence_refs"]), "created_at": row["created_at"]} for row in memory.db.execute("SELECT * FROM hypotheses ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            memory.close()

    def list_dream_distillations(self, member_id, *, limit=20):
        memory = self.memory(member_id)
        self._ensure_dream_tables(memory)
        try:
            return [{"distillation_id": row["distillation_id"], "source_memory_ids": json.loads(row["source_memory_ids"]), "summary": row["summary"], "information_loss": row["information_loss"], "reversible": bool(row["reversible"]), "status": row["status"], "created_at": row["created_at"]} for row in memory.db.execute("SELECT * FROM memory_distillations ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            memory.close()

    def list_dream_abstractions(self, member_id, *, limit=20):
        memory = self.memory(member_id)
        self._ensure_dream_tables(memory)
        try:
            return [{"abstraction_id": row["abstraction_id"], "source_memory_ids": json.loads(row["source_memory_ids"]), "supports": json.loads(row["supports"]), "counterexamples": json.loads(row["counterexamples"]), "applicability": json.loads(row["applicability"]), "failure_conditions": json.loads(row["failure_conditions"]), "abstraction_level": row["abstraction_level"], "status": row["status"], "created_at": row["created_at"]} for row in memory.db.execute("SELECT * FROM memory_abstractions ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            memory.close()


def _skill_state_dict(state) -> dict[str, Any]:
    phase = state.phase.value if hasattr(state.phase, "value") else str(state.phase)
    return {"phase": phase, "level": state.level, "abstraction_level": state.abstraction_level, "marginal_gain": state.marginal_gain, "plateau_score": state.plateau_score, "activation_count": state.activation_count}
