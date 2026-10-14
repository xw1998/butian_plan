"""数字人自由进化与发展能力的定向测试（标准库可独立运行）。

覆盖：记忆合成、技能阶段演化、退化评估/降温/复温、版本链审计、
梦境自省（run/distill/abstract/propose/schedule/verify）、验证队列，
以及 HTTP 路由与 MCP 工具入口的连通性。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from urllib import request as urlrequest

from butian.runtime.service import ButianService


def _seed(service: ButianService):
    service.add_member("m1", "成员一")
    service.store.set_member_token("m1", "secret")
    memory = service.memory("m1")
    memory.put("mem_a", "experience", "经验 A：先校准再测量", status="warm", source_event_ids=["ev_a"])
    memory.put("mem_b", "experience", "经验 B：环境漂移需复检", status="warm", source_event_ids=["ev_b"])
    memory.close()


def test_evolution_memory_and_skill(tmp_path):
    service = ButianService(tmp_path)
    try:
        _seed(service)
        composed = service.compose_memories("m1", "mem_ab", ["mem_a", "mem_b"], content="合成：先校准再测量，并复检漂移")
        assert composed["gain"] > 0 and composed["memory_id"] == "mem_ab"

        state = service.update_skill("m1", "测量", level=0.3, marginal=0.1)
        assert state["phase"] == "developing" and state["level"] == 0.3
        assert service.list_skills("m1")[0]["domain"] == "测量"

        assessment = service.assess_memory_degradation("m1", "mem_a", consecutive_failures=1)
        assert assessment["state"] in {"cooling", "degraded", "fresh", "disputed"}
        assert "consecutive_failures" in assessment["reasons"]

        assert service.cool_memory("m1", "mem_a")["cooled"] is True
        assert service.rewarm_memory("m1", "mem_a", success=True, evidence_ref="ev_x")["status"] == "warm"

        versions = service.memory_versions("m1", "mem_a")
        assert versions["chain"]["valid"] is True and versions["versions"]

        batch = service.create_validation_batch("m1", "mem_ab", sample_count=3)
        assert batch["batch_id"].startswith("batch_")
        updated = service.update_validation_batch("m1", batch["batch_id"], "prototype_received")
        assert updated["state"] == "prototype_received"
        assert service.list_validation_batches("m1", "mem_ab")["batches"]
    finally:
        service.close()


def test_evolution_dream_cycle(tmp_path):
    service = ButianService(tmp_path)
    try:
        _seed(service)
        run = service.dream_run("m1", min_fragments=1, min_interval=0, budget=10)
        assert run["status"] == "completed" and run["candidate_id"]
        candidates = service.list_dream_candidates("m1")
        assert len(candidates) == 1

        distillation = service.dream_distill("m1", ["mem_a", "mem_b"], summary="两条经验的摘要")
        assert distillation["reversible"] == 1 and distillation["information_loss"] == 0.0

        abstraction = service.dream_abstract("m1", ["mem_a", "mem_b"], pattern="先校准再测量")
        assert abstraction["abstraction_level"] == 1

        proposed = service.dream_propose_hypothesis(
            "m1", candidates[0]["candidate_id"], "假设：先校准能降低漂移误差",
            prior_reliability=0.4, argument="来自合成记忆",
        )
        hypothesis_id = proposed["hypothesis_id"]

        schedule = service.dream_schedule_verification(
            "m1", hypothesis_id, "在两个环境复测", cost=2, risk=0.2, expected_information_gain=5,
        )
        assert schedule["status"] == "awaiting_authorization" and schedule["priority"] > 0

        authorized = service.dream_schedule_verification(
            "m1", hypothesis_id, "授权后进入队列", cost=1, risk=0.1, expected_information_gain=3,
            authorized=True,
        )
        assert authorized["status"] == "queued"
        assert len(service.dream_verification_queue("m1")["queue"]) == 1

        # 无独立证据只能记 unknown，不得伪造 supported
        recorded = service.dream_record_verification("m1", hypothesis_id, outcome="unknown", evidence_refs=[])
        assert recorded["status"] == "unknown"

        assert service.list_dream_runs("m1")
        assert service.list_dream_hypotheses("m1")[0]["status"] == "unknown"
        assert service.list_dream_distillations("m1")
        assert service.list_dream_abstractions("m1")
        assert len(service.dream_verification_queue("m1")["queue"]) == 1
    finally:
        service.close()


def test_evolution_http_routes(tmp_path):
    from butian.runtime import http_api

    service = ButianService(tmp_path)
    _seed(service)
    http_api.ApiHandler.service = service
    http_api.ApiHandler.experiments = None
    http_api.ApiHandler.web_root = Path(__file__).resolve().parents[1] / "web"
    server = http_api.ThreadingHTTPServer(("127.0.0.1", 0), http_api.ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def _read(path, data=None):
            req = urlrequest.Request(base + path, data=data, headers={"Content-Type": "application/json", "X-Butian-Member": "m1", "X-Butian-Token": "secret"}, method="POST" if data is not None else "GET")
            try:
                with urlrequest.urlopen(req) as resp:
                    return json.loads(resp.read())
            except urlrequest.HTTPError as exc:
                return json.loads(exc.read())

        def post(path, body):
            return _read(path, json.dumps(body).encode())

        def get(path):
            return _read(path)

        # 尚无任何梦境记录时也必须可读（懒建表的回归保护）
        empty = get("/api/evolution?member_id=m1")
        assert empty["status"] == "ok" and empty["data"]["dream_runs"] == []

        composed = post("/api/evolution/compose", {"member_id": "m1", "result_memory_id": "mem_ab", "component_memory_ids": ["mem_a", "mem_b"], "content": "合成内容"})
        assert composed["status"] == "ok" and composed["data"]["gain"] > 0

        skill = post("/api/evolution/skill/update", {"member_id": "m1", "domain": "测量", "level": 0.4, "marginal": 0.2})
        assert skill["data"]["phase"] == "developing"

        run = post("/api/evolution/dream/run", {"member_id": "m1", "min_fragments": 1, "min_interval": 0})
        assert run["data"]["status"] == "completed"

        overview = get("/api/evolution?member_id=m1")
        assert overview["status"] == "ok"
        assert overview["data"]["skills"] and overview["data"]["dream_runs"]
        assert "dream_candidates" in overview["data"]

        missing = get("/api/evolution")
        assert missing["status"] == "error"
    finally:
        server.shutdown()
        server.server_close()
        service.close()


def test_evolution_mcp_tools(tmp_path):
    from butian import mcp_server

    service = ButianService(tmp_path)
    try:
        _seed(service)
        service.store.set_member_token("m1", "secret")
        auth = {"actor_member_id": "m1", "token": "secret"}
        tool_names = {tool["name"] for tool in mcp_server.TOOLS}
        for name in ("butian_evolution_compose", "butian_evolution_skills", "butian_dream_run", "butian_dream_history"):
            assert name in tool_names

        composed = mcp_server.call_tool(service, "butian_evolution_compose", {**auth, "member_id": "m1", "result_memory_id": "mem_ab", "component_memory_ids": ["mem_a", "mem_b"], "content": "合成内容"})
        assert composed["gain"] > 0
        skills = mcp_server.call_tool(service, "butian_evolution_skills", {**auth, "member_id": "m1"})
        assert set(skills) == {"skills", "events"}
        run = mcp_server.call_tool(service, "butian_dream_run", {**auth, "member_id": "m1", "min_fragments": 1, "min_interval": 0})
        assert run["status"] == "completed"
        history = mcp_server.call_tool(service, "butian_dream_history", {**auth, "member_id": "m1"})
        assert history["runs"] and history["candidates"]
    finally:
        service.close()


def _run_all():
    tests = [
        test_evolution_memory_and_skill,
        test_evolution_dream_cycle,
        test_evolution_http_routes,
        test_evolution_mcp_tools,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as tmp:
            test(Path(tmp))
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _run_all()

import pytest

@pytest.mark.parametrize('metrics', [{'cost': float('nan')}, {'cost': float('inf')}, {'risk': True}, {'expected_information_gain': float('inf')}, {'authorized': 1}, {'authorized': 'false'}])
def test_dream_schedule_rejects_invalid_authorization_metrics(tmp_path, metrics):
    service = ButianService(tmp_path)
    try:
        _seed(service)
        run = service.dream_run('m1', min_fragments=1, min_interval=0)
        hypothesis = service.dream_propose_hypothesis('m1', run['candidate_id'], 'bounded claim', prior_reliability=0.4, argument='candidate')['hypothesis_id']
        args = dict(cost=1, risk=0.1, expected_information_gain=1, authorized=False)
        args.update(metrics)
        with pytest.raises(ValueError, match='invalid_verification_action_metrics'):
            service.dream_schedule_verification('m1', hypothesis, 'bounded check', **args)
        memory = service.memory('m1')
        try:
            assert memory.db.execute('SELECT COUNT(*) FROM verification_actions').fetchone()[0] == 0
        finally:
            memory.close()
    finally:
        service.close()


def test_dream_schedule_queue_failure_rolls_back_action(tmp_path):
    import sqlite3
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        service.add_member('m1', 'Worker')
        memory = service.memory('m1')
        try:
            from butian.runtime.dream import DreamRunner
            DreamRunner(memory)
            memory.db.execute("INSERT INTO hypotheses(hypothesis_id,candidate_id,statement,prior_reliability,argument,status,created_at,updated_at) VALUES ('atomic-hyp','candidate','bounded',0.2,'bounded','candidate',1,1)")
            memory.db.execute("CREATE TRIGGER reject_queue BEFORE INSERT ON verification_queue BEGIN SELECT RAISE(ABORT,'queue unavailable'); END")
        finally:
            memory.close()
        with pytest.raises(sqlite3.IntegrityError, match='queue unavailable'):
            service.dream_schedule_verification('m1', 'atomic-hyp', 'bounded check', cost=1, risk=0, expected_information_gain=1, authorized=True)
        memory = service.memory('m1')
        try:
            assert memory.db.execute('SELECT COUNT(*) FROM verification_actions').fetchone()[0] == 0
        finally:
            memory.close()
    finally:
        service.close()
