"""用 host-loop-v2 的 45 条真实记忆复算召回的「修前 / 修后」精度。

背景：`butian_memory_recall` 过去把查询按空白切词后走 LIKE 或链，任何一条记忆撞上一个
到处都出现的词就进候选；排序按 status/weight/时间走。于是问 calibration004 时，
36 条同族记忆一起端回来，宿主必须自己按实体名过滤。

修法写在 butian/runtime/memory.py 的 recall_for_decision 里：4 字以上、且在库里稀有的词
当硬闸门；稀有度用 IDF 加权参与排序。这个脚本只做复算，不改任何实验产物。

    python docs/论文/check_recall_precision.py \
        --results docs/论文/experiment_results/host-loop-v2-20261102 \
        --out docs/论文/experiment_results/host-loop-v2-20261102/recall-precision-fix.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butian.runtime.memory import MemoryStore  # noqa: E402

RANK = "CASE status WHEN 'hot' THEN 0 WHEN 'warm' THEN 1 WHEN 'candidate' THEN 2 WHEN 'disputed' THEN 3 ELSE 4 END"


def old_recall(store: MemoryStore, query: str, limit: int = 100) -> list[str]:
    """修复前的实现：按空白切词，单词时整串拆成二字组，OR 链 + 状态排序。"""
    tokens = list(dict.fromkeys(token for token in query.split() if len(token) >= 2))
    if len(tokens) <= 1 and len(query.strip()) >= 2:
        normalized = query.strip().replace(" ", "")
        tokens = list(dict.fromkeys(normalized[index:index + 2] for index in range(len(normalized) - 1)))
    if not tokens:
        return []
    clauses = " OR ".join("content LIKE ?" for _ in tokens)
    args: list[object] = [f"%{token}%" for token in tokens]
    args.append(limit)
    rows = store.db.execute(
        f"SELECT memory_id FROM memories WHERE ({clauses}) AND status NOT IN ('invalidated','archived') ORDER BY {RANK}, weight DESC, updated_at DESC LIMIT ?",
        args,
    )
    return [row[0] for row in rows]


def build_store(memories: list[dict], path: Path) -> MemoryStore:
    store = MemoryStore(path)
    for item in memories:
        evidence = ["replay-seed"] if item["status"] in {"warm", "hot"} else ["replay-seed"]
        store.put(
            item["memory_id"],
            item["kind"],
            item["content"],
            confidence=float(item.get("confidence", 0.2)),
            weight=float(item.get("weight", 1.0)),
            status=item["status"],
            source_event_ids=evidence,
        )
    return store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    results = Path(args.results)
    exported = json.loads((results / "memories.json").read_text(encoding="utf-8"))
    memories = exported["memories"]

    with tempfile.TemporaryDirectory() as workdir:
        store = build_store(memories, Path(workdir) / "memory.db")
        try:
            rounds = sorted(int(path.stem.split("-")[0][1:]) for path in (results / "stages").glob("r*-recall-request.json"))
            per_round = {}
            for round_no in rounds:
                request = json.loads((results / "stages" / f"r{round_no}-recall-request.json").read_text(encoding="utf-8"))
                rows = []
                for item in request["items"]:
                    entity = item["entity"]
                    before = old_recall(store, entity, args.limit)
                    after_items = store.recall_for_decision(entity, limit=args.limit)
                    after = [entry["memory_id"] for entry in after_items]
                    rows.append({
                        "entity": entity,
                        "group": item.get("group"),
                        "version": item.get("version"),
                        "before_items": len(before),
                        "before_relevant": sum(entity in memory_id or entity in store.get(memory_id)["content"] for memory_id in before),
                        "after_items": len(after),
                        "after_relevant": sum(entity in entry["memory_id"] or entity in entry["content"] for entry in after_items),
                        "after_ids": after,
                    })
                per_round[str(round_no)] = rows
        finally:
            store.close()

    flat = [row for rows in per_round.values() for row in rows]
    summary = {
        "source": "host-loop-v2 / 45 memories / 4 rounds x 18 entities",
        "limit": args.limit,
        "queries": len(flat),
        "before": {
            "items_median": statistics.median(row["before_items"] for row in flat),
            "items_mean": round(statistics.mean(row["before_items"] for row in flat), 2),
            "items_max": max(row["before_items"] for row in flat),
            "items_total": sum(row["before_items"] for row in flat),
            "relevant_total": sum(row["before_relevant"] for row in flat),
            "precision": round(sum(row["before_relevant"] for row in flat) / max(1, sum(row["before_items"] for row in flat)), 4),
            "queries_with_foreign_items": sum(1 for row in flat if row["before_items"] != row["before_relevant"]),
        },
        "after": {
            "items_median": statistics.median(row["after_items"] for row in flat),
            "items_mean": round(statistics.mean(row["after_items"] for row in flat), 2),
            "items_max": max(row["after_items"] for row in flat),
            "items_total": sum(row["after_items"] for row in flat),
            "relevant_total": sum(row["after_relevant"] for row in flat),
            "precision": round(sum(row["after_relevant"] for row in flat) / max(1, sum(row["after_items"] for row in flat)), 4),
            "queries_with_foreign_items": sum(1 for row in flat if row["after_items"] != row["after_relevant"]),
            "queries_empty": sum(1 for row in flat if row["after_items"] == 0),
        },
        "per_round": {key: [{"entity": row["entity"], "group": row["group"], "before": row["before_items"], "after": row["after_items"], "after_ids": row["after_ids"]} for row in rows] for key, rows in per_round.items()},
    }
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("queries", "before", "after")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
