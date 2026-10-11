from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from .runtime.store import Store
from .runtime.service import ButianService

def emit(data, status="ok") -> None:
    print(json.dumps({"status": status, "data": data}, ensure_ascii=False))

def home() -> Path:
    return Path(os.environ.get("BUTIAN_HOME", Path.home() / ".butian"))

def main(argv: list[str] | None = None, *, home_dir=None) -> int:
    parser = argparse.ArgumentParser(prog="butian")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=8765)
    sub.add_parser("mcp")
    member = sub.add_parser("member").add_subparsers(dest="member_command", required=True)
    add = member.add_parser("add"); add.add_argument("member_id"); add.add_argument("name"); add.add_argument("--role", default="member"); add.add_argument("--model")
    hypothesis = sub.add_parser("hypothesis").add_subparsers(dest="hypothesis_command", required=True)
    hget = hypothesis.add_parser("get"); hget.add_argument("hypothesis_id")
    hstart = hypothesis.add_parser("start"); hstart.add_argument("action_id")
    hgrant = hypothesis.add_parser("grant"); hgrant.add_argument("action_id"); hgrant.add_argument("actor_member_id"); hgrant.add_argument("reason"); hgrant.add_argument("--deny", action="store_true")
    task = sub.add_parser("task").add_subparsers(dest="task_command", required=True)
    create = task.add_parser("create"); create.add_argument("session_id"); create.add_argument("title"); create.add_argument("--assignee"); create.add_argument("--parent"); create.add_argument("--payload", default="{}")
    task.add_parser("list")
    ttree = task.add_parser("tree"); ttree.add_argument("session_id", nargs="?")
    treview = task.add_parser("review"); treview.add_argument("task_id"); treview.add_argument("revision", type=int); treview.add_argument("verdict", choices=["approve", "rework"]); treview.add_argument("--reviewer"); treview.add_argument("--notes", default=""); treview.add_argument("--rework-limit", type=int, default=3)
    experiments = sub.add_parser("experiment").add_subparsers(dest="experiment_command", required=True)
    ebatch = experiments.add_parser("batch"); ebatch.add_argument("name"); ebatch.add_argument("task_package"); ebatch.add_argument("dataset_version"); ebatch.add_argument("code_commit"); ebatch.add_argument("variants"); ebatch.add_argument("--configuration", default="{}"); ebatch.add_argument("--repeats", type=int, default=1)
    ecompare = experiments.add_parser("compare"); ecompare.add_argument("experiment_ids"); ecompare.add_argument("metrics")
    source = sub.add_parser("source").add_subparsers(dest="source_command", required=True)
    sadd = source.add_parser("add"); sadd.add_argument("source_type"); sadd.add_argument("locator"); sadd.add_argument("title"); sadd.add_argument("content_hash")
    scap = source.add_parser("capture"); scap.add_argument("url"); scap.add_argument("--title"); scap.add_argument("--source-type", default="web"); scap.add_argument("--timeout", type=float, default=10); scap.add_argument("--max-bytes", type=int, default=1000000)
    source.add_parser("runs")
    claim = sub.add_parser("claim").add_subparsers(dest="claim_command", required=True)
    cadd = claim.add_parser("add"); cadd.add_argument("source_id"); cadd.add_argument("claim"); cadd.add_argument("--confidence", type=float, default=0.0)
    evidence = sub.add_parser("evidence").add_subparsers(dest="evidence_command", required=True)
    erecord = evidence.add_parser("record"); erecord.add_argument("path")
    memory = sub.add_parser("memory").add_subparsers(dest="memory_command", required=True)
    mrecall = memory.add_parser("recall"); mrecall.add_argument("member_id"); mrecall.add_argument("query"); mrecall.add_argument("decision_id")
    moutcome = memory.add_parser("outcome"); moutcome.add_argument("member_id"); moutcome.add_argument("recall_id"); moutcome.add_argument("--used", default=""); moutcome.add_argument("--useful", default=""); moutcome.add_argument("--harmful", default=""); moutcome.add_argument("--evidence"); moutcome.add_argument("--missed", action="store_true")
    mpractice = memory.add_parser("practice"); mpractice.add_argument("member_id"); mpractice.add_argument("memory_id"); mpractice.add_argument("kind"); mpractice.add_argument("content"); mpractice.add_argument("outcome", choices=["success", "failure", "unknown"]); mpractice.add_argument("independent_key"); mpractice.add_argument("--evidence")
    evolution = sub.add_parser("evolution").add_subparsers(dest="evolution_command", required=True)
    eks = evolution.add_parser("skills"); eks.add_argument("member_id")
    edr = evolution.add_parser("dream-run"); edr.add_argument("member_id"); edr.add_argument("--min-fragments", type=int, default=1); edr.add_argument("--min-interval", type=float, default=0)
    ehist = evolution.add_parser("history"); ehist.add_argument("member_id")
    ecomp = evolution.add_parser("compose"); ecomp.add_argument("member_id"); ecomp.add_argument("result_memory_id"); ecomp.add_argument("component_ids"); ecomp.add_argument("content")
    eskill = evolution.add_parser("skill-update"); eskill.add_argument("member_id"); eskill.add_argument("domain"); eskill.add_argument("level", type=float); eskill.add_argument("marginal", type=float)
    edeg = evolution.add_parser("degrade"); edeg.add_argument("member_id"); edeg.add_argument("memory_id"); edeg.add_argument("--failures", type=int, default=0)
    session = sub.add_parser("session").add_subparsers(dest="session_command", required=True)
    screate = session.add_parser("create"); screate.add_argument("mode", choices=["solo", "team"]); screate.add_argument("--dispatcher"); screate.add_argument("--current-member"); screate.add_argument("--requested-model")
    sswitch = session.add_parser("switch"); sswitch.add_argument("session_id"); sswitch.add_argument("mode", choices=["solo", "team"]); sswitch.add_argument("--current-member")
    session.add_parser("list")
    dispatcher = sub.add_parser("dispatcher").add_subparsers(dest="dispatcher_command", required=True)
    dispatcher.add_parser("get")
    dset = dispatcher.add_parser("set"); dset.add_argument("member_id")
    issue = sub.add_parser("tool-issue").add_subparsers(dest="issue_command", required=True)
    escalations = issue.add_parser("escalations"); escalations.add_argument("member_id"); escalations.add_argument("--status")
    claim_issue = issue.add_parser("claim"); claim_issue.add_argument("member_id"); claim_issue.add_argument("work_item_id")
    resolve_issue = issue.add_parser("resolve"); resolve_issue.add_argument("member_id"); resolve_issue.add_argument("work_item_id"); resolve_issue.add_argument("status", choices=["completed", "rejected"]); resolve_issue.add_argument("--resolution", default="")
    expire_issue = issue.add_parser("expire"); expire_issue.add_argument("member_id"); expire_issue.add_argument("--now", type=float)
    migrate = sub.add_parser("migrate").add_subparsers(dest="migrate_command", required=True)
    mpreflight = migrate.add_parser("preflight"); mpreflight.add_argument("source_path"); mpreflight.add_argument("--roles-dir")
    mimport = migrate.add_parser("import"); mimport.add_argument("source_path"); mimport.add_argument("--roles-dir"); mimport.add_argument("--update-existing", action="store_true"); mimport.add_argument("--no-memories", action="store_true")
    migrate.add_parser("runs")
    args = parser.parse_args(argv)
    path = home() / "team.db"
    if args.command == "init":
        Store(path).close(); emit({"initialized": str(path)}); return 0
    if args.command == "serve":
        from .runtime.http_api import serve as run_server
        run_server(home(), args.host, args.port); return 0
    if args.command == "mcp":
        from .mcp_server import run
        run(home()); return 0
    if args.command in {"hypothesis", "source", "claim", "evidence", "memory", "experiment", "evolution", "migrate", "session", "task", "dispatcher", "tool-issue"}:
        service = ButianService(Path(home_dir) if home_dir else home())
        try:
            if args.command == "session" and args.session_command == "create":
                emit(service.create_session(args.mode, args.dispatcher, args.requested_model, None, current_member_id=args.current_member)); return 0
            if args.command == "session" and args.session_command == "switch":
                emit(service.switch_session_mode(args.session_id, args.mode, current_member_id=args.current_member)); return 0
            if args.command == "session" and args.session_command == "list":
                emit({"sessions": [dict(r) for r in service.store.db.execute("SELECT * FROM sessions ORDER BY created_at")], "dispatcher_config": service.get_dispatcher_config()}); return 0
            if args.command == "task" and args.task_command == "create":
                emit({"task_id": service.store.create_task(args.session_id, args.title, args.assignee, args.parent, json.loads(args.payload))}); return 0
            if args.command == "task" and args.task_command == "list":
                emit({"tasks": service.store.list_tasks()}); return 0
            if args.command == "task" and args.task_command == "tree":
                emit({"tree": service.task_tree(args.session_id)}); return 0
            if args.command == "task" and args.task_command == "review":
                emit(service.review_task(args.task_id, args.revision, args.verdict, reviewer_member_id=args.reviewer, notes=args.notes, rework_limit=args.rework_limit)); return 0
            if args.command == "dispatcher" and args.dispatcher_command == "get":
                emit({"dispatcher_member_id": service.get_dispatcher_config()}); return 0
            if args.command == "dispatcher" and args.dispatcher_command == "set":
                service.set_dispatcher_config(args.member_id); emit({"dispatcher_member_id": args.member_id}); return 0
            if args.command == "tool-issue":
                if args.issue_command == "escalations":
                    emit({"work_items": service.list_tool_issue_escalations(args.member_id, status=args.status)}); return 0
                if args.issue_command == "claim":
                    emit(service.claim_tool_issue_escalation(args.member_id, args.work_item_id)); return 0
                if args.issue_command == "resolve":
                    emit(service.resolve_tool_issue_escalation(args.member_id, args.work_item_id, status=args.status, resolution=args.resolution)); return 0
                if args.issue_command == "expire":
                    emit({"issues": service.expire_tool_issue_escalations(args.member_id, now=args.now)}); return 0
            if args.command == "migrate" and args.migrate_command == "preflight":
                raise ValueError("historical_identity_migration_out_of_scope")
            if args.command == "migrate" and args.migrate_command == "import":
                raise ValueError("historical_identity_migration_out_of_scope")
            if args.command == "migrate" and args.migrate_command == "runs":
                raise ValueError("historical_identity_migration_out_of_scope")
            if args.command == "hypothesis" and args.hypothesis_command == "get":
                emit(service.get_hypothesis(args.hypothesis_id)); return 0
            if args.command == "hypothesis" and args.hypothesis_command == "start":
                service.start_hypothesis_action(args.action_id); emit({"action_id": args.action_id}); return 0
            if args.command == "hypothesis" and args.hypothesis_command == "grant":
                service.grant_hypothesis_authorization(args.action_id, actor_member_id=args.actor_member_id, reason=args.reason, authorized=not args.deny); emit({"action_id": args.action_id}); return 0
            if args.command == "experiment":
                from .runtime.experiments import ExperimentStore
                experiments = ExperimentStore(home() / "experiments.db")
                try:
                    if args.experiment_command == "batch":
                        result = experiments.run_batch(args.name, json.loads(args.variants), task_package=json.loads(args.task_package), dataset_version=args.dataset_version, code_commit=args.code_commit, configuration=json.loads(args.configuration), repeats=args.repeats); emit(result); return 0
                    if args.experiment_command == "compare":
                        result = experiments.batch_compare([x for x in args.experiment_ids.split(",") if x], [x for x in args.metrics.split(",") if x]); emit(result); return 0
                finally:
                    experiments.close()
            if args.command == "source":
                if args.source_command == "capture":
                    result = service.learning.capture_url(args.url, title=args.title, source_type=args.source_type, timeout=args.timeout, max_bytes=args.max_bytes); result.pop("content", None); emit(result); return 0
                if args.source_command == "runs":
                    emit({"runs": service.learning.list_learning_runs()}); return 0
                source_id = service.learning.add_source(args.source_type, args.locator, args.title, args.content_hash); emit({"source_id": source_id}); return 0
            if args.command == "claim":
                claim_id = service.learning.add_claim(args.source_id, args.claim, confidence=args.confidence); emit({"claim_id": claim_id}); return 0
            if args.command == "evidence":
                evidence_id = service.record_evidence(args.path); emit({"evidence_id": evidence_id}); return 0
            if args.command == "memory" and args.memory_command == "recall":
                data = service.recall_for_decision(args.member_id, args.query, decision_id=args.decision_id); emit(data); return 0
            if args.command == "evolution":
                if args.evolution_command == "skills":
                    emit({"skills": service.list_skills(args.member_id), "events": service.list_skill_events(args.member_id)}); return 0
                if args.evolution_command == "dream-run":
                    emit(service.dream_run(args.member_id, min_fragments=args.min_fragments, min_interval=args.min_interval)); return 0
                if args.evolution_command == "history":
                    emit({"runs": service.list_dream_runs(args.member_id), "candidates": service.list_dream_candidates(args.member_id), "hypotheses": service.list_dream_hypotheses(args.member_id), "distillations": service.list_dream_distillations(args.member_id), "abstractions": service.list_dream_abstractions(args.member_id)}); return 0
                if args.evolution_command == "compose":
                    emit(service.compose_memories(args.member_id, args.result_memory_id, [x for x in args.component_ids.split(",") if x], content=args.content)); return 0
                if args.evolution_command == "skill-update":
                    emit(service.update_skill(args.member_id, args.domain, level=args.level, marginal=args.marginal)); return 0
                if args.evolution_command == "degrade":
                    emit(service.assess_memory_degradation(args.member_id, args.memory_id, consecutive_failures=args.failures)); return 0
            if args.command == "memory" and args.memory_command == "outcome":
                result = service.record_recall_outcome(args.member_id, args.recall_id, used_memory_ids=[x for x in args.used.split(",") if x], useful_memory_ids=[x for x in args.useful.split(",") if x], harmful_memory_ids=[x for x in args.harmful.split(",") if x], evidence_ref=args.evidence, missed=args.missed); emit({"recorded": result}); return 0
            if args.command == "memory" and args.memory_command == "practice":
                result = service.record_practice_outcome(args.member_id, memory_id=args.memory_id, kind=args.kind, content=args.content, outcome=args.outcome, independent_key=args.independent_key, evidence_ref=args.evidence); emit(result); return 0
        finally:
            service.close()
    store = Store(path)
    try:
        if args.command == "member" and args.member_command == "add":
            store.add_member(args.member_id, args.name, args.role, model_id=args.model); emit({"member_id": args.member_id}); return 0
    finally:
        store.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
