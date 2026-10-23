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
        # 自动扫查里“无事可做”的结果不落库：否则每人每小时一条 skipped 会把
        # 真正的 completed 从“最近 20 条”里挤出去，也让 skipped 指标凭空线性增长。
        assert runs_b == []
        totals_b = {row["member_id"]: row for row in snapshot(service, "b")["member_stats"]}["b"]["totals"]
        assert totals_b.get("dream_skipped", 0) == 0
        assert totals_b.get("dream_attempts", 0) == 0
        skipped_auto = [item for item in result["members"] if item["member_id"] == "b"][0]["dream"]
        assert skipped_auto["status"] == "skipped" and skipped_auto["recorded"] is False
        assert skipped_auto["reason"] == "insufficient_fragments"
        totals_a = snapshot(service, "a")["totals"]
        assert totals_a["dream_runs"] == 1
        assert totals_a["dream_auto"] == 1
    finally:
        service.close()


def test_manual_skipped_checks_are_still_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        # 用户手动点了做梦但没有材料：必须留下记录并给出原因，不能静默。
        result = service.dream_run("a", min_fragments=3, min_interval=0)
        assert result["status"] == "skipped"
        assert result["reason"] == "insufficient_fragments"
        assert result["recorded"] is True
        assert result["run_id"]
        runs = service.list_dream_runs("a")
        assert len(runs) == 1 and runs[0]["status"] == "skipped" and runs[0]["source"] == "manual"
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
