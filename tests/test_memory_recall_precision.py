"""召回精度（实体闸门 + IDF 排序）与记忆内容修订通道。

这两条是 host-loop-v2 实验暴露出来的短板：
1. `butian_memory_recall` 几乎把全部记忆都端回来，宿主只能自己按实体名过滤；
2. 被证据证伪的记忆只能把状态打到 disputed，内容一个字都改不了，坏经验永久卡住。
"""

import pytest

from butian.mcp_server import TOOLS, call_tool
from butian.runtime.memory import MemoryStore
from butian.runtime.service import ButianService


def registered_proof(service, member, label):
    path = service.home / (label + ".txt")
    path.write_text("execution result: " + label, encoding="utf-8")
    return service.record_evidence(path, owner_member_id=member, actor_member_id=member)


def seed_entities(store, count=10, skip=None):
    """造 count 条同族记忆（只差实体名），标号 skip 的那条是被问到的实体。"""
    for index in range(count):
        store.put(f"calibration{index:03d}-v1", "calibration-spec", f"calibration{index:03d}-v1 校准系数 offset={index * 2}", status="candidate")


def test_recall_gates_on_rare_entity_token(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        seed_entities(store, skip=4)
        items = store.recall_for_decision("calibration004 校准系数 offset", limit=100)
        assert [item["memory_id"] for item in items] == ["calibration004-v1"], [item["memory_id"] for item in items]
        assert items[0]["gated_by"] == ["calibration004"]
        assert items[0]["match_score"] == 1.0
        # 没有闸门时（查询里没有库内稀有词）行为与老版本一致：同族记忆全部返回，只是按相关度排序
        loose = store.recall_for_decision("校准系数 offset", limit=100)
        assert len(loose) == 10
        assert [item["match_score"] for item in loose] == sorted((item["match_score"] for item in loose), reverse=True)
    finally:
        store.close()


def test_recall_falls_back_when_entity_is_unknown(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        seed_entities(store, skip=4)
        # 问的是一个库里根本没出现过的实体：闸门命中 0 条，退回或链给出同族经验，而不是返回空
        items = store.recall_for_decision("calibration999 校准系数 offset", limit=100)
        assert len(items) == 10
        assert all(item["gated_by"] == [] for item in items)
    finally:
        store.close()


def test_recall_structured_filters(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    try:
        store.put("lesson-1", "lesson", "校准系数 必须实测", status="candidate")
        store.put("experience-1", "experience", "校准系数 必须实测并留证据", status="disputed")
        assert [item["memory_id"] for item in store.recall_for_decision("校准系数", kind="lesson")] == ["lesson-1"]
        assert [item["memory_id"] for item in store.recall_for_decision("校准系数", statuses=["disputed"])] == ["experience-1"]
        assert [item["memory_id"] for item in store.recall_for_decision("校准系数", kind="experience", statuses=["candidate"])] == []
    finally:
        store.close()


def test_service_recall_reports_match_evidence(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "工程师")
        memory = service.memory("m")
        seed_entities(memory, skip=4)
        memory.close()
        packet = service.recall_for_decision("m", "calibration004 校准系数 offset", decision_id="d-1")
        assert [item["memory_id"] for item in packet["memories"]] == ["calibration004-v1"]
        item = packet["memories"][0]
        assert item["gated_by"] == ["calibration004"] and item["matched_tokens"] == ["calibration004", "校准系数", "offset"]
        assert item["needs_revalidation"] is True
        assert service.record_recall_outcome("m", packet["recall_id"], used_memory_ids=["calibration004-v1"], missed=False) is True
    finally:
        service.close()


def test_revision_rewrites_content_and_supersedes_old_version(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "工程师")
        memory = service.memory("m")
        memory.put("cali-4", "calibration-spec", "calibration004-v1 校准系数 offset=37", status="candidate")
        memory.close()
        service.record_practice_outcome("m", memory_id="cali-4", kind="calibration-spec", content="calibration004-v1 校准系数 offset=37", outcome="success", independent_key="run-a", evidence_ref=registered_proof(service, "m", "good-a"))
        service.record_practice_outcome("m", memory_id="cali-4", kind="calibration-spec", content="calibration004-v1 校准系数 offset=37", outcome="success", independent_key="run-b", evidence_ref=registered_proof(service, "m", "good-b"))
        broken = service.record_practice_outcome("m", memory_id="cali-4", kind="calibration-spec", content="calibration004-v1 校准系数 offset=37", outcome="failure", independent_key="run-c", evidence_ref=registered_proof(service, "m", "bad-c"))
        assert broken["status"] == "disputed" and broken["failures"] == 1
        # 内容改不动是旧缺陷：再攒三次成功仍然是 disputed，且内容还是错值
        for index, label in enumerate(("good-d", "good-e", "good-f")):
            stale = service.record_practice_outcome("m", memory_id="cali-4", kind="calibration-spec", content="calibration004-v1 校准系数 offset=37", outcome="success", independent_key=f"run-{index}", evidence_ref=registered_proof(service, "m", label))
        assert stale["status"] == "disputed" and stale["failures"] == 1

        # 每次 practice 观测也会落一版（内容不变、只改状态），所以修订版号 = 现有版数 + 1
        memory = service.memory("m")
        try:
            versions_before = len(memory.list_versions("cali-4"))
        finally:
            memory.close()
        revised = service.revise_memory("m", "cali-4", content="calibration004-v1 校准系数 offset=0", reason="实测为空高零点，offset 应为 0", evidence_refs=[registered_proof(service, "m", "measure-g")])
        assert revised["version"] == versions_before + 1 and revised["status"] == "candidate"
        assert revised["previous_status"] == "disputed"
        memory = service.memory("m")
        try:
            assert memory.get("cali-4")["content"] == "calibration004-v1 校准系数 offset=0"
            versions = memory.list_versions("cali-4")
            assert [item["version"] for item in versions] == list(range(1, versions_before + 2))
            assert all(item["version_status"] == "superseded" for item in versions[:-1])
            assert versions[-1]["change_reason"].startswith("revision:")
            memory.audit_version_chain("cali-4")
            events = list(memory.db.execute("SELECT payload FROM memory_events WHERE memory_id='cali-4' AND event_type='content_revision'"))
            assert len(events) == 1
        finally:
            memory.close()
        # 修订后计数从修订时刻重新起算：两次独立成功就能回到 warm（不修就永远被一条失败压死）
        first = service.record_practice_outcome("m", memory_id="cali-4", kind="calibration-spec", content="calibration004-v1 校准系数 offset=0", outcome="success", independent_key="after-a", evidence_ref=registered_proof(service, "m", "after-a"))
        assert first["failures"] == 0
        second = service.record_practice_outcome("m", memory_id="cali-4", kind="calibration-spec", content="calibration004-v1 校准系数 offset=0", outcome="success", independent_key="after-b", evidence_ref=registered_proof(service, "m", "after-b"))
        assert second["status"] == "warm" and second["successes"] == 2
    finally:
        service.close()


def test_revision_requires_independent_evidence_and_fresh_hash(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "工程师")
        memory = service.memory("m")
        memory.put("cali-5", "calibration-spec", "calibration005-v1 校准系数 offset=23", status="candidate")
        old_hash = __import__("hashlib").sha256("calibration005-v1 校准系数 offset=23".encode()).hexdigest()
        memory.close()
        used = registered_proof(service, "m", "used-once")
        service.record_practice_outcome("m", memory_id="cali-5", kind="calibration-spec", content="calibration005-v1 校准系数 offset=23", outcome="failure", independent_key="run-a", evidence_ref=used)
        with pytest.raises(ValueError, match="revision_evidence_must_be_independent"):
            service.revise_memory("m", "cali-5", content="calibration005-v1 校准系数 offset=26", reason="修正", evidence_refs=[used])
        with pytest.raises(ValueError, match="revision_no_change"):
            service.revise_memory("m", "cali-5", content="calibration005-v1 校准系数 offset=23", reason="无改动", evidence_refs=[registered_proof(service, "m", "fresh-a")])
        with pytest.raises(ValueError, match="revision_reason_required"):
            service.revise_memory("m", "cali-5", content="calibration005-v1 校准系数 offset=26", reason="  ", evidence_refs=[registered_proof(service, "m", "fresh-b")])
        with pytest.raises(ValueError, match="memory_review_content_changed"):
            service.revise_memory("m", "cali-5", content="calibration005-v1 校准系数 offset=26", reason="修正", evidence_refs=[registered_proof(service, "m", "fresh-c")], expected_content_hash=old_hash.replace("a", "b"))
        assert service.revise_memory("m", "cali-5", content="calibration005-v1 校准系数 offset=26", reason="修正", evidence_refs=[registered_proof(service, "m", "fresh-d")], expected_content_hash=old_hash)["status"] == "candidate"
        with pytest.raises(ValueError, match="memory_not_found"):
            service.revise_memory("m", "missing", content="x", reason="修正", evidence_refs=[registered_proof(service, "m", "fresh-e")])
    finally:
        service.close()


def test_mcp_memory_revise_tool_is_registered_and_callable(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        service.store.set_member_token("m", "secret")
        assert any(tool["name"] == "butian_memory_revise" for tool in TOOLS)
        import json
        json.dumps(TOOLS)
        memory = service.memory("m")
        memory.put("m-1", "lesson", "旧结论", status="disputed")
        memory.close()
        result = call_tool(service, "butian_memory_revise", {"member_id": "m", "memory_id": "m-1", "content": "新结论", "reason": "补测推翻旧结论", "evidence_refs": [registered_proof(service, "m", "mcp-revise")], "actor_member_id": "m", "token": "secret"})
        assert result["status"] == "candidate" and result["version"] == 2
        memory = service.memory("m")
        try:
            assert memory.get("m-1")["content"] == "新结论"
        finally:
            memory.close()
    finally:
        service.close()
