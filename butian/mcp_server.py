from __future__ import annotations
import json
import sys
from .runtime.service import ButianService

TOOLS = [
    {"name": "butian_status", "description": "Return Butian runtime status", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "butian_memory_search", "description": "Search a member's isolated memory", "inputSchema": {"type": "object", "required": ["member_id", "query"], "properties": {"member_id": {"type": "string"}, "query": {"type": "string"}, "kind": {"type": "string"}}}},
    {"name": "butian_task_list", "description": "List orchestration tasks", "inputSchema": {"type": "object", "properties": {}}},
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
    if name == "butian_task_list": return {"tasks": service.store.list_tasks()}
    if name == "butian_memory_search":
        memory = service.memory(args["member_id"])
        try: return {"memories": memory.search(args["query"], kind=args.get("kind"))}
        finally: memory.close()
    raise ValueError("unknown_tool")
