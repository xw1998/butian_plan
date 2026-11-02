"""Pin the real-host multiround numbers to the committed result files.

论文 §VI.H 引用的每个数字都必须能在
``docs/论文/experiment_results/host-loop-20261102/`` 里逐字复核；同时钉住三件
容易被“顺手改回去”的事：归档里 (entity, version) 只能有一条 RULE 行、归档必须
写明换算约定、召回上限必须够取回全部记忆。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = ROOT / "docs" / "论文"
RESULT_DIR = PAPER_DIR / "experiment_results" / "host-loop-20261102"
PROTOCOL = ROOT / "docs" / "论文" / "实验协议-真实宿主多轮.md"
RUNNER = ROOT / "docs" / "论文" / "run_host_multiround_study.py"

# 论文 §VI.H：轮次 / 分组 -> (题数, 正确, 答错, 答不出, 源读取, 依据经验, 旧版本错误)
PUBLISHED_BY_ROUND = {
    "r1-stable": (6, 6, 0, 0, 6, 0, 0),
    "r1-drift": (6, 6, 0, 0, 6, 0, 0),
    "r2-stable": (6, 6, 0, 0, 0, 6, 0),
    "r2-drift": (6, 6, 0, 0, 6, 0, 0),
    "r3-stable": (6, 6, 0, 0, 0, 6, 0),
    "r3-drift": (6, 6, 0, 0, 6, 0, 0),
}


def _load(name):
    path = RESULT_DIR / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"结果文件缺失：{path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_protocol_is_preregistered():
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "host-loop-v1" in text
    assert "written_before_execution = true" in text
    for name in ("P1", "P2", "P3", "P4", "P5"):
        assert name in text


def test_summary_shape_and_round_table():
    summary = _load("summary")
    assert summary["protocol"] == "host-loop-v1"
    assert summary["host_kind"] == "real_model_current_conversation"
    assert summary["model_weights_changed"] is False
    assert summary["rounds"] == 3
    assert summary["total_answers"] == 36
    assert len(summary["rows"]) == 36
    for key, expected in PUBLISHED_BY_ROUND.items():
        block = summary["by_round"][key]
        assert (block["n"], block["correct"], block["wrong"], block["unknown"],
                block["reads"], block["experience_basis"],
                block["stale_version_errors"]) == expected, key


def test_all_rows_correct_and_reads_sum():
    summary = _load("summary")
    rows = summary["rows"]
    assert all(row["verdict"] == "correct" for row in rows)
    assert all(row["stale_version_error"] is False for row in rows)
    assert sum(1 for row in rows if row["need_source"]) == 24
    # 稳定实体只在首轮读源；漂移实体三轮都读
    stable = [row for row in rows if row["stable"]]
    drift = [row for row in rows if not row["stable"]]
    assert [row["need_source"] for row in stable] == [True] * 6 + [False] * 12
    assert all(row["need_source"] for row in drift)


def test_criteria_all_hold_with_strict_p5():
    summary = _load("summary")
    assert summary["criteria"] == {
        "P1_accuracy_not_worse": True,
        "P2_version_gate_effective": True,
        "P3_reuse_saves_reads": True,
        "P4_experience_formed": True,
        "P5_behaviour_auditable": True,
    }
    assert summary["all_criteria_met"] is True
    assert summary["p5_violations"] == []
    assert summary["recall_limit"] == 50


def test_memory_terminal_state():
    memories = _load("memories")["memories"]
    assert len(memories) == 24
    warm = [m for m in memories if m["status"] == "warm"]
    candidate = [m for m in memories if m["status"] == "candidate"]
    assert len(warm) == 6 and len(candidate) == 18
    assert all(m["successes"] == 3 and len(m["independent_keys"]) == 3 for m in warm)
    assert all(m["successes"] == 1 for m in candidate)
    # warm 的六条全是稳定实体的 v1
    assert sorted(m["memory_id"] for m in warm) == [
        "calibration000-v1", "calibration001-v1", "schema000-v1",
        "schema001-v1", "tool000-v1", "tool001-v1",
    ]


def test_archive_has_one_rule_per_entity_and_version():
    """修过的构造缺陷：同一实体同一版本只能一条 RULE 行，且必须写明换算约定。"""
    text = (RESULT_DIR / "fixtures" / "contract-manual-v1-v3.txt").read_text(encoding="utf-8")
    rules = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("RULE ")]
    keys = [(r["entity"], r["version"]) for r in rules]
    assert len(keys) == len(set(keys))
    assert "(raw - offset) / scale" in text
    assert "flow = payload[flow_field] / divisor" in text


def test_stable_entities_reuse_one_rule_across_rounds():
    """稳定实体的三轮必须共用同一条规则，否则归档会出现同名同版本的多条 RULE。"""
    if str(PAPER_DIR) not in sys.path:
        sys.path.insert(0, str(PAPER_DIR))
    from run_host_multiround_study import build_tasks

    tasks, rules = build_tasks()
    assert len(tasks) == 36
    for entity in ("calibration000", "schema001", "tool000"):
        payloads = {json.dumps(rules[f"{entity}@{rnd}"], sort_keys=True) for rnd in (1, 2, 3)}
        assert len(payloads) == 1, entity
    for entity in ("calibration002", "schema002", "tool002"):
        assert [rules[f"{entity}@{rnd}"]["version"] for rnd in (1, 2, 3)] == [1, 2, 3]


def test_host_decisions_show_version_check():
    """P2/P3 的行为证据：稳定实体后两轮不读源，漂移实体三轮都读源。"""
    expected = {1: 12, 2: 6, 3: 6}  # 首轮无经验，全部读源
    for round_no in (1, 2, 3):
        path = RESULT_DIR / "stages" / f"r{round_no}-recall-decision.json"
        if not path.is_file():
            pytest.skip(f"决策文件缺失：{path}")
        with path.open(encoding="utf-8") as handle:
            decisions = json.load(handle)
        reads = sum(1 for value in decisions.values() if value["need_source"])
        assert reads == expected[round_no], round_no
        assert all("need_source" in value and "use_experience" in value for value in decisions.values())


def test_runner_is_committed():
    assert RUNNER.is_file()
    source = RUNNER.read_text(encoding="utf-8")
    assert "RECALL_LIMIT = 50" in source
    assert "host_kind" in source and "stub" in source
