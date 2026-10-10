import pytest

from butian.runtime.memory import MemoryStore
from butian.runtime.service import ButianService
from butian.mcp_server import call_tool, TOOLS


def test_evidence_becomes_stale(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("one", encoding="utf-8")
    service = ButianService(tmp_path / "home")
    evidence_id = service.store.record_evidence(source)
    assert service.store.verify_evidence(evidence_id)["status"] == "valid"
    source.write_text("two", encoding="utf-8")
    assert service.store.verify_evidence(evidence_id)["status"] == "stale"
    service.close()


def test_mcp_tools_cover_minimum_runtime(tmp_path):
    service = ButianService(tmp_path)
    assert {"name": "butian_task_create", "description": "Create a task", "inputSchema": {"type": "object", "required": ["session_id", "title"], "properties": {"session_id": {"type": "string"}, "title": {"type": "string"}, "assignee_id": {"type": "string"}}}} in TOOLS
    session = call_tool(service, "butian_session_create", {"mode": "solo"})
    task = call_tool(service, "butian_task_create", {"session_id": session["session_id"], "title": "验证"})
    assert task["task_id"].startswith("task_")
    call_tool(service, "butian_task_transition", {"task_id": task["task_id"], "expected_revision": 1, "target": "ready", "operation_key": "mcp-1"})
    assert call_tool(service, "butian_task_list", {"status": "ready"})["tasks"][0]["status"] == "ready"
    service.close()


def test_transition_idempotency_does_not_reapply_revision(tmp_path):
    from butian.runtime.store import Store
    store = Store(tmp_path / "team.db")
    store.add_member("d", "派活者")
    session = store.create_session("team", "d", None, {})
    task = store.create_task(session, "idempotent")
    store.transition_task(task, 1, "ready", "same-op")
    store.transition_task(task, 1, "ready", "same-op")
    row = store.list_tasks()[0]
    assert row["status"] == "ready" and row["revision"] == 2
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE idempotency_key='same-op'").fetchone()[0] == 1
    store.close()


def test_memory_source_event_must_exist(tmp_path):
    from butian.runtime.memory import MemoryStore
    memory = MemoryStore(tmp_path / "memory.db")
    with pytest.raises(ValueError, match="invalid_memory_source_events"):
        memory.put("m1", "lesson", "不可伪造", source_event_ids=["fake"], source_validator=lambda ids: False)
    memory.close()

def test_memory_rejects_sensitive_content_and_cross_member_access(tmp_path):
    from butian.runtime.security import find_sensitive
    memory = MemoryStore(tmp_path / "memory.db")
    assert find_sensitive("Authorization: Bearer abcdefghijklmnop")
    with pytest.raises(ValueError, match="sensitive_memory_content_rejected"):
        memory.put("m-secret", "lesson", "password=topsecret")
    memory.close()

    service = ButianService(tmp_path / "service")
    service.add_member("a", "甲")
    service.add_member("b", "乙")
    with pytest.raises(ValueError, match="member_memory_access_denied"):
        service.memory("b", actor_member_id="a")
    service.memory("a", actor_member_id="a").close()
    service.close()

def test_memory_versions_layers_and_rewarm(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    memory.put("m1", "lesson", "版本一", status="warm", source_event_ids=["evt"])
    memory.put("m1", "lesson", "版本二", status="warm", source_event_ids=["evt"], change_reason="实测修订")
    assert memory.db.execute("SELECT COUNT(*) FROM memory_versions WHERE memory_id='m1'").fetchone()[0] == 2
    assert memory.search("版本二")[0]["content"] == "版本二"
    assert memory.cool("m1")
    assert memory.search("版本二")[0]["status"] == "cold"
    assert memory.rewarm("m1", success=True, evidence_ref="evt:rewarm") == "warm"
    memory.close()

def test_dream_gates_and_candidate_audit(tmp_path):
    from butian.runtime.dream import DreamRunner
    memory = MemoryStore(tmp_path / "memory.db")
    dream = DreamRunner(memory)
    assert dream.run(min_fragments=2)["reason"] == "insufficient_fragments"
    memory.put("a", "lesson", "片段一")
    memory.put("b", "lesson", "片段二")
    result = dream.run(min_fragments=2, min_interval=100)
    assert result["status"] == "completed"
    assert memory.db.execute("SELECT status FROM dream_candidates").fetchone()[0] == "candidate"
    assert dream.run(min_fragments=2, min_interval=100)["reason"] == "interval_not_elapsed"
    memory.close()

def test_import_nvwa_agent_assets_is_read_only_and_idempotent(tmp_path):
    import sqlite3
    from butian.runtime.migration import MemberAssetMigrator
    source = tmp_path / "nvwa-memory.db"
    con = sqlite3.connect(source)
    con.executescript("CREATE TABLE member(id TEXT PRIMARY KEY,kind TEXT,name TEXT,role TEXT,duties TEXT,provider TEXT,model TEXT,created_at REAL); CREATE TABLE member_trait(id INTEGER,member_id TEXT,trait TEXT,value TEXT,intensity REAL,source TEXT); CREATE TABLE member_domain(id INTEGER,member_id TEXT,domain TEXT,level REAL,evidence TEXT);")
    con.execute("INSERT INTO member VALUES ('src-1','agent','数字人甲','工程师','负责验证','p','m',1)")
    con.execute("INSERT INTO member_trait VALUES (1,'src-1','严谨',NULL,0.9,'user')")
    con.execute("INSERT INTO member_domain VALUES (1,'src-1','测试',0.8,'配置')")
    con.commit(); con.close()
    service = ButianService(tmp_path / "home")
    migrator = MemberAssetMigrator(service)
    first = migrator.import_agents(source)
    second = migrator.import_agents(source)
    assert first['imported'] == 1 and second['skipped'] == 1
    assert service.store.db.execute("SELECT name FROM members WHERE member_id='src-1'").fetchone()[0] == '数字人甲'
    service.close()

def test_composition_graph_deduplicates_and_tracks_skill_phase(tmp_path):
    from butian.runtime.memory import MemoryStore
    from butian.core.experience import SkillPhase
    memory = MemoryStore(tmp_path / "memory.db")
    memory.put("a", "lesson", "原子一", weight=0.4)
    memory.put("b", "lesson", "原子二", weight=0.3)
    result = memory.compose("c", ["a", "b"], content="复合经验")
    assert result["gain"] == pytest.approx(0.7)
    with pytest.raises(ValueError, match="composition_already_exists"):
        memory.compose("d", ["b", "a"], content="重复复合")
    with pytest.raises(ValueError, match="components_must_be_unique"):
        memory.compose("e", ["a", "a"], content="重复组件")
    assert memory.update_skill("测试", level=.7, marginal=.01).phase == SkillPhase.DEVELOPING
    assert memory.update_skill("测试", level=.7, marginal=.01).phase == SkillPhase.STABLE
    assert memory.update_skill("测试", level=.7, marginal=.01, novel_environment=True).phase == SkillPhase.ACTIVATING
    memory.close()

def test_memory_versions_form_unbounded_evidence_linked_chain(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    parent = None
    for index in range(1, 105):
        parent = memory.put(
            "m-chain", "lesson", f"迭代步骤 {index}",
            environment={"runtime": "test", "revision": index},
            source_event_ids=[f"evt:{index}"], status="candidate",
            parent_version_id=parent, change_reason=f"验证修订 {index}",
        )
    versions = memory.list_versions("m-chain")
    assert len(versions) == 104
    assert [item["version"] for item in versions] == list(range(1, 105))
    assert versions[-1]["parent_version_id"] == versions[-2]["version_id"]
    assert memory.get("m-chain")["content"] == "迭代步骤 104"
    memory.audit_version_chain("m-chain")
    memory.close()

def test_lesson_recall_is_explicit_audited_and_searches_both_memory_types(tmp_path):
    service = ButianService(tmp_path / "home")
    service.add_member("m", "工程师")
    memory = service.memory("m")
    memory.put("lesson", "lesson", "不要把未知依赖版本当成兼容", status="candidate", environment={"os": "x"})
    memory.put("experience", "experience", "未知依赖版本必须先实测", status="candidate", environment={"os": "x"})
    memory.close()
    packet = service.recall_for_decision("m", "未知依赖版本兼容性", decision_id="task:1:plan", environment={"os": "x"})
    assert {item["kind"] for item in packet["memories"]} == {"lesson", "experience"}
    assert packet["decision_id"] == "task:1:plan"
    assert service.record_recall_outcome("m", packet["recall_id"], used_memory_ids=[], missed=True) is True
    assert service.recall_audit("m")[0]["missed"] == 1
    service.close()


def test_current_memory_is_overwritten_but_history_is_auditable(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    memory.put("m", "experience", "旧方法", source_event_ids=["e1"])
    memory.put("m", "experience", "新方法", source_event_ids=["e2"], change_reason="实测改进")
    assert memory.get("m")["content"] == "新方法"
    assert [row["content"] for row in memory.list_versions("m")] == ["旧方法", "新方法"]
    memory.audit_version_chain("m")
    memory.close()


def test_hardware_hypothesis_waits_for_batch_validation(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    memory.put("pcb", "practice_hypothesis", "该电源布局应稳定", knowledge_source="model", validation_state="pending_prototype")
    assert memory.get("pcb")["status"] == "pending_validation"
    batch = memory.create_validation_batch("pcb", sample_count=3, notes="样机回来后批量测试")
    memory.update_validation_batch(batch, "prototype_received")
    memory.update_validation_batch(batch, "batch_validating")
    with pytest.raises(ValueError, match="validated_requires_complete_batch_evidence"):
        memory.update_validation_batch(batch, "validated", pass_count=2, evidence_refs=["lab:1"])
    result = memory.update_validation_batch(batch, "validated", pass_count=3, evidence_refs=["lab:1", "lab:2", "lab:3"])
    assert result["state"] == "validated"
    assert memory.get("pcb")["status"] == "warm"
    memory.close()


def test_dream_hypothesis_argument_and_verification_drive(tmp_path):
    from butian.runtime.dream import DreamRunner
    memory = MemoryStore(tmp_path / "memory.db")
    dream = DreamRunner(memory)
    for index in range(2):
        memory.put(f"f{index}", "lesson", f"历史经验 {index}")
    result = dream.run(min_fragments=2, min_interval=0)
    hypothesis_id = dream.propose_hypothesis(result["candidate_id"], "布局假设成立", prior_reliability=.6, argument="两条相似经验支持", evidence_refs=["memory:f0"])
    pending = dream.schedule_verification(hypothesis_id, "样机批量测试", cost=2, risk=.2, expected_information_gain=.8)
    assert pending["status"] == "awaiting_authorization"
    queued = dream.schedule_verification(hypothesis_id, "低风险仿真", cost=1, risk=.1, expected_information_gain=.4, authorized=True)
    assert queued["status"] == "queued" and dream.verification_queue()
    with pytest.raises(ValueError, match="verification_evidence_required"):
        dream.record_verification(hypothesis_id, outcome="supported", evidence_refs=[])
    outcome = dream.record_verification(hypothesis_id, outcome="supported", evidence_refs=["lab:1"], new_memory_id="new", new_content="验证后的经验")
    assert outcome["status"] == "supported"
    assert memory.get("new")["content"] == "验证后的经验"
    memory.close()
