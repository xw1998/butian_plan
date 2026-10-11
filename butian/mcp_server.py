from __future__ import annotations
import json
import sys
from .runtime.service import ButianService

TOOLS = [
    {"name": "butian_provider_start", "description": "Start a member-bound Provider execution", "inputSchema": {"type": "object", "required": ["member_id", "prompt"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "prompt": {"type": "string"}, "task_id": {"type": "string"}, "provider_id": {"type": "string"}, "model": {"type": "string"}}}},
    {"name": "butian_provider_consume", "description": "Consume Provider output and persist its terminal status", "inputSchema": {"type": "object", "required": ["provider_session_id", "prompt"], "properties": {"provider_session_id": {"type": "string"}, "prompt": {"type": "string"}}}},
    {"name": "butian_status", "description": "Return Butian runtime status", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "butian_member_preflight", "description": "Read-only preflight of a Nuwa-style member database; reports schema gaps and identity conflicts", "inputSchema": {"type": "object", "required": ["source_path"], "properties": {"source_path": {"type": "string"}, "roles_dir": {"type": "string"}}}},
    {"name": "butian_member_import", "description": "Import digital members (agents) from a Nuwa-style memory.db read-only", "inputSchema": {"type": "object", "required": ["source_path"], "properties": {"source_path": {"type": "string"}, "roles_dir": {"type": "string"}, "update_existing": {"type": "boolean"}, "import_memories": {"type": "boolean"}}}},
    {"name": "butian_migration_runs", "description": "List member import (migration) runs", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "butian_memory_search", "description": "Search a member's isolated memory", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "query"], "properties": {"actor_member_id": {"type": "string"}, "token": {"type": "string"}, "member_id": {"type": "string"}, "query": {"type": "string"}, "kind": {"type": "string"}}}},
    {"name": "butian_memory_recall", "description": "Recall relevant experience and lessons before a task decision", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "query", "decision_id"], "properties": {"actor_member_id": {"type": "string"}, "token": {"type": "string"}, "member_id": {"type": "string"}, "query": {"type": "string"}, "decision_id": {"type": "string"}, "environment": {"type": "object"}, "limit": {"type": "integer"}}}},
    {"name": "butian_memory_recall_outcome", "description": "Record whether recalled memories were used and whether recall missed relevant lessons", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "recall_id", "used_memory_ids", "missed"], "properties": {"actor_member_id": {"type": "string"}, "token": {"type": "string"}, "member_id": {"type": "string"}, "recall_id": {"type": "string"}, "used_memory_ids": {"type": "array", "items": {"type": "string"}}, "missed": {"type": "boolean"}, "useful_memory_ids": {"type": "array", "items": {"type": "string"}}, "harmful_memory_ids": {"type": "array", "items": {"type": "string"}}, "evidence_ref": {"type": "string"}}}},
    {"name": "butian_memory_recall_audit", "description": "Inspect recall outcomes and missed-memory audits", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id"], "properties": {"actor_member_id": {"type": "string"}, "token": {"type": "string"}, "member_id": {"type": "string"}}}},
    {"name": "butian_memory_practice", "description": "Record an evidence-backed practice outcome; repeated independent success can warm a candidate memory", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "memory_id", "kind", "content", "outcome", "independent_key"], "properties": {"actor_member_id": {"type": "string"}, "token": {"type": "string"}, "member_id": {"type": "string"}, "memory_id": {"type": "string"}, "kind": {"type": "string"}, "content": {"type": "string"}, "outcome": {"type": "string", "enum": ["success", "failure", "unknown"]}, "independent_key": {"type": "string"}, "evidence_ref": {"type": "string"}, "environment": {"type": "object"}, "source_event_ids": {"type": "array"}}}},
    {"name": "butian_task_list", "description": "List orchestration tasks", "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}},
    {"name": "butian_session_create", "description": "Create solo or team session; team dispatcher defaults to backend config then current conversing member", "inputSchema": {"type": "object", "required": ["mode"], "properties": {"mode": {"type": "string", "enum": ["solo", "team"]}, "dispatcher_member_id": {"type": "string"}, "requested_model": {"type": "string"}, "capabilities": {"type": "object"}, "current_member_id": {"type": "string"}}}},
    {"name": "butian_session_switch_mode", "description": "Switch a session between solo/team; running tasks are preserved, never silently cancelled", "inputSchema": {"type": "object", "required": ["session_id", "mode"], "properties": {"session_id": {"type": "string"}, "mode": {"type": "string", "enum": ["solo", "team"]}, "current_member_id": {"type": "string"}}}},
    {"name": "butian_session_list", "description": "List sessions with mode and dispatcher snapshot plus backend dispatcher config", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "butian_dispatcher_set", "description": "Set backend default dispatcher member (only affects new sessions)", "inputSchema": {"type": "object", "required": ["member_id"], "properties": {"member_id": {"type": "string"}}}},
    {"name": "butian_task_tree", "description": "Return the task tree (roots with nested children)", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}}},
    {"name": "butian_task_review", "description": "Review a task in review status: approve (done, gated by handoff evidence) or rework (back to ready, capped by rework limit)", "inputSchema": {"type": "object", "required": ["task_id", "revision", "verdict"], "properties": {"task_id": {"type": "string"}, "revision": {"type": "integer"}, "verdict": {"type": "string", "enum": ["approve", "rework"]}, "reviewer_member_id": {"type": "string"}, "notes": {"type": "string"}, "rework_limit": {"type": "integer"}}}},
    {"name": "butian_task_create", "description": "Create a task", "inputSchema": {"type": "object", "required": ["session_id", "title"], "properties": {"session_id": {"type": "string"}, "title": {"type": "string"}, "assignee_id": {"type": "string"}}}},
    {"name": "butian_task_transition", "description": "Transition a task with revision guard", "inputSchema": {"type": "object", "required": ["task_id", "expected_revision", "target", "operation_key"], "properties": {"task_id": {"type": "string"}, "expected_revision": {"type": "integer"}, "target": {"type": "string"}, "operation_key": {"type": "string"}}}},
    {"name": "butian_task_claim", "description": "Claim a ready task with a lease", "inputSchema": {"type": "object", "required": ["task_id", "member_id"], "properties": {"task_id": {"type": "string"}, "member_id": {"type": "string"}, "lease_seconds": {"type": "integer"}, "token": {"type": "string"}}}},
    {"name": "butian_task_release", "description": "Release a task lease and requeue claimed work", "inputSchema": {"type": "object", "required": ["task_id", "lease_id"], "properties": {"task_id": {"type": "string"}, "lease_id": {"type": "string"}}}},
    {"name": "butian_task_heartbeat", "description": "Renew a task lease", "inputSchema": {"type": "object", "required": ["task_id", "lease_id"], "properties": {"task_id": {"type": "string"}, "lease_id": {"type": "string"}, "lease_seconds": {"type": "integer"}}}},
    {"name": "butian_strategy_recommend", "description": "Recommend explainable strategies from task features", "inputSchema": {"type": "object", "required": ["task_features"], "properties": {"task_features": {"type": "object"}}}},
    {"name": "butian_strategy_compose", "description": "Compose member defaults for a task", "inputSchema": {"type": "object", "required": ["member_ids", "task_features"], "properties": {"member_ids": {"type": "array"}, "task_features": {"type": "object"}}}},
    {"name": "butian_strategy_assign", "description": "Assign or override a task strategy with an audit reason", "inputSchema": {"type": "object", "required": ["task_id", "strategy_id"], "properties": {"task_id": {"type": "string"}, "strategy_id": {"type": "string"}, "stages": {"type": "array"}, "reason": {"type": "string"}, "decision_by": {"type": "string"}}}},
    {"name": "butian_strategy_step", "description": "Record an actual strategy stage", "inputSchema": {"type": "object", "required": ["task_id", "revision", "strategy_id", "stage"], "properties": {"task_id": {"type": "string"}, "revision": {"type": "integer"}, "strategy_id": {"type": "string"}, "stage": {"type": "string"}, "member_id": {"type": "string"}, "status": {"type": "string"}, "reason": {"type": "string"}, "output_ref": {"type": "string"}}}},
    {"name": "butian_strategy_steps", "description": "List recorded strategy stages", "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}}},
    {"name": "butian_user_question", "description": "Ask the user and move a task to waiting_user", "inputSchema": {"type": "object", "required": ["task_id", "revision", "member_id", "question"], "properties": {"task_id": {"type": "string"}, "revision": {"type": "integer"}, "member_id": {"type": "string"}, "question": {"type": "string"}}}},
    {"name": "butian_user_answer", "description": "Answer an open user question", "inputSchema": {"type": "object", "required": ["question_id", "answer"], "properties": {"question_id": {"type": "string"}, "answer": {"type": "string"}}}},
    {"name": "butian_evidence_verify", "description": "Verify evidence has not changed", "inputSchema": {"type": "object", "required": ["evidence_id"], "properties": {"evidence_id": {"type": "string"}, "actor_member_id": {"type": "string"}}}},
    {"name": "butian_knowledge_source_add", "description": "Register reproducible external knowledge source", "inputSchema": {"type": "object", "required": ["source_type", "locator", "title", "content_hash"], "properties": {"source_type": {"type": "string"}, "locator": {"type": "string"}, "title": {"type": "string"}, "content_hash": {"type": "string"}, "metadata": {"type": "object"}}}},
    {"name": "butian_knowledge_source_verify", "description": "Verify retrieved source bytes against the registered SHA-256", "inputSchema": {"type": "object", "required": ["source_id", "content"], "properties": {"source_id": {"type": "string"}, "content": {"type": "string"}}}},
    {"name": "butian_knowledge_source_capture", "description": "Fetch an explicitly requested public URL with SSRF and size guards, then hash-verify and register it", "inputSchema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}, "title": {"type": "string"}, "source_type": {"type": "string"}, "timeout": {"type": "number"}, "max_bytes": {"type": "integer"}}}},
    {
        "name": "butian_knowledge_claim_add",
        "description": "Register a source claim as candidate knowledge",
        "inputSchema": {
            "type": "object",
            "required": ["source_id", "claim"],
            "properties": {
                "source_id": {"type": "string"},
                "claim": {"type": "string"},
                "conditions": {"type": "object"},
                "limitations": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
                "predictions": {"type": "array", "items": {"type": "string"}},
                "falsifiers": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {"name": "butian_hypothesis_from_claim", "description": "Convert an external claim into a candidate prior", "inputSchema": {"type": "object", "required": ["claim_id"], "properties": {"claim_id": {"type": "string"}}}},
    {"name": "butian_hypothesis_action_authorize", "description": "Create a verification action awaiting explicit user authorization", "inputSchema": {"type": "object", "required": ["hypothesis_id", "description", "cost", "risk", "information_gain"], "properties": {"hypothesis_id": {"type": "string"}, "description": {"type": "string"}, "cost": {"type": "number"}, "risk": {"type": "number"}, "information_gain": {"type": "number"}}}},
    {"name": "butian_hypothesis_action_grant", "description": "Record explicit authorization decision", "inputSchema": {"type": "object", "required": ["action_id", "actor_member_id", "reason", "authorized"], "properties": {"action_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "reason": {"type": "string"}, "authorized": {"type": "boolean"}}}},
    {"name": "butian_hypothesis_action_start", "description": "Start an authorized hypothesis verification action", "inputSchema": {"type": "object", "required": ["action_id"], "properties": {"action_id": {"type": "string"}}}},
    {"name": "butian_hypothesis_get", "description": "Inspect hypothesis and its observations", "inputSchema": {"type": "object", "required": ["hypothesis_id"], "properties": {"hypothesis_id": {"type": "string"}}}},
    {"name": "butian_hypothesis_observe", "description": "Record evidence-backed hypothesis observation and optionally create candidate memory", "inputSchema": {"type": "object", "required": ["action_id", "outcome", "independent_key"], "properties": {"action_id": {"type": "string"}, "outcome": {"type": "string"}, "evidence_ids": {"type": "array"}, "independent_key": {"type": "string"}, "notes": {"type": "string"}, "environment": {"type": "object"}, "actor_member_id": {"type": "string"}, "member_id": {"type": "string"}}}},
    {"name": "butian_experiment_create", "description": "Create a reproducible experiment record", "inputSchema": {"type": "object", "required": ["name", "task_package", "dataset_version", "code_commit", "provider_version"], "properties": {"name": {"type": "string"}, "task_package": {}, "dataset_version": {"type": "string"}, "code_commit": {"type": "string"}, "configuration": {"type": "object"}, "provider_version": {"type": "string"}, "environment": {"type": "object"}}}},
    {"name": "butian_experiment_event", "description": "Append an immutable experiment event", "inputSchema": {"type": "object", "required": ["experiment_id", "event_type", "payload"], "properties": {"experiment_id": {"type": "string"}, "event_type": {"type": "string"}, "payload": {"type": "object"}}}},
    {"name": "butian_experiment_batch", "description": "Create matched B0/B1/B2 or provider experiment variants", "inputSchema": {"type": "object", "required": ["name", "variants", "task_package", "dataset_version", "code_commit"], "properties": {"name": {"type": "string"}, "variants": {"type": "array", "items": {"type": "object"}}, "task_package": {}, "dataset_version": {"type": "string"}, "code_commit": {"type": "string"}, "configuration": {"type": "object"}, "environment": {"type": "object"}, "repeats": {"type": "integer", "minimum": 1}}}},
    {"name": "butian_experiment_compare", "description": "Compare experiment variants over multiple metrics", "inputSchema": {"type": "object", "required": ["experiment_ids", "metrics"], "properties": {"experiment_ids": {"type": "array", "items": {"type": "string"}}, "metrics": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "butian_peer_ask", "description": "Ask a successful member how they solved a task; creates a candidate only", "inputSchema": {"type": "object", "required": ["asker_id", "expert_id", "question"], "properties": {"asker_id": {"type": "string"}, "expert_id": {"type": "string"}, "question": {"type": "string"}, "task_id": {"type": "string"}, "comparison": {"type": "object"}}}},
    {"name": "butian_peer_answer", "description": "Record an expert answer as an unverified candidate", "inputSchema": {"type": "object", "required": ["learning_id", "answer"], "properties": {"learning_id": {"type": "string"}, "answer": {"type": "string"}, "method": {"type": "string"}, "evidence_ids": {"type": "array"}}}},
    {"name": "butian_peer_verify", "description": "Verify or reject a peer-learning candidate after reproduction", "inputSchema": {"type": "object", "required": ["learning_id", "success"], "properties": {"learning_id": {"type": "string"}, "success": {"type": "boolean"}, "evidence_ids": {"type": "array"}, "notes": {"type": "string"}}}},
    {"name": "butian_peer_list", "description": "List peer-learning records", "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "member_id": {"type": "string"}}}},
    {"name": "butian_evolution_compose", "description": "Compose multiple member memories into a compound memory with audited gain", "inputSchema": {"type": "object", "required": ["member_id", "result_memory_id", "component_memory_ids", "content"], "properties": {"member_id": {"type": "string"}, "result_memory_id": {"type": "string"}, "component_memory_ids": {"type": "array", "items": {"type": "string"}}, "content": {"type": "string"}, "kind": {"type": "string"}, "environment": {"type": "object"}, "composition_bonus": {"type": "number"}, "redundancy_penalty": {"type": "number"}, "conflict_penalty": {"type": "number"}}}},
    {"name": "butian_evolution_skill_update", "description": "Advance or regress a member skill phase from observable evidence", "inputSchema": {"type": "object", "required": ["member_id", "domain", "level", "marginal"], "properties": {"member_id": {"type": "string"}, "domain": {"type": "string"}, "level": {"type": "number"}, "marginal": {"type": "number"}, "contradiction": {"type": "boolean"}, "novel_environment": {"type": "boolean"}, "independent_successes": {"type": "integer"}, "composition_successes": {"type": "integer"}, "regression_evidence": {"type": "boolean"}}}},
    {"name": "butian_evolution_skills", "description": "List a member's skill phases and phase-change events", "inputSchema": {"type": "object", "required": ["member_id"], "properties": {"member_id": {"type": "string"}}}},
    {"name": "butian_evolution_degradation", "description": "Assess a memory's degradation and update its freshness", "inputSchema": {"type": "object", "required": ["member_id", "memory_id"], "properties": {"member_id": {"type": "string"}, "memory_id": {"type": "string"}, "half_life_days": {"type": "number"}, "current_environment": {"type": "object"}, "consecutive_failures": {"type": "integer"}, "contradiction_count": {"type": "integer"}, "dependency_drift_score": {"type": "number"}, "hard_failure": {"type": "boolean"}}}},
    {"name": "butian_evolution_cool", "description": "Cool a warm or hot memory", "inputSchema": {"type": "object", "required": ["member_id", "memory_id"], "properties": {"member_id": {"type": "string"}, "memory_id": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "butian_evolution_rewarm", "description": "Rewarm a cold memory after an evidenced success", "inputSchema": {"type": "object", "required": ["member_id", "memory_id", "success"], "properties": {"member_id": {"type": "string"}, "memory_id": {"type": "string"}, "success": {"type": "boolean"}, "evidence_ref": {"type": "string"}}}},
    {"name": "butian_evolution_versions", "description": "List a memory's version chain and verify its integrity", "inputSchema": {"type": "object", "required": ["member_id", "memory_id"], "properties": {"member_id": {"type": "string"}, "memory_id": {"type": "string"}}}},
    {"name": "butian_dream_run", "description": "Run a gated dream introspection cycle over a member's memories", "inputSchema": {"type": "object", "required": ["member_id"], "properties": {"member_id": {"type": "string"}, "min_fragments": {"type": "integer"}, "min_interval": {"type": "number"}, "budget": {"type": "integer"}, "lock_timeout": {"type": "number"}}}},
    {"name": "butian_dream_distill", "description": "Distill multiple memories into a reversible summary", "inputSchema": {"type": "object", "required": ["member_id", "memory_ids", "summary"], "properties": {"member_id": {"type": "string"}, "memory_ids": {"type": "array", "items": {"type": "string"}}, "summary": {"type": "string"}, "merged_items": {"type": "array"}, "conflicts": {"type": "array"}, "discarded_items": {"type": "array"}, "information_loss": {"type": "number"}}}},
    {"name": "butian_dream_abstract", "description": "Abstract a pattern from multiple memories with applicability and failure conditions", "inputSchema": {"type": "object", "required": ["member_id", "memory_ids", "pattern"], "properties": {"member_id": {"type": "string"}, "memory_ids": {"type": "array", "items": {"type": "string"}}, "pattern": {"type": "string"}, "supports": {"type": "array"}, "counterexamples": {"type": "array"}, "applicability": {"type": "array"}, "failure_conditions": {"type": "array"}, "abstraction_level": {"type": "integer"}}}},
    {"name": "butian_dream_propose", "description": "Propose a hypothesis from a dream candidate with a prior reliability", "inputSchema": {"type": "object", "required": ["member_id", "candidate_id", "statement", "prior_reliability", "argument"], "properties": {"member_id": {"type": "string"}, "candidate_id": {"type": "string"}, "statement": {"type": "string"}, "prior_reliability": {"type": "number"}, "argument": {"type": "string"}, "evidence_refs": {"type": "array"}}}},
    {"name": "butian_dream_schedule", "description": "Schedule a verification action with cost, risk and information gain", "inputSchema": {"type": "object", "required": ["member_id", "hypothesis_id", "description", "cost", "risk", "expected_information_gain"], "properties": {"member_id": {"type": "string"}, "hypothesis_id": {"type": "string"}, "description": {"type": "string"}, "cost": {"type": "number"}, "risk": {"type": "number"}, "expected_information_gain": {"type": "number"}, "authorized": {"type": "boolean"}}}},
    {"name": "butian_dream_verify", "description": "Record the outcome of a dream hypothesis verification with evidence", "inputSchema": {"type": "object", "required": ["member_id", "hypothesis_id", "outcome"], "properties": {"member_id": {"type": "string"}, "hypothesis_id": {"type": "string"}, "outcome": {"type": "string"}, "evidence_refs": {"type": "array"}, "new_memory_id": {"type": "string"}, "new_content": {"type": "string"}}}},
    {"name": "butian_dream_queue", "description": "List a member's pending dream verification queue", "inputSchema": {"type": "object", "required": ["member_id"], "properties": {"member_id": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "butian_dream_history", "description": "List a member's dream runs, candidates, hypotheses and distillations", "inputSchema": {"type": "object", "required": ["member_id"], "properties": {"member_id": {"type": "string"}, "limit": {"type": "integer"}}}},
]


TOOLS.extend([
    {"name": "butian_tool_propose", "description": "Propose a reusable owned tool", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "name", "description", "reuse_lower", "manual_cost_upper", "call_cost_lower", "build_cost_upper", "maintenance_cost_upper", "risk_cost_upper"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}, "reuse_lower": {"type": "number"}, "manual_cost_upper": {"type": "number"}, "call_cost_lower": {"type": "number"}, "build_cost_upper": {"type": "number"}, "maintenance_cost_upper": {"type": "number"}, "risk_cost_upper": {"type": "number"}, "manifest": {"type": "object"}}}},
    {"name": "butian_tool_version_add", "description": "Add an owned tool version", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "tool_id", "content", "manifest"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "tool_id": {"type": "string"}, "content": {"type": "string"}, "manifest": {"type": "object"}}}},
    {"name": "butian_tool_verify", "description": "Verify an owned tool", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "tool_id", "version", "outcome", "independent_key"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "tool_id": {"type": "string"}, "version": {"type": "integer"}, "outcome": {"type": "string"}, "independent_key": {"type": "string"}}}},
    {"name": "butian_tool_share", "description": "Share an active tool", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "tool_id", "recipient_member_id"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "tool_id": {"type": "string"}, "recipient_member_id": {"type": "string"}, "permission": {"type": "string"}}}},
    {"name": "butian_tool_list", "description": "List owned tools and incoming shares", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "status": {"type": "string"}}}},
    {"name": "butian_tool_accept", "description": "Accept a shared tool for trial", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "share_id"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "share_id": {"type": "string"}}}},
    {"name": "butian_tool_trial", "description": "Record a controlled shared-tool trial", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "share_id", "outcome", "independent_key"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "share_id": {"type": "string"}, "outcome": {"type": "string"}, "independent_key": {"type": "string"}}}},
    {"name": "butian_tool_archive", "description": "Archive an owned tool", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "tool_id"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "tool_id": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "butian_tool_restore", "description": "Restore an archived tool", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "tool_id"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "tool_id": {"type": "string"}}}},
    {"name": "butian_tool_execute", "description": "Execute one fixed active tool version with audit and optional dry-run", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token", "tool_id", "version"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "tool_id": {"type": "string"}, "version": {"type": "integer"}, "input": {}, "dry_run": {"type": "boolean"}, "task_id": {"type": "string"}, "share_id": {"type": "string"}}}},
    {"name": "butian_tool_executions", "description": "List a member's tool execution audit records", "inputSchema": {"type": "object", "required": ["member_id", "actor_member_id", "token"], "properties": {"member_id": {"type": "string"}, "actor_member_id": {"type": "string"}, "token": {"type": "string"}, "tool_id": {"type": "string"}, "limit": {"type": "integer"}}}},
])


