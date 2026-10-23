"""自省归属：谁被触发谁计数，定时维护不再替全体成员做梦。"""

import pytest

from butian.runtime.dashboard import snapshot
from butian.runtime.service import ButianService


def _seed(service, member_id, count=3):
    memory = service.memory(member_id)
    try:
        for index in range(count):
            memory.put(f"{member_id}-{index}", "experience", f"{member_id} observation {index}", status="candidate")
    finally:
        memory.close()


def test_manual_dream_only_counts_for_the_member_who_dreamed(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.add_member("b", "B")
        _seed(service, "a")
        result = service.dream_run("a", min_fragments=1, min_interval=0)
        assert result["status"] == "completed"
        assert result["source"] == "manual"
        reported = snapshot(service, "a")
        rows = {row["member_id"]: row for row in reported["member_stats"]}
        assert rows["a"]["totals"]["dream_runs"] == 1
        assert rows["a"]["totals"]["dream_auto"] == 0
        assert rows["b"]["totals"].get("dream_runs", 0) == 0
        assert rows["b"]["totals"].get("dream_attempts", 0) == 0
        assert reported["totals"]["dream_runs"] == 1
    finally:
        service.close()


def test_maintenance_tick_leaves_every_dream_counter_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    monkeypatch.delenv("BUTIAN_AUTO_DREAM", raising=False)
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.add_member("b", "B")
        _seed(service, "a")
        _seed(service, "b")
        before = snapshot(service, "a")["totals"]
        result = service.maintenance_tick()
        after = snapshot(service, "a")["totals"]
        assert result["auto_dream"] is False
        for key in ("dream_runs", "dream_skipped", "dream_attempts", "dream_auto"):
            assert after.get(key, 0) == before.get(key, 0), key
        assert all(item["dream"]["status"] == "not_requested" for item in result["members"])
        rows = {row["member_id"]: row for row in snapshot(service, "a")["member_stats"]}
        assert rows["a"]["totals"]["dream_runs"] == 0
        assert rows["b"]["totals"]["dream_runs"] == 0
    finally:
        service.close()


def test_auto_dream_opt_in_is_marked_and_stays_per_member(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    monkeypatch.setenv("BUTIAN_AUTO_DREAM", "1")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.add_member("b", "B")
        _seed(service, "a")
        result = service.maintenance_tick()
        assert result["auto_dream"] is True
        runs_a = service.list_dream_runs("a", limit=5)
        runs_b = service.list_dream_runs("b", limit=5)
        assert any(run["status"] == "completed" and run["source"] == "auto" for run in runs_a)
        assert runs_b and all(run["status"] == "skipped" for run in runs_b)
        totals_a = snapshot(service, "a")["totals"]
        assert totals_a["dream_runs"] == 1
        assert totals_a["dream_auto"] == 1
    finally:
        service.close()


def test_dream_source_must_be_known(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        with pytest.raises(ValueError):
            service.dream_run("a", min_fragments=1, min_interval=0, source="scheduled")
    finally:
        service.close()
