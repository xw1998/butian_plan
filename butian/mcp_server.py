from __future__ import annotations
import json
import sys
from .runtime.service import ButianService

TOOLS = [
    {"name": "butian_status", "description": "Return Butian runtime status", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "butian_memory_search", "description": "Search a member's isolated memory", "inputSchema": {"type": "object", "required": ["member_id", "query"], "properties": {"member_id": {"type": "string"}, "query": {"type": "string"}, "kind": {"type": "string"}}}},
    {"name": "butian_task_list", "description": "List orchestration tasks", "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}},
    {"name": "butian_session_create", "description": "Create solo or team session", "inputSchema": {"type": "object", "required": ["mode"], "properties": {"mode": {"type": "string", "enum": ["solo", "team"]}, "dispatcher_member_id": {"type": "string"}, "requested_model": {"type": "string"}, "capabilities": {"type": "object"}}}},
    {"name": "butian_task_create", "description": "Create a task", "inputSchema": {"type": "object", "required": ["session_id", "title"], "properties": {"session_id": {"type": "string"}, "title": {"type": "string"}, "assignee_id": {"type": "string"}}}},
    {"name": "butian_task_transition", "description": "Transition a task with revision guard", "inputSchema": {"type": "object", "required": ["task_id", "expected_revision", "target", "operation_key"], "properties": {"task_id": {"type": "string"}, "expected_revision": {"type": "integer"}, "target": {"type": "string"}, "operation_key": {"type": "string"}}}},
    {"name": "butian_task_claim", "description": "Claim a ready task with a lease", "inputSchema": {"type": "object", "required": ["task_id", "member_id"], "properties": {"task_id": {"type": "string"}, "member_id": {"type": "string"}, "lease_seconds": {"type": "integer"}, "token": {"type": "string"}}}},
    {"name": "butian_task_release", "description": "Release a task lease and requeue claimed work", "inputSchema": {"type": "object", "required": ["task_id", "lease_id"], "properties": {"task_id": {"type": "string"}, "lease_id": {"type": "string"}}}},
    {"name": "butian_task_heartbeat", "description": "Renew a task lease", "inputSchema": {"type": "object", "required": ["task_id", "lease_id"], "properties": {"task_id": {"type": "string"}, "lease_id": {"type": "string"}, "lease_seconds": {"type": "integer"}}}},
    {"name": "butian_user_question", "description": "Ask the user and move a task to waiting_user", "inputSchema": {"type": "object", "required": ["task_id", "revision", "member_id", "question"], "properties": {"task_id": {"type": "string"}, "revision": {"type": "integer"}, "member_id": {"type": "string"}, "question": {"type": "string"}}}},
    {"name": "butian_user_answer", "description": "Answer an open user question", "inputSchema": {"type": "object", "required": ["question_id", "answer"], "properties": {"question_id": {"type": "string"}, "answer": {"type": "string"}}}},
    {"name": "butian_evidence_verify", "description": "Verify evidence has not changed", "inputSchema": {"type": "object", "required": ["evidence_id"], "properties": {"evidence_id": {"type": "string"}}}},
    {"name": "butian_peer_ask", "description": "Ask a successful member how they solved a task; creates a candidate only", "inputSchema": {"type": "object", "required": ["asker_id", "expert_id", "question"], "properties": {"asker_id": {"type": "string"}, "expert_id": {"type": "string"}, "question": {"type": "string"}, "task_id": {"type": "string"}, "comparison": {"type": "object"}}}},
    {"name": "butian_peer_answer", "description": "Record an expert answer as an unverified candidate", "inputSchema": {"type": "object", "required": ["learning_id", "answer"], "properties": {"learning_id": {"type": "string"}, "answer": {"type": "string"}, "method": {"type": "string"}, "evidence_ids": {"type": "array"}}}},
    {"name": "butian_peer_verify", "description": "Verify or reject a peer-learning candidate after reproduction", "inputSchema": {"type": "object", "required": ["learning_id", "success"], "properties": {"learning_id": {"type": "string"}, "success": {"type": "boolean"}, "evidence_ids": {"type": "array"}, "notes": {"type": "string"}}}},
    {"name": "butian_peer_list", "description": "List peer-learning records", "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "member_id": {"type": "string"}}}},
]

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

def call_tool(service, name, args):
    if name == "butian_status": return {"ok": True, "service": "butian"}
    if name == "butian_task_list": return {"tasks": service.store.list_tasks(args.get("status"))}
    if name == "butian_session_create": return service.create_session(args["mode"], args.get("dispatcher_member_id"), args.get("requested_model"), args.get("capabilities"))
    if name == "butian_task_create": return {"task_id": service.store.create_task(args["session_id"], args["title"], args.get("assignee_id"))}
    if name == "butian_task_transition":
        service.store.transition_task(args["task_id"], args["expected_revision"], args["target"], args["operation_key"]); return {"ok": True}
    if name == "butian_task_claim":
        return service.store.claim_task(args["task_id"], args["member_id"], args.get("lease_seconds", 300), args.get("token"))
    if name == "butian_task_release":
        service.store.release_lease(args["task_id"], args["lease_id"]); return {"ok": True}
    if name == "butian_task_heartbeat":
        service.store.heartbeat(args["task_id"], args["lease_id"], args.get("lease_seconds", 300)); return {"ok": True}
    if name == "butian_user_question":
        return {"question_id": service.ask_user(args["task_id"], args["revision"], args["member_id"], args["question"])}
    if name == "butian_user_answer":
        return {"task_id": service.answer_user(args["question_id"], args["answer"])}
    if name == "butian_evidence_verify": return service.store.verify_evidence(args["evidence_id"])
    if name == "butian_peer_ask": return {"learning_id": service.ask_peer(args["asker_id"], args["expert_id"], args["question"], task_id=args.get("task_id"), comparison=args.get("comparison"))}
    if name == "butian_peer_answer": return service.answer_peer(args["learning_id"], args["answer"], method=args.get("method"), evidence_ids=args.get("evidence_ids"))
    if name == "butian_peer_verify": return service.verify_peer_learning(args["learning_id"], success=args["success"], evidence_ids=args.get("evidence_ids"), notes=args.get("notes", ""))
    if name == "butian_peer_list": return {"learning": service.list_peer_learning(status=args.get("status"), member_id=args.get("member_id"))}
    if name == "butian_memory_search":
        memory = service.memory(args["member_id"])
        try: return {"memories": memory.search(args["query"], kind=args.get("kind"))}
        finally: memory.close()
    raise ValueError("unknown_tool")