def run(home=None):
    service = ButianService(home)
    for line in sys.stdin:
        try:
            req = json.loads(line); method = req.get("method"); params = req.get("params", {})
            if method == "initialize": result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "butian", "version": "0.1.0"}}
            elif method == "notifications/initialized": continue
            elif method == "tools/list": result = {"tools": TOOLS}
            elif method == "tools/call": result = call_tool(service, params.get("name"), params.get("arguments", {}))
            else: result = {}
            if "id" in req: print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32000, "message": str(exc)}}, ensure_ascii=False), flush=True)
    service.close()

# Keep the public MCP contract aligned with the member boundary enforced below.
_MEMBER_TOOL_PREFIXES = ("butian_memory_", "butian_evolution_", "butian_dream_")
_MEMBER_TOOL_NAMES = {tool["name"] for tool in TOOLS if tool["name"].startswith(_MEMBER_TOOL_PREFIXES)}
_MEMBER_TOOL_NAMES |= {
    "butian_provider_start", "butian_provider_consume", "butian_task_list", "butian_task_tree",
    "butian_task_review", "butian_task_create", "butian_task_transition", "butian_task_claim",
    "butian_task_release", "butian_task_heartbeat", "butian_strategy_compose", "butian_strategy_assign",
    "butian_strategy_step", "butian_strategy_steps", "butian_user_question", "butian_user_answer",
    "butian_evidence_verify", "butian_knowledge_source_add", "butian_knowledge_source_verify",
    "butian_knowledge_source_capture", "butian_knowledge_claim_add", "butian_hypothesis_from_claim",
    "butian_hypothesis_action_authorize", "butian_hypothesis_action_grant", "butian_hypothesis_action_start", "butian_tool_propose", "butian_tool_version_add", "butian_tool_verify", "butian_tool_share",
    "butian_hypothesis_get", "butian_hypothesis_observe", "butian_peer_ask", "butian_peer_answer",
    "butian_peer_verify", "butian_peer_list",
}
for _action, _fields in {
    "issue_report": {"tool_id": "string", "version": "integer", "description": "string"},
    "issues": {"tool_id": "string"},
    "issue_repair": {"issue_id": "string", "repair_version": "integer", "diagnosis": "string"},
    "repair_confirm": {"issue_id": "string", "execution_id": "string", "resolved": "boolean"},
}.items():
    _properties = {key: {"type": value} for key, value in _fields.items()}
    _properties.update({key: {"type": "string"} for key in ("member_id", "actor_member_id", "token")})
    if _action == "issue_report":
        _properties.update({"execution_id": {"type": "string"}, "severity": {"type": "string"}})
    TOOLS.append({"name": "butian_tool_" + _action, "description": "Tool developer feedback: " + _action,
                  "inputSchema": {"type": "object", "properties": _properties,
                                  "required": list(_fields) + ["member_id", "actor_member_id", "token"]}})

