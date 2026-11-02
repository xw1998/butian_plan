"""Pin the published end-to-end gate numbers to the committed result files.

论文 §V.F（表5）与 §VIII 引用的每个数字都必须能在
``docs/论文/experiment_results/e2e-gate-20261102/`` 里逐字复核；任何一侧被改动
（包括重跑脚本但没更新结果，或改了结果但没更新论文）都会在这里失败。
"""

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "docs" / "论文" / "experiment_results" / "e2e-gate-20261102"

ARMS = ["G0-index", "G1-direct", "G2-gate", "G3-gate-inject", "G4-reuse-inject", "G5-inject-warm"]

# 论文表5（20 种子合并，每臂 2880 题）：
# 条件 / 正确 / 错误 / 未知 / 源读取 / 写回 / 闸拒绝
PUBLISHED_ROWS = {
    "G0-index": (2880, 2759, 0, 121, 2759, 0, 0),
    "G1-direct": (2880, 2880, 0, 0, 496, 496, 0),
    "G2-gate": (2880, 2880, 0, 0, 736, 736, 240),
    "G3-gate-inject": (2880, 2844, 0, 36, 992, 992, 449),
    "G4-reuse-inject": (2880, 2653, 209, 18, 752, 943, 0),
    "G5-inject-warm": (2880, 2634, 209, 37, 1601, 1792, 868),
}

SEEDS = [1103, 2207, 3301, 4409, 5519, 6619, 7723, 8837, 9941, 10151,
         11257, 12301, 13309, 14387, 15401, 16411, 17417, 18427, 19433, 20441]


def _load(name):
    path = RESULT_DIR / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"结果文件缺失：{path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_published_table5_matches_result_files():
    summary = _load("summary")["summary"]
    assert sorted(summary) == sorted(ARMS)
    for arm in ARMS:
        row = summary[arm]
        expected = PUBLISHED_ROWS[arm]
        actual = (row["tasks"], row["correct"], row["wrong"], row["unknown"],
                  row["source_reads"], row["writes"], row["gate_rejections"])
        assert actual == expected, f"{arm} 与论文表5不一致"


def test_accuracy_column_matches_published_rates():
    summary = _load("summary")["summary"]
    # 论文正文引用的小数：0.9580 / 1.0000 / 1.0000 / 0.9875 / 0.9212 / 0.9146
    published = {
        "G0-index": 0.9580, "G1-direct": 1.0, "G2-gate": 1.0,
        "G3-gate-inject": 0.9875, "G4-reuse-inject": 0.9212, "G5-inject-warm": 0.9146,
    }
    for arm, rate in published.items():
        assert summary[arm]["accuracy"] == pytest.approx(rate, abs=5e-5), arm


def test_gate_effect_size_and_interval():
    paired = _load("summary")["paired"]
    effect = paired["G2-gate"]["paired"]["accuracy_delta_vs_index"]
    # +4.20 个百分点，95% 区间 [3.65, 4.79]，20/20 种子同向
    assert effect["mean"] == pytest.approx(0.042, abs=5e-4)
    low, high = effect["cluster_bootstrap_95"]
    assert low == pytest.approx(0.0365, abs=5e-4)
    assert high == pytest.approx(0.0479, abs=5e-4)
    assert len(effect["seed_values"]) == 20
    assert all(value > 0 for value in effect["seed_values"])


def test_gate_costs_exactly_one_write_and_no_accuracy_on_direct_arm():
    paired = _load("summary")["paired"]
    block = paired["G2-gate"]["paired"]
    assert block["accuracy_delta_vs_direct"]["mean"] == 0.0
    assert all(value == 0.0 for value in block["accuracy_delta_vs_direct"]["seed_values"])
    reads = block["read_delta_vs_direct"]["seed_values"]
    # 每种子恒 +12 次读取，等于 240 次闸拒绝 / 20 种子
    assert reads == [12] * 20


def test_verification_arms_lose_first_attempt_correctness():
    summary = _load("summary")
    paired = summary["paired"]
    # 注入坏经验后，正确率相对直写臂降 −7.88 pp / −8.54 pp，
    # 折成每种子少解 11.35 / 12.30 题（结果文档相对直写列的口径）。
    assert paired["G4-reuse-inject"]["paired"]["accuracy_delta_vs_direct"]["mean"] == pytest.approx(-0.0788, abs=5e-5)
    assert paired["G5-inject-warm"]["paired"]["accuracy_delta_vs_direct"]["mean"] == pytest.approx(-0.0854, abs=5e-5)
    for arm, published in (("G4-reuse-inject", 11.35), ("G5-inject-warm", 12.30)):
        lost = (summary["summary"]["G1-direct"]["correct"] - summary["summary"][arm]["correct"]) / 20
        assert lost == pytest.approx(published, abs=5e-3)
    # 两条臂各 209 次错误，且都是 20/20 种子为负
    for arm in ("G4-reuse-inject", "G5-inject-warm"):
        assert summary["summary"][arm]["wrong"] == 209
        values = paired[arm]["paired"]["accuracy_delta_vs_direct"]["seed_values"]
        assert sum(value < 0 for value in values) == 20


def test_replication_check_is_clean_on_the_published_ten_seeds():
    check = _load("replication-check")
    assert check["compared_arm"] == "B2-full"
    for section in ("stream", "row_level"):
        rows = check[section]
        assert len(rows) == 20
        assert sum(row["mismatches"] for row in rows) == 0, section
        # 前 10 个种子逐项复核 144 条；后 10 个种子没有可对照的已发布任务流
        assert [row["checked"] for row in rows[:10]] == [144] * 10
        assert [row["checked"] for row in rows[10:]] == [0] * 10


def test_protocol_is_frozen_and_matches_the_recorded_seeds():
    protocol = _load("protocol")
    assert protocol["protocol_version"] == 1
    assert protocol["methods"] == ARMS
    assert protocol["seeds"] == SEEDS
    assert protocol["rounds"] == 12
    assert protocol["entities_per_family"] == 4
    assert protocol["inject_rounds"] == [6, 10]
    assert protocol["candidate_reusable"] == ["G4-reuse-inject"]
    assert protocol["bootstrap_draws"] == 10000
    assert protocol["written_before_execution"] is True


def test_environment_pins_the_runner_that_produced_the_results():
    environment = _load("environment")
    assert environment["base_commit"].startswith("6396bab")
    runner = ROOT / "docs" / "论文" / "run_endtoend_gate_study.py"
    digest = hashlib.sha256(runner.read_bytes()).hexdigest()
    assert digest == environment["script_sha256"], "运行脚本已改动，结果文件需要重跑后才可信"


def test_manifest_matches_committed_result_files():
    manifest = _load("manifest")
    checked = 0
    for name, expected in manifest.items():
        path = RESULT_DIR / name
        if not path.is_file():
            # rows/damage/injections/memory-audit 体积过大不随仓库发布
            continue
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
        checked += 1
    assert checked >= 5


def test_lifecycle_probe_shows_the_automatic_path_does_not_close_the_loop():
    probe = _load("lifecycle-probe")
    assert probe["automatic_path_closes_loop"] is False
    assert probe["automatic_rewarm_after_failure"] == "disputed"
    assert probe["explicit_revision_restores_candidate"] == "candidate"


def test_summary_declares_no_model_inference():
    summary = _load("summary")
    assert summary["execution_kind"] == "end_to_end_gate_benchmark"
    assert summary["llm_inference_calls"] == 0
    assert summary["model_weights_changed"] is False
    assert len(summary["limitations"]) >= 3
