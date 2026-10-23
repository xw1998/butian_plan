import os

from butian.runtime.service import ButianService


def test_maintenance_tick_audits_without_dreaming_for_members(tmp_path):
    old = os.environ.get("BUTIAN_MAINTENANCE_ENABLED")
    os.environ["BUTIAN_MAINTENANCE_ENABLED"] = "0"
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        result = service.maintenance_tick()
        assert result["status"] == "completed"
        assert result["auto_dream"] is False
        assert result["external_learning"] == "authorized_url_queue"
        assert result["external_learning_result"]["claims_created"] == 0
        assert result["external_learning_result"]["memories_created"] == 0
        member = next(item for item in result["members"] if item["member_id"] == "m")
        assert member["status"] == "not_requested"
        assert member["reason"] == "auto_dream_disabled"
        assert all(item["dream"]["status"] == "not_requested" for item in result["members"])
        assert service.list_dream_runs("m") == []
        event = service.store.db.execute("SELECT event_type FROM events WHERE event_type='maintenance_tick'").fetchone()
        assert event["event_type"] == "maintenance_tick"
    finally:
        service.close()
        if old is None:
            os.environ.pop("BUTIAN_MAINTENANCE_ENABLED", None)
        else:
            os.environ["BUTIAN_MAINTENANCE_ENABLED"] = old


def test_scheduler_is_enabled_by_default_and_close_stops_thread(tmp_path):
    old_enabled = os.environ.pop("BUTIAN_MAINTENANCE_ENABLED", None)
    old_interval = os.environ.get("BUTIAN_MAINTENANCE_INTERVAL")
    os.environ["BUTIAN_MAINTENANCE_INTERVAL"] = "60"
    service = ButianService(tmp_path)
    thread = service._maintenance_thread
    try:
        assert service._maintenance_enabled is True
        assert thread is not None and thread.is_alive()
    finally:
        service.close()
        if old_enabled is not None:
            os.environ["BUTIAN_MAINTENANCE_ENABLED"] = old_enabled
        if old_interval is None:
            os.environ.pop("BUTIAN_MAINTENANCE_INTERVAL", None)
        else:
            os.environ["BUTIAN_MAINTENANCE_INTERVAL"] = old_interval
    assert not thread.is_alive()


def test_maintenance_scans_decay_and_schedules_cold_review(tmp_path):
    old = os.environ.get("BUTIAN_MAINTENANCE_ENABLED")
    os.environ["BUTIAN_MAINTENANCE_ENABLED"] = "0"
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        memory = service.memory("m")
        try:
            memory.put("old", "lesson", "old lesson", confidence=0.9, weight=1.0, status="warm", source_event_ids=["seed"])
            memory.db.execute("UPDATE memories SET created_at=?, updated_at=? WHERE memory_id=?", (0.0, 0.0, "old"))
        finally:
            memory.close()
        result = service.maintenance_tick()
        member = next(item["memories"] for item in result["members"] if item["member_id"] == "m")
        assert member["assessed"] == 1
        assert member["cooled"] == 1
        assert member["rewarmed"] == 0
        assert member["rewarm_policy"] == "evidence_required"
        memory = service.memory("m")
        try:
            assert memory.get("old")["status"] == "cold"
        finally:
            memory.close()
        result = service.maintenance_tick()
        member_result = next(item for item in result["members"] if item["member_id"] == "m")
        assert member_result["memories"]["review_due"] == 1
        assert service.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='memory_review_due'").fetchone()[0] == 1
    finally:
        service.close()
        if old is None:
            os.environ.pop("BUTIAN_MAINTENANCE_ENABLED", None)
        else:
            os.environ["BUTIAN_MAINTENANCE_ENABLED"] = old


