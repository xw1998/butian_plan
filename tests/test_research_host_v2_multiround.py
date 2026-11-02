"""钉住真实宿主压力档（host-loop v2）的每个已发表数字。

论文 §VI.I 引用的数字都必须能在
``docs/论文/experiment_results/host-loop-v2-20261102/`` 里逐字复核；同时钉住
四件容易被"顺手改回去"的事：污染组的三条记忆内容始终是错值、状态停在 disputed、
诱饵组的版本号不写进字段名、以及洁净组后三轮读源为 0。
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = ROOT / "docs" / "论文"
RESULT_DIR = PAPER_DIR / "experiment_results" / "host-loop-v2-20261102"
PROTOCOL = PAPER_DIR / "实验协议-真实宿主多轮-v2.md"
RUNNER = PAPER_DIR / "run_host_v2_multiround_study.py"

# 论文 §VI.I：轮次 / 分组 -> (题数, 正确, 答错, 读源, 依据经验, 旧版本错误, 复用坏经验)
PUBLISHED_BY_ROUND = {
    "r1-clean-stable": (6, 6, 0, 6, 0, 0, 0),
    "r1-clean-drift": (6, 6, 0, 6, 0, 0, 0),
    "r1-decoy-drift": (3, 3, 0, 3, 0, 0, 0),
    "r1-poison-stable": (3, 0, 3, 0, 3, 0, 3),
    "r2-clean-stable": (6, 6, 0, 0, 6, 0, 0),
    "r2-clean-drift": (6, 6, 0, 6, 0, 0, 0),
    "r2-decoy-drift": (3, 3, 0, 3, 0, 0, 0),
    "r2-poison-stable": (3, 3, 0, 3, 0, 0, 0),
    "r3-clean-stable": (6, 6, 0, 0, 6, 0, 0),
    "r3-clean-drift": (6, 6, 0, 6, 0, 0, 0),
    "r3-decoy-drift": (3, 3, 0, 3, 0, 0, 0),
    "r3-poison-stable": (3, 3, 0, 3, 0, 0, 0),
    "r4-clean-stable": (6, 6, 0, 0, 6, 0, 0),
    "r4-clean-drift": (6, 6, 0, 6, 0, 0, 0),
    "r4-decoy-drift": (3, 3, 0, 3, 0, 0, 0),
    "r4-poison-stable": (3, 3, 0, 3, 0, 0, 0),
}


def _load(name):
    path = RESULT_DIR / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"结果文件缺失：{path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_protocol_is_preregistered():
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "host-loop-v2" in text
    assert "written_before_execution = true" in text
    for name in ("N1", "N2", "N3", "N4", "N5"):
        assert name in text


def test_summary_shape_and_round_table():
    summary = _load("summary")
    assert summary["protocol"] == "host-loop-v2"
    assert summary["host_kind"] == "real_model_current_conversation"
    assert summary["model_weights_changed"] is False
    assert summary["rounds"] == 4
    assert summary["entities_per_family"] == 6
    assert summary["total_answers"] == 72
    assert len(summary["rows"]) == 72
    for key, expected in PUBLISHED_BY_ROUND.items():
        block = summary["by_round"][key]
        assert (block["n"], block["correct"], block["wrong"], block["reads"],
                block["experience_basis"], block["stale_version_errors"],
                block["poisoned_reuse"]) == expected, key
    assert summary["recall_limit"] == 100


def test_totals_match_paper():
    summary = _load("summary")
    rows = summary["rows"]
    assert sum(1 for row in rows if row["verdict"] == "correct") == 69
    assert sum(1 for row in rows if row["verdict"] == "wrong") == 3
    assert sum(1 for row in rows if row["verdict"] == "unknown") == 0
    # 72 次满读 vs 51 次实测 -> 省 21 次（29.2%）
    assert sum(1 for row in rows if row["need_source"]) == 51
    wrong = [row for row in rows if row["verdict"] == "wrong"]
    assert {row["group"] for row in wrong} == {"poison-stable"}
    assert {row["round"] for row in wrong} == {1}


def test_all_criteria_and_poison_recovery():
    summary = _load("summary")
    assert summary["criteria"] == {
        "N1_clean_reuse_holds": True,
        "N2_version_gate_under_decoy": True,
        "N3_poison_recovery_fast": True,
        "N4_degradation_visible_and_used": True,
        "N5_behaviour_auditable": True,
    }
    assert summary["all_criteria_met"] is True
    assert summary["n5_violations"] == []
    recovery = summary["poison_recovery"]
    assert set(recovery) == {"calibration004", "schema004", "tool004"}
    for entity, block in recovery.items():
        assert block["first_wrong_round"] == 1, entity
        assert block["recovered_round"] == 2, entity
        assert block["read_rounds"] == [2, 3, 4], entity


def test_poison_memories_keep_wrong_content_and_disputed():
    """N4 的硬证据：内容一字未改、状态停在 disputed、却攒了 5 次成功。"""
    data = _load("memories")
    memories = {row["memory_id"]: row for row in data["memories"]}
    assert len(memories) == 45
    poison = _load("fixtures/poison")
    for entity, spec in poison.items():
        memory = memories[spec["memory_id"]]
        assert memory["status"] == "disputed", entity
        assert memory["successes"] == 5 and memory["failures"] == 1, entity
        wrong = json.loads(memory["content"].split("RULE ", 1)[1])
        for field in ("offset", "divisor", "factor"):
            if field in spec["memory"]:
                assert wrong[field] == spec["memory"][field] != spec["true"][field], entity
    statuses = [row["status"] for row in data["memories"]]
    assert statuses.count("warm") == 6
    assert statuses.count("candidate") == 36
    assert statuses.count("disputed") == 3


def test_decoy_versions_hide_the_version_number():
    """诱饵组的版本号不进字段名/端点名，只有数值差 3%。"""
    rules = _load("fixtures/rules")
    for entity in ("calibration005", "schema005", "tool005"):
        versions = [rules[f"{entity}@{n}"] for n in (1, 2, 3, 4)]
        assert [rule["version"] for rule in versions] == [1, 2, 3, 4]
        names = {(rule.get("endpoint"), rule.get("parameter"), rule.get("flow_field"),
                  rule.get("level_field")) for rule in versions}
        assert len(names) == 1, entity
        for field, step in (("offset", 3), ("divisor", 3), ("factor", 3)):
            if field in versions[0]:
                assert [rule[field] for rule in versions] == [versions[0][field] + step * n for n in range(4)]


def test_host_decisions_show_disputed_switch():
    """N3/N4 的行为证据：污染组首轮不读源、此后每轮读源。"""
    for round_no in (1, 2, 3, 4):
        path = RESULT_DIR / "stages" / f"r{round_no}-recall-decision.json"
        if not path.is_file():
            pytest.skip(f"决策文件缺失：{path}")
        with path.open(encoding="utf-8") as handle:
            decisions = json.load(handle)
        reads = sum(1 for value in decisions.values() if value["need_source"])
        expected = {1: 15, 2: 12, 3: 12, 4: 12}[round_no]
        assert reads == expected, round_no
        assert len(decisions) == 18


def test_runner_is_committed():
    assert RUNNER.is_file()
    source = RUNNER.read_text(encoding="utf-8")
    assert "PROTOCOL = 'host-loop-v2'" in source
    assert "RECALL_LIMIT = 100" in source
    assert "POISON_OFFSET" in source
    assert "host_kind" in source and "stub" in source