for _tool in TOOLS:
    if _tool["name"] in _MEMBER_TOOL_NAMES:
        _props = _tool["inputSchema"].setdefault("properties", {})
        _props.setdefault("actor_member_id", {"type": "string"})
        _props.setdefault("token", {"type": "string"})
        _required = _tool["inputSchema"].setdefault("required", [])
        for _field in ("actor_member_id", "token"):
            if _field not in _required:
                _required.append(_field)

def _require_member_arg(service, args, member_id=None):
    target = member_id or args.get("member_id")
    if not target:
        raise ValueError("member_id_required")
    service.require_member_access(target, actor_member_id=args.get("actor_member_id"), token=args.get("token"))
    return target


def call_tool(service, name, args):
    if name == "butian_status": return {"ok": True, "service": "butian"}
    member_tools = {tool["name"] for tool in TOOLS if tool["name"].startswith(("butian_memory_", "butian_evolution_", "butian_dream_"))}
    member_tools.update({"butian_tool_issue_report", "butian_tool_issues", "butian_tool_issue_repair", "butian_tool_repair_confirm"})
    member_tools |= {"butian_tool_propose", "butian_tool_version_add", "butian_tool_verify", "butian_tool_share", "butian_tool_execute", "butian_tool_executions", "butian_tool_list", "butian_tool_accept", "butian_tool_trial", "butian_tool_archive", "butian_tool_restore", "butian_provider_start", "butian_provider_consume", "butian_task_list", "butian_task_tree", "butian_task_review", "butian_task_create", "butian_task_transition", "butian_task_claim", "butian_task_release", "butian_task_heartbeat", "butian_strategy_compose", "butian_strategy_assign", "butian_strategy_step", "butian_strategy_steps", "butian_user_question", "butian_user_answer", "butian_evidence_verify", "butian_knowledge_source_add", "butian_knowledge_source_verify", "butian_knowledge_source_capture", "butian_knowledge_claim_add", "butian_hypothesis_from_claim", "butian_hypothesis_action_authorize", "butian_hypothesis_action_grant", "butian_hypothesis_action_start", "butian_hypothesis_get", "butian_hypothesis_observe", "butian_peer_ask", "butian_peer_answer", "butian_peer_verify", "butian_peer_list"}
    admin_tools = {"butian_member_preflight", "butian_member_import", "butian_migration_runs", "butian_dispatcher_set", "butian_session_create", "butian_session_switch_mode", "butian_session_list", "butian_strategy_recommend", "butian_experiment_create", "butian_experiment_event", "butian_experiment_batch", "butian_experiment_compare", "butian_knowledge_source_add", "butian_knowledge_source_verify", "butian_knowledge_source_capture", "butian_knowledge_claim_add", "butian_hypothesis_from_claim", "butian_hypothesis_action_authorize", "butian_hypothesis_action_grant", "butian_hypothesis_action_start", "butian_hypothesis_get", "butian_hypothesis_observe", "butian_peer_ask", "butian_peer_answer", "butian_peer_verify", "butian_peer_list", "butian_evidence_verify"}
    if name in admin_tools:
        if name in {"butian_session_create", "butian_session_switch_mode"}:
            actor = args.get("current_member_id") or args.get("dispatcher_member_id")
            if actor:
                _require_member_arg(service, args, member_id=actor)
                admin_tools.remove(name)
            else:
                raise ValueError("admin_authorization_required")
        if name in admin_tools:
            raise ValueError("admin_authorization_required")
    if name in member_tools:
        resource_member = args.get("member_id") or args.get("actor_member_id") or args.get("dispatcher_member_id") or args.get("current_member_id") or args.get("reviewer_member_id") or args.get("asker_id")
        if name == "butian_provider_consume":
            row = service.store.db.execute("SELECT member_id FROM provider_sessions WHERE provider_session_id=?", (args.get("provider_session_id"),)).fetchone()
            resource_member = row["member_id"] if row else None
        elif name.startswith(("butian_task_", "butian_strategy_")) or name in {"butian_user_question", "butian_user_answer"}:
            task_id = args.get("task_id")
            if not task_id and args.get("question_id"):
                question = service.store.db.execute("SELECT task_id,member_id FROM questions WHERE question_id=?", (args["question_id"],)).fetchone()
                task_id = question["task_id"] if question else None
                resource_member = resource_member or (question["member_id"] if question else None)
            row = service.store.db.execute("SELECT assignee_id,session_id FROM tasks WHERE task_id=?", (task_id,)).fetchone() if task_id else None
            if row:
                session = service.store.session(row["session_id"])
                resource_member = resource_member or row["assignee_id"] or session.get("dispatcher_member_id")
        if not resource_member:
            raise ValueError("member_id_required")
        _require_member_arg(service, args, member_id=resource_member)
    if name == "butian_member_preflight": return service.preflight_members(args["source_path"], roles_dir=args.get("roles_dir"))
    if name == "butian_member_import": return service.import_members(args["source_path"], roles_dir=args.get("roles_dir"), update_existing=args.get("update_existing", False), import_memories=args.get("import_memories", False))
    if name == "butian_migration_runs": return {"runs": service.list_migration_runs(limit=args.get("limit", 50))}
    if name == "butian_tool_issue_report": return service.report_tool_issue(args["member_id"], args["tool_id"], args["version"], description=args["description"], execution_id=args.get("execution_id"), severity=args.get("severity", "bug"))
    if name == "butian_tool_issues": return {"issues": service.list_tool_issues(args["member_id"], args["tool_id"])}
    if name == "butian_tool_issue_repair": return service.repair_tool_issue(args["member_id"], args["issue_id"], repair_version=args["repair_version"], diagnosis=args["diagnosis"])
    if name == "butian_tool_repair_confirm": return service.confirm_tool_repair(args["member_id"], args["issue_id"], execution_id=args["execution_id"], resolved=args["resolved"])
    if name == "butian_tool_execute": return service.execute_tool(args["member_id"], args["tool_id"], args["version"], args.get("input"), dry_run=args.get("dry_run", False), task_id=args.get("task_id"), share_id=args.get("share_id"))
    if name == "butian_tool_executions": return {"executions": service.list_tool_executions(args["member_id"], tool_id=args.get("tool_id"), limit=args.get("limit", 50))}
    if name == "butian_tool_propose": return service.propose_tool(args["member_id"], args["name"], args["description"], reuse_lower=args["reuse_lower"], manual_cost_upper=args["manual_cost_upper"], call_cost_lower=args["call_cost_lower"], build_cost_upper=args["build_cost_upper"], maintenance_cost_upper=args["maintenance_cost_upper"], risk_cost_upper=args["risk_cost_upper"], manifest=args.get("manifest"))
    if name == "butian_tool_version_add": return service.add_tool_version(args["member_id"], args["tool_id"], args["content"].encode("utf-8"), args["manifest"])
    if name == "butian_tool_verify": return service.verify_tool(args["member_id"], args["tool_id"], args["version"], args["outcome"], independent_key=args["independent_key"], task_id=args.get("task_id"), evidence_ids=args.get("evidence_ids"), environment=args.get("environment"))
    if name == "butian_tool_share": return service.share_tool(args["member_id"], args["tool_id"], args["recipient_member_id"], permission=args.get("permission", "team_readonly"))
    if name == "butian_tool_list": return {"tools": service.list_tools(args["member_id"], status=args.get("status")), "shares": service.list_tool_shares(args["member_id"], status=args.get("status"))}
    if name == "butian_tool_accept": return service.accept_tool_share(args["member_id"], args["share_id"])
    if name == "butian_tool_trial": return service.trial_tool(args["member_id"], args["share_id"], args["outcome"], independent_key=args["independent_key"], task_id=args.get("task_id"), environment=args.get("environment"))
    if name == "butian_tool_archive": return service.archive_tool(args["member_id"], args["tool_id"], reason=args.get("reason", "maintenance"))
    if name == "butian_tool_restore": return service.restore_tool(args["member_id"], args["tool_id"])
    if name == "butian_provider_start": return service.start_provider_task(args["member_id"], args["prompt"], task_id=args.get("task_id"), provider_id=args.get("provider_id"), model=args.get("model"))
    if name == "butian_provider_consume": return service.consume_provider_task(args["provider_session_id"], args["prompt"])
    if name == "butian_task_list": return {"tasks": service.store.list_tasks(args.get("status"))}
    if name == "butian_session_create": return service.create_session(args.get("mode", "solo"), args.get("dispatcher_member_id"), args.get("requested_model"), args.get("capabilities"), current_member_id=args.get("current_member_id"))
    if name == "butian_session_switch_mode": return service.switch_session_mode(args["session_id"], args["mode"], current_member_id=args.get("current_member_id"))
    if name == "butian_session_list": return {"sessions": [dict(r) for r in service.store.db.execute("SELECT * FROM sessions ORDER BY created_at")], "dispatcher_config": service.get_dispatcher_config()}
    if name == "butian_dispatcher_set": service.set_dispatcher_config(args["member_id"]); return {"dispatcher_member_id": args["member_id"]}
    if name == "butian_task_tree": return {"tree": service.task_tree(args.get("session_id"))}
    if name == "butian_task_review": return service.review_task(args["task_id"], args["revision"], args["verdict"], reviewer_member_id=args.get("reviewer_member_id"), notes=args.get("notes", ""), rework_limit=args.get("rework_limit", 3))
    if name == "butian_task_create": return {"task_id": service.store.create_task(args["session_id"], args["title"], args.get("assignee_id"), args.get("parent_id"), args.get("payload"))}
    if name == "butian_task_transition":
        service.store.transition_task(args["task_id"], args["expected_revision"], args["target"], args["operation_key"]); return {"ok": True}
    if name == "butian_task_claim":
        return service.store.claim_task(args["task_id"], args["member_id"], args.get("lease_seconds", 300), args.get("token"))
    if name == "butian_task_release":
        service.store.release_lease(args["task_id"], args["lease_id"]); return {"ok": True}
    if name == "butian_task_heartbeat":
        service.store.heartbeat(args["task_id"], args["lease_id"], args.get("lease_seconds", 300)); return {"ok": True}
    if name == "butian_strategy_recommend":
        return {"recommendations": service.recommend_strategies(args["task_features"])}
    if name == "butian_strategy_compose":
        return service.compose_strategies(args["member_ids"], args["task_features"])
    if name == "butian_strategy_assign":
        return service.assign_strategy(args["task_id"], args["strategy_id"], args.get("stages"), args.get("reason"), args.get("decision_by"))
    if name == "butian_strategy_step":
        return {"step_id": service.store.record_strategy_step(args["task_id"], args["revision"], args["strategy_id"], args["stage"], member_id=args.get("member_id"), status=args.get("status", "completed"), reason=args.get("reason"), output_ref=args.get("output_ref"))}
    if name == "butian_strategy_steps":
        return {"steps": service.store.list_strategy_steps(args["task_id"])}
    if name == "butian_user_question":
        return {"question_id": service.ask_user(args["task_id"], args["revision"], args["member_id"], args["question"])}
    if name == "butian_user_answer":
        return {"task_id": service.answer_user(args["question_id"], args["answer"])}
    if name == "butian_evidence_verify": return service.store.verify_evidence(args["evidence_id"], actor_member_id=args.get("actor_member_id"))
    if name == "butian_knowledge_source_add": return {"source_id": service.learning.add_source(args["source_type"], args["locator"], args["title"], args["content_hash"], metadata=args.get("metadata"))}
    if name == "butian_knowledge_source_verify": return service.learning.verify_source_content(args["source_id"], args["content"])
    if name == "butian_knowledge_source_capture":
        result = service.learning.capture_url(args["url"], title=args.get("title"), source_type=args.get("source_type", "web"), timeout=float(args.get("timeout", 10)), max_bytes=int(args.get("max_bytes", 1000000)))
        result.pop("content", None)
        return result
    if name == "butian_knowledge_claim_add": return {"claim_id": service.learning.add_claim(args["source_id"], args["claim"], conditions=args.get("conditions"), limitations=args.get("limitations"), confidence=args.get("confidence", 0.0), predictions=args.get("predictions"), falsifiers=args.get("falsifiers"))}
    if name == "butian_hypothesis_from_claim": return service.promote_claim_to_prior(args["claim_id"])
    if name == "butian_hypothesis_action_authorize": return {"action_id": service.authorize_hypothesis(args["hypothesis_id"], args["description"], cost=args["cost"], risk=args["risk"], information_gain=args["information_gain"])}
    if name == "butian_hypothesis_action_grant": return {"ok": service.grant_hypothesis_authorization(args["action_id"], actor_member_id=args["actor_member_id"], reason=args["reason"], authorized=args["authorized"]) is None}
    if name == "butian_hypothesis_action_start": return {"ok": service.start_hypothesis_action(args["action_id"]) is None}
    if name == "butian_hypothesis_get": return service.get_hypothesis(args["hypothesis_id"])
    if name == "butian_hypothesis_observe": return service.observe_hypothesis(args["action_id"], args["outcome"], args.get("evidence_ids", []), independent_key=args["independent_key"], notes=args.get("notes", ""), environment=args.get("environment"), actor_member_id=args.get("actor_member_id"), member_id=args.get("member_id"))
    if name == "butian_experiment_create":
        from .runtime.experiments import ExperimentStore
        store = ExperimentStore(service.home / "experiments.db")
        try: return {"experiment_id": store.create(args["name"], task_package=args["task_package"], dataset_version=args["dataset_version"], code_commit=args["code_commit"], configuration=args.get("configuration", {}), provider_version=args["provider_version"], environment=args.get("environment"))}
        finally: store.close()
    if name == "butian_experiment_event":
        from .runtime.experiments import ExperimentStore
        store = ExperimentStore(service.home / "experiments.db")
        try: return {"event_id": store.add_event(args["experiment_id"], args["event_type"], args["payload"])}
        finally: store.close()
    if name in {"butian_experiment_batch", "butian_experiment_compare"}:
        from .runtime.experiments import ExperimentStore
        store = ExperimentStore(service.home / "experiments.db")
        try:
            if name == "butian_experiment_batch":
                return store.run_batch(args["name"], args["variants"], task_package=args["task_package"], dataset_version=args["dataset_version"], code_commit=args["code_commit"], configuration=args.get("configuration"), environment=args.get("environment"), repeats=args.get("repeats", 1))
            return store.batch_compare(args["experiment_ids"], args["metrics"])
        finally: store.close()
    if name == "butian_peer_ask": return {"learning_id": service.ask_peer(args["asker_id"], args["expert_id"], args["question"], task_id=args.get("task_id"), comparison=args.get("comparison"))}
    if name == "butian_peer_answer": return service.answer_peer(args["learning_id"], args["answer"], method=args.get("method"), evidence_ids=args.get("evidence_ids"))
    if name == "butian_peer_verify": return service.verify_peer_learning(args["learning_id"], success=args["success"], evidence_ids=args.get("evidence_ids"), notes=args.get("notes", ""))
    if name == "butian_peer_list": return {"learning": service.list_peer_learning(status=args.get("status"), member_id=args.get("member_id"))}
    if name == "butian_memory_search":
        service.require_member_access(args["member_id"], actor_member_id=args.get("actor_member_id"), token=args.get("token"))
        memory = service.memory(args["member_id"], actor_member_id=args["member_id"])
        try: return {"memories": memory.search(args["query"], kind=args.get("kind"))}
        finally: memory.close()
    if name == "butian_memory_recall":
        service.require_member_access(args["member_id"], actor_member_id=args.get("actor_member_id"), token=args.get("token"))
        return service.recall_for_decision(args["member_id"], args["query"], decision_id=args["decision_id"], environment=args.get("environment"), limit=args.get("limit", 20))
    if name == "butian_memory_recall_outcome":
        service.require_member_access(args["member_id"], actor_member_id=args.get("actor_member_id"), token=args.get("token"))
        return {"ok": service.record_recall_outcome(args["member_id"], args["recall_id"], used_memory_ids=args["used_memory_ids"], missed=args["missed"], useful_memory_ids=args.get("useful_memory_ids"), harmful_memory_ids=args.get("harmful_memory_ids"), evidence_ref=args.get("evidence_ref"))}
    if name == "butian_memory_recall_audit":
        service.require_member_access(args["member_id"], actor_member_id=args.get("actor_member_id"), token=args.get("token"))
        return {"decisions": service.recall_audit(args["member_id"])}
    if name == "butian_memory_practice":
        service.require_member_access(args["member_id"], actor_member_id=args.get("actor_member_id"), token=args.get("token"))
        return service.record_practice_outcome(args["member_id"], memory_id=args["memory_id"], kind=args["kind"], content=args["content"], outcome=args["outcome"], independent_key=args["independent_key"], evidence_ref=args.get("evidence_ref"), environment=args.get("environment"), source_event_ids=args.get("source_event_ids"))
    if name == "butian_evolution_compose":
        return service.compose_memories(args["member_id"], args["result_memory_id"], args["component_memory_ids"], content=args["content"], kind=args.get("kind", "compound"), environment=args.get("environment"), composition_bonus=args.get("composition_bonus", 0.0), redundancy_penalty=args.get("redundancy_penalty", 0.0), conflict_penalty=args.get("conflict_penalty", 0.0))
    if name == "butian_evolution_skill_update":
        return service.update_skill(args["member_id"], args["domain"], level=args["level"], marginal=args["marginal"], contradiction=args.get("contradiction", False), novel_environment=args.get("novel_environment", False), independent_successes=args.get("independent_successes", 0), composition_successes=args.get("composition_successes", 0), regression_evidence=args.get("regression_evidence", False))
    if name == "butian_evolution_skills":
        return {"skills": service.list_skills(args["member_id"]), "events": service.list_skill_events(args["member_id"])}
    if name == "butian_evolution_degradation":
        return service.assess_memory_degradation(args["member_id"], args["memory_id"], half_life_days=args.get("half_life_days", 30.0), current_environment=args.get("current_environment"), consecutive_failures=args.get("consecutive_failures", 0), contradiction_count=args.get("contradiction_count", 0), dependency_drift_score=args.get("dependency_drift_score", 0.0), hard_failure=args.get("hard_failure", False))
    if name == "butian_evolution_cool":
        return service.cool_memory(args["member_id"], args["memory_id"], reason=args.get("reason", "stale"))
    if name == "butian_evolution_rewarm":
        return service.rewarm_memory(args["member_id"], args["memory_id"], success=args["success"], evidence_ref=args.get("evidence_ref"))
    if name == "butian_evolution_versions":
        return service.memory_versions(args["member_id"], args["memory_id"])
    if name == "butian_dream_run":
        return service.dream_run(args["member_id"], min_fragments=args.get("min_fragments", 3), min_interval=args.get("min_interval", 3600), budget=args.get("budget", 10), lock_timeout=args.get("lock_timeout", 7200))
    if name == "butian_dream_distill":
        return service.dream_distill(args["member_id"], args["memory_ids"], summary=args["summary"], merged_items=args.get("merged_items"), conflicts=args.get("conflicts"), discarded_items=args.get("discarded_items"), information_loss=args.get("information_loss", 0.0))
    if name == "butian_dream_abstract":
        return service.dream_abstract(args["member_id"], args["memory_ids"], pattern=args["pattern"], supports=args.get("supports"), counterexamples=args.get("counterexamples"), applicability=args.get("applicability"), failure_conditions=args.get("failure_conditions"), abstraction_level=args.get("abstraction_level", 1))
    if name == "butian_dream_propose":
        return service.dream_propose_hypothesis(args["member_id"], args["candidate_id"], args["statement"], prior_reliability=args["prior_reliability"], argument=args["argument"], evidence_refs=args.get("evidence_refs"))
    if name == "butian_dream_schedule":
        return service.dream_schedule_verification(args["member_id"], args["hypothesis_id"], args["description"], cost=args["cost"], risk=args["risk"], expected_information_gain=args["expected_information_gain"], authorized=args.get("authorized", False))
    if name == "butian_dream_verify":
        return service.dream_record_verification(args["member_id"], args["hypothesis_id"], outcome=args["outcome"], evidence_refs=args.get("evidence_refs", []), new_memory_id=args.get("new_memory_id"), new_content=args.get("new_content"))
    if name == "butian_dream_queue":
        return service.dream_verification_queue(args["member_id"], limit=args.get("limit", 20))
    if name == "butian_dream_history":
        limit = args.get("limit", 20)
        return {"runs": service.list_dream_runs(args["member_id"], limit=limit), "candidates": service.list_dream_candidates(args["member_id"], limit=limit), "hypotheses": service.list_dream_hypotheses(args["member_id"], limit=limit), "distillations": service.list_dream_distillations(args["member_id"], limit=limit), "abstractions": service.list_dream_abstractions(args["member_id"], limit=limit)}
    raise ValueError("unknown_tool")
