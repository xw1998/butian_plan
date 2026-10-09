from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from .runtime.store import Store

def home() -> Path:
    return Path(os.environ.get("BUTIAN_HOME", Path.home() / ".butian"))

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="butian")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=8765)
    sub.add_parser("mcp")
    member = sub.add_parser("member").add_subparsers(dest="member_command", required=True)
    add = member.add_parser("add"); add.add_argument("member_id"); add.add_argument("name"); add.add_argument("--role", default="member"); add.add_argument("--model")
    task = sub.add_parser("task").add_subparsers(dest="task_command", required=True)
    create = task.add_parser("create"); create.add_argument("session_id"); create.add_argument("title"); create.add_argument("--assignee")
    task.add_parser("list")
    args = parser.parse_args(argv)
    path = home() / "team.db"
    if args.command == "init":
        Store(path).close(); print(f"initialized {path}"); return 0
    if args.command == "serve":
        from .runtime.http_api import serve as run_server
        run_server(home(), args.host, args.port); return 0
    if args.command == "mcp":
        from .mcp_server import run
        run(home()); return 0
    store = Store(path)
    try:
        if args.command == "member" and args.member_command == "add":
            store.add_member(args.member_id, args.name, args.role, model_id=args.model); print(args.member_id); return 0
        if args.command == "task" and args.task_command == "create":
            print(store.create_task(args.session_id, args.title, args.assignee)); return 0
        if args.command == "task" and args.task_command == "list":
            print(json.dumps(store.list_tasks(), ensure_ascii=False)); return 0
    finally:
        store.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
