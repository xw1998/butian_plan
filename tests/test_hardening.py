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
    assert memory.rewarm("m1", success=True) == "warm"
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
