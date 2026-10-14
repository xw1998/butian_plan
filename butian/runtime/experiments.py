from __future__ import annotations

import hashlib
import json
import math
import platform
import sqlite3
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
 experiment_id TEXT PRIMARY KEY, name TEXT NOT NULL, task_package_hash TEXT NOT NULL,
 dataset_version TEXT NOT NULL, code_commit TEXT NOT NULL, configuration TEXT NOT NULL,
 provider_version TEXT NOT NULL, environment_fingerprint TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_events (
 event_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, event_type TEXT NOT NULL,
 payload TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiment_events ON experiment_events(experiment_id, created_at);
"""


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def environment_fingerprint(extra: dict[str, Any] | None = None) -> str:
    base = {"python": sys.version, "platform": platform.platform(), "machine": platform.machine()}
    if extra:
        base["extra"] = extra
    return stable_hash(base)


class ExperimentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def create(self, name: str, *, task_package: Any, dataset_version: str, code_commit: str,
               configuration: dict[str, Any], provider_version: str,
               environment: dict[str, Any] | None = None, experiment_id: str | None = None) -> str:
        if not name.strip() or not dataset_version.strip() or not code_commit.strip() or not provider_version.strip():
            raise ValueError("experiment_metadata_required")
        experiment_id = experiment_id or f"exp_{uuid.uuid4().hex}"
        now = time.time()
        self.db.execute(
            "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?)",
            (experiment_id, name, stable_hash(task_package), dataset_version, code_commit,
             json.dumps(configuration, ensure_ascii=False, sort_keys=True), provider_version,
             environment_fingerprint(environment), now),
        )
        return experiment_id

    def add_event(self, experiment_id: str, event_type: str, payload: dict[str, Any]) -> str:
        if not self.db.execute("SELECT 1 FROM experiments WHERE experiment_id=?", (experiment_id,)).fetchone():
            raise ValueError("experiment_not_found")
        event_id = f"evt_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO experiment_events VALUES (?,?,?,?,?)", (event_id, experiment_id, event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), time.time()))
        return event_id

    def get(self, experiment_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        if not row:
            raise ValueError("experiment_not_found")
        result = dict(row)
        result["configuration"] = json.loads(result["configuration"])
        result["events"] = [dict(item) for item in self.db.execute("SELECT * FROM experiment_events WHERE experiment_id=? ORDER BY created_at", (experiment_id,))]
        for item in result["events"]:
            item["payload"] = json.loads(item["payload"])
        return result

    def summarize(self, experiment_id: str, metric: str) -> dict[str, Any]:
        events = self.db.execute("SELECT payload FROM experiment_events WHERE experiment_id=? AND event_type='metric'", (experiment_id,)).fetchall()
        values = []
        for row in events:
            payload = json.loads(row["payload"])
            if isinstance(payload.get(metric), (int, float)) and math.isfinite(payload[metric]):
                values.append(float(payload[metric]))
        if not values:
            return {"metric": metric, "sample_count": 0, "mean": None, "stddev": None, "ci95": None}
        mean = statistics.fmean(values)
        if len(values) == 1:
            return {"metric": metric, "sample_count": 1, "mean": mean, "stddev": None, "ci95": None}
        stddev = statistics.stdev(values) if len(values) > 1 else 0.0
        margin = 1.96 * stddev / math.sqrt(len(values))
        return {"metric": metric, "sample_count": len(values), "mean": mean, "stddev": stddev, "ci95": [mean - margin, mean + margin], "ci95_method": "normal_approximation", "ci95_small_sample": len(values) < 30}

    def compare(self, experiment_ids: list[str], metric: str) -> list[dict[str, Any]]:
        if not experiment_ids:
            raise ValueError("experiment_ids_required")
        results = []
        for experiment_id in experiment_ids:
            detail = self.get(experiment_id)
            results.append({"experiment_id": experiment_id, "summary": self.summarize(experiment_id, metric), "metadata": {"name": detail["name"], "dataset_version": detail["dataset_version"], "provider_version": detail["provider_version"], "code_commit": detail["code_commit"], "configuration": detail["configuration"]}, "failures": self.failure_samples(experiment_id), "context_black_hole": self.context_black_hole(experiment_id)})
        return results

    def run_batch(self, name: str, variants: list[dict[str, Any]], *, task_package: Any, dataset_version: str, code_commit: str, configuration: dict[str, Any] | None = None, environment: dict[str, Any] | None = None, repeats: int = 1) -> dict[str, Any]:
        """Create one reproducible experiment per B0/B1/B2 or provider variant."""
        if not variants:
            raise ValueError("experiment_variants_required")
        if not isinstance(repeats, int) or repeats < 1:
            raise ValueError("experiment_repeats_required")
        ids = []
        base = dict(configuration or {})
        for variant in variants:
            variant_name = str(variant.get("name", "")).strip()
            provider_version = str(variant.get("provider_version", "")).strip()
            if not variant_name or not provider_version:
                raise ValueError("experiment_variant_metadata_required")
            config = dict(base)
            config.update(variant.get("configuration", {}))
            config["variant"] = variant_name
            for repeat in range(repeats):
                repeat_config = dict(config)
                repeat_config["repeat"] = repeat + 1
                ids.append(self.create(f"{name}:{variant_name}:r{repeat + 1}", task_package=task_package, dataset_version=dataset_version, code_commit=code_commit, configuration=repeat_config, provider_version=provider_version, environment=environment))
        return {"batch_name": name, "experiment_ids": ids, "task_package_hash": stable_hash(task_package), "dataset_version": dataset_version, "repeats": repeats}

    def batch_compare(self, experiment_ids: list[str], metrics: list[str]) -> dict[str, Any]:
        if not experiment_ids or not metrics:
            raise ValueError("batch_compare_inputs_required")
        if len(experiment_ids) != len(set(experiment_ids)):
            raise ValueError("experiment_ids_must_be_unique")
        details = [self.get(experiment_id) for experiment_id in experiment_ids]
        keys = ("task_package_hash", "dataset_version", "code_commit", "environment_fingerprint")
        mismatches = {key: sorted({str(detail[key]) for detail in details}) for key in keys if len({str(detail[key]) for detail in details}) > 1}
        shared_configs = [{key: value for key, value in detail["configuration"].items() if key not in {"variant", "repeat"}} for detail in details]
        invariant_configs = [{key: value for key, value in config.items() if key not in {"memory"}} for config in shared_configs]
        if len({stable_hash(config) for config in invariant_configs}) > 1:
            mismatches["shared_configuration"] = [stable_hash(config) for config in invariant_configs]
        if mismatches:
            raise ValueError("experiment_match_mismatch:" + json.dumps(mismatches, sort_keys=True))
        provider_strata = {}
        for detail in details:
            provider_strata.setdefault(detail["provider_version"], []).append(detail["experiment_id"])
        return {"provider_strata": provider_strata, "pooled_provider_comparison": len(provider_strata) == 1, "experiments": experiment_ids, "metrics": {metric: self.compare(experiment_ids, metric) for metric in metrics}, "matching": {"task_package_hash": details[0]["task_package_hash"], "dataset_version": details[0]["dataset_version"], "code_commit": details[0]["code_commit"], "environment_fingerprint": details[0]["environment_fingerprint"], "shared_configuration": invariant_configs[0]}}

    def failure_samples(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payload FROM experiment_events WHERE experiment_id=? AND event_type='failure' ORDER BY created_at", (experiment_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def context_black_hole(self, experiment_id: str) -> dict[str, Any]:
        events = self.db.execute("SELECT payload FROM experiment_events WHERE experiment_id=? AND event_type='context'", (experiment_id,)).fetchall()
        total = len(events)
        lost = sum(1 for row in events if json.loads(row["payload"]).get("retrieved") is False)
        return {"sample_count": total, "lost_count": lost, "loss_rate": (lost / total if total else None)}
