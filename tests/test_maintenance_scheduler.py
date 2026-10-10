import os

from butian.runtime.service import ButianService


def test_maintenance_tick_audits_skipped_dream_gate(tmp_path):
    old = os.environ.get("BUTIAN_MAINTENANCE_ENABLED")
    os.environ["BUTIAN_MAINTENANCE_ENABLED"] = "0"
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        result = service.maintenance_tick()
        assert result["status"] == "completed"
        assert result["external_learning"] == "not_implemented"
        assert result["members"][0]["status"] == "skipped"
        assert result["members"][0]["reason"] == "insufficient_fragments"
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
        member = result["members"][0]["memories"]
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
        assert result["members"][0]["memories"]["review_due"] == 1
        assert service.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='memory_review_due'").fetchone()[0] == 1
    finally:
        service.close()
        if old is None:
            os.environ.pop("BUTIAN_MAINTENANCE_ENABLED", None)
        else:
            os.environ["BUTIAN_MAINTENANCE_ENABLED"] = old