def test_issue_escalation_creates_dispatcher_work_item_lifecycle(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("owner", "Owner")
        service.add_member("dispatcher", "Dispatcher")
        service.set_dispatcher_config("dispatcher")
        tool_id = service.propose_tool("owner", "tool", "desc", reuse_lower=1, manual_cost_upper=1, call_cost_lower=1, build_cost_upper=1, maintenance_cost_upper=1, risk_cost_upper=1, manifest={"permissions": ["execute"], "entrypoint": ["python", "tool.py"]})["tool_id"]
        service.add_tool_version("owner", tool_id, "print('ok')", {"input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "failure_conditions": ["timeout"], "entrypoint": ["python", "tool.py"]})
        issue = service.report_tool_issue("owner", tool_id, 1, description="timeout")
        item = service.expire_tool_issue_escalations("owner", now=issue["due_at"] + 1)[0]
        assert item["work_item_id"]
        work_items = service.list_tool_issue_escalations("dispatcher", status="open")
        assert len(work_items) == 1
        claimed = service.claim_tool_issue_escalation("dispatcher", item["work_item_id"])
        assert claimed["status"] == "claimed"
        completed = service.resolve_tool_issue_escalation("dispatcher", item["work_item_id"], status="completed", resolution="reviewed")
        assert completed["status"] == "completed"
        assert completed["issue_status"] == "open"
        assert completed["issue_resolution_required"] is True
        assert service.store.db.execute("SELECT status FROM tool_issues WHERE issue_id=?", (issue["issue_id"],)).fetchone()["status"] == "open"
        events = service.store.db.execute("SELECT event_type FROM tool_issue_escalation_events WHERE work_item_id=? ORDER BY rowid", (item["work_item_id"],)).fetchall()
        assert [row["event_type"] for row in events] == ["created", "claimed", "completed"]
        second = service.tools.escalate_issue("owner", issue["issue_id"], reason="manual-retry", now=issue["due_at"] + 10)
        service.claim_tool_issue_escalation("dispatcher", second["work_item_id"])
        rejected = service.resolve_tool_issue_escalation("dispatcher", second["work_item_id"], status="rejected", resolution="outside responsibility")
        assert rejected["next_work_item_id"]
        next_item = service.list_tool_issue_escalations("dispatcher", status="open")
        assert len(next_item) == 1
        assert next_item[0]["work_item_id"] == rejected["next_work_item_id"]
        rejection_events = service.store.db.execute("SELECT event_type FROM tool_issue_escalation_events WHERE work_item_id=? ORDER BY rowid", (second["work_item_id"],)).fetchall()
        assert [row["event_type"] for row in rejection_events] == ["created", "claimed", "rejected", "re-escalated"]
    finally:
        service.close()

def test_issue_escalation_rejection_chain_creates_distinct_work_items(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("owner", "Owner")
        service.add_member("dispatcher", "Dispatcher")
        service.set_dispatcher_config("dispatcher")
        tool_id = service.propose_tool("owner", "tool", "desc", reuse_lower=1, manual_cost_upper=1, call_cost_lower=1, build_cost_upper=1, maintenance_cost_upper=1, risk_cost_upper=1, manifest={"permissions": ["execute"], "entrypoint": ["python", "tool.py"]})["tool_id"]
        service.add_tool_version("owner", tool_id, "print('ok')", {"input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "permissions": ["execute"], "failure_conditions": ["timeout"], "entrypoint": ["python", "tool.py"]})
        issue = service.report_tool_issue("owner", tool_id, 1, description="timeout")
        item = service.tools.escalate_issue("owner", issue["issue_id"], reason="manual", now=100.0)
        work_ids = []
        for index in range(5):
            work_id = item["work_item_id"]
            work_ids.append(work_id)
            service.claim_tool_issue_escalation("dispatcher", work_id)
            rejected = service.resolve_tool_issue_escalation("dispatcher", work_id, status="rejected", resolution=f"reject-{index}")
            item = {"work_item_id": rejected["next_work_item_id"]}
        assert len(set(work_ids)) == 5
        rows = service.list_tool_issue_escalations("dispatcher")
        assert len(rows) == 6
        assert [row["status"] for row in rows] == ["rejected"] * 5 + ["open"]
    finally:
        service.close()
