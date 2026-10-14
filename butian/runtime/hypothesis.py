from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
import threading
from .store import serialized_write
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HypothesisAssessment:
    reliability: float
    rationale: dict[str, Any]


class HypothesisStore:
    """Team-level prior knowledge and practice-hypothesis ledger.

    A prior can rank verification work, but never proves a hypothesis. Only
    independent, valid evidence can create a verification observation.
    """

    SOURCES = {"experience", "lesson", "external", "user", "skill", "analogy", "deduction", "unknown"}
    KINDS = {"practice_hypothesis", "hardware_hypothesis", "causal_hypothesis"}
    OUTCOMES = {"supported", "rejected", "inconclusive", "execution_failed", "unknown"}

    def __init__(self, db: sqlite3.Connection, *, write_lock=None):
        self._write_lock = write_lock or threading.RLock()
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS prior_knowledge (
            prior_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, title TEXT NOT NULL,
            claim TEXT NOT NULL, environment TEXT NOT NULL, source_refs TEXT NOT NULL,
            reliability REAL NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            owner_member_id TEXT
        );
        CREATE TABLE IF NOT EXISTS practice_hypotheses (
            hypothesis_id TEXT PRIMARY KEY, prior_ids TEXT NOT NULL, kind TEXT NOT NULL,
            statement TEXT NOT NULL, rationale TEXT NOT NULL, conditions TEXT NOT NULL,
            predictions TEXT NOT NULL, falsifiers TEXT NOT NULL, status TEXT NOT NULL,
            prior_reliability REAL NOT NULL, verification_required INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, owner_member_id TEXT
        );
        CREATE TABLE IF NOT EXISTS hypothesis_authorizations (authorization_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, actor_member_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL, owner_member_id TEXT);
        CREATE TABLE IF NOT EXISTS hypothesis_actions (
            action_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, description TEXT NOT NULL,
            cost REAL NOT NULL, risk REAL NOT NULL, information_gain REAL NOT NULL,
            authorization TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            started_at REAL, finished_at REAL, owner_member_id TEXT
        );
        CREATE TABLE IF NOT EXISTS hypothesis_observations (
            observation_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL,
            action_id TEXT NOT NULL, outcome TEXT NOT NULL, evidence_ids TEXT NOT NULL,
            environment TEXT NOT NULL, independent_key TEXT NOT NULL, notes TEXT NOT NULL,
            created_at REAL NOT NULL, owner_member_id TEXT, UNIQUE(hypothesis_id, independent_key)
        );
        """)
        for table in ("prior_knowledge", "practice_hypotheses", "hypothesis_authorizations", "hypothesis_actions", "hypothesis_observations"):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if "owner_member_id" not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN owner_member_id TEXT")
        self.db.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)

    @serialized_write
    def add_prior(self, source_kind: str, title: str, claim: str, *, environment=None, source_refs=None, reliability: float = 0.0, owner_member_id: str | None = None) -> str:
        if source_kind not in self.SOURCES or not title.strip() or not claim.strip() or not 0 <= reliability <= 1:
            raise ValueError("invalid_prior_knowledge")
        prior_id = f"prior_{uuid.uuid4().hex}"
        now = time.time()
        self.db.execute("INSERT INTO prior_knowledge VALUES (?,?,?,?,?,?,?,?,?,?,?)", (prior_id, source_kind, title.strip(), claim.strip(), self._json(environment or {}), self._json(list(source_refs or [])), reliability, "candidate", now, now, owner_member_id))
        return prior_id

    def assess(self, source_kinds: list[str], *, environment_match: float = 0.5, independent_sources: int = 0, counterexamples: int = 0) -> HypothesisAssessment:
        if not source_kinds or any(item not in self.SOURCES for item in source_kinds) or not 0 <= environment_match <= 1:
            raise ValueError("invalid_prior_assessment")
        source_base = {"experience": .7, "lesson": .7, "external": .55, "user": .5, "skill": .5, "analogy": .35, "deduction": .3, "unknown": .1}
        base = sum(source_base[x] for x in source_kinds) / len(source_kinds)
        independent_bonus = min(.2, independent_sources * .05)
        contradiction_penalty = min(.3, counterexamples * .1)
        score = max(0.0, min(1.0, base * environment_match + independent_bonus - contradiction_penalty))
        return HypothesisAssessment(score, {"source_kinds": source_kinds, "environment_match": environment_match, "independent_sources": independent_sources, "counterexamples": counterexamples, "is_prior_only": True})

    @serialized_write
    def propose(self, kind: str, statement: str, *, prior_ids=None, rationale: str, conditions=None, predictions=None, falsifiers=None, assessment: HypothesisAssessment, owner_member_id: str | None = None) -> str:
        if kind not in self.KINDS or not statement.strip() or not rationale.strip() or not assessment.rationale.get("is_prior_only"):
            raise ValueError("invalid_practice_hypothesis")
        ids = list(dict.fromkeys(prior_ids or []))
        if ids:
            found = {row[0] for row in self.db.execute("SELECT prior_id FROM prior_knowledge WHERE prior_id IN (%s) AND owner_member_id IS ?" % ",".join("?" * len(ids)), ids + [owner_member_id])}
            if found != set(ids):
                raise ValueError("prior_knowledge_not_found")
        hypothesis_id = f"hyp_{uuid.uuid4().hex}"
        now = time.time()
        self.db.execute("INSERT INTO practice_hypotheses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (hypothesis_id, self._json(ids), kind, statement.strip(), rationale.strip(), self._json(conditions or {}), self._json(predictions or []), self._json(falsifiers or []), "candidate", assessment.reliability, 1, now, now, owner_member_id))
        return hypothesis_id

    @serialized_write
    def authorize(self, hypothesis_id: str, description: str, *, cost: float, risk: float, information_gain: float, owner_member_id: str | None = None) -> str:
        if not self.db.execute("SELECT 1 FROM practice_hypotheses WHERE hypothesis_id=? AND owner_member_id IS ?", (hypothesis_id, owner_member_id)).fetchone():
            raise ValueError("hypothesis_not_found")
        if not isinstance(description, str) or not description.strip() or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (cost, risk, information_gain)) or cost < 0 or not 0 <= risk <= 1 or information_gain < 0:
            raise ValueError("invalid_verification_metrics")
        action_id = f"act_{uuid.uuid4().hex}"
        self.db.execute("INSERT INTO hypothesis_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (action_id, hypothesis_id, description, cost, risk, information_gain, "", "awaiting_authorization", time.time(), None, None, owner_member_id))
        return action_id

    @serialized_write
    def grant_authorization(self, action_id: str, *, actor_member_id: str, reason: str, authorized: bool, owner_member_id: str | None = None) -> None:
        row = self.db.execute("SELECT * FROM hypothesis_actions WHERE action_id=? AND owner_member_id IS ?", (action_id, owner_member_id)).fetchone()
        if not row:
            raise ValueError("verification_action_not_found")
        if row["status"] != "awaiting_authorization":
            raise ValueError("verification_action_not_awaiting_authorization")
        if not isinstance(authorized, bool):
            raise ValueError("authorization_must_be_bool")
        if not actor_member_id.strip() or not reason.strip():
            raise ValueError("authorization_actor_and_reason_required")
        if not self.db.execute("SELECT 1 FROM members WHERE member_id=?", (actor_member_id,)).fetchone():
            raise ValueError("authorization_actor_not_found")
        now = time.time()
        auth_id = f"auth_{uuid.uuid4().hex}"
        self.db.execute("SAVEPOINT grant_hypothesis_authorization")
        try:
            self.db.execute("INSERT INTO hypothesis_authorizations VALUES (?,?,?,?,?,?,?)", (auth_id, row["hypothesis_id"], actor_member_id, "approved" if authorized else "denied", reason, now, owner_member_id))
            updated = self.db.execute("UPDATE hypothesis_actions SET authorization=?,status=? WHERE action_id=? AND status='awaiting_authorization' AND owner_member_id IS ?", (auth_id, "queued" if authorized else "denied", action_id, owner_member_id)).rowcount
            if updated != 1:
                raise ValueError("verification_action_not_awaiting_authorization")
            self.db.execute("RELEASE SAVEPOINT grant_hypothesis_authorization")
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT grant_hypothesis_authorization")
            self.db.execute("RELEASE SAVEPOINT grant_hypothesis_authorization")
            raise

    @serialized_write
    def start(self, action_id: str, *, owner_member_id: str | None = None) -> None:
        self.db.execute("SAVEPOINT hypothesis_start_budget")
        try:
            from .exploration_budget import ExplorationBudget
            action = self.db.execute("SELECT * FROM hypothesis_actions WHERE action_id=? AND owner_member_id IS ?", (action_id, owner_member_id)).fetchone()
            if action and action["status"] == "queued":
                ExplorationBudget(self.db).reserve(action_id, owner_member_id, cost=action["cost"], risk=action["risk"])
            updated = self.db.execute("UPDATE hypothesis_actions SET status='running',started_at=? WHERE action_id=? AND status='queued' AND owner_member_id IS ?", (time.time(), action_id, owner_member_id)).rowcount
            if updated != 1:
                visible = self.db.execute("SELECT 1 FROM hypothesis_actions WHERE action_id=? AND owner_member_id IS ?", (action_id, owner_member_id)).fetchone()
                if not visible:
                    raise ValueError("verification_action_not_found")
                raise ValueError("verification_action_not_authorized_or_not_queued")

            self.db.execute("RELEASE SAVEPOINT hypothesis_start_budget")
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT hypothesis_start_budget")
            self.db.execute("RELEASE SAVEPOINT hypothesis_start_budget")
            raise

    @serialized_write
    def observe(self, action_id: str, outcome: str, evidence_ids: list[str], *, environment=None, independent_key: str, notes: str = "", outbox: dict[str, Any] | None = None, observation_id: str | None = None, owner_member_id: str | None = None) -> str:
        if outcome not in self.OUTCOMES or not independent_key.strip():
            raise ValueError("invalid_hypothesis_observation")
        action = self.db.execute("SELECT * FROM hypothesis_actions WHERE action_id=? AND owner_member_id IS ?", (action_id, owner_member_id)).fetchone()
        if not action:
            raise ValueError("verification_action_not_found")
        if action["status"] != "running":
            raise ValueError("verification_action_not_running")
        if outcome in {"supported", "rejected"} and not evidence_ids:
            raise ValueError("evidence_required_for_conclusive_outcome")
        if outbox is not None:
            required = {"member_id", "memory_id", "kind", "content"}
            missing = required.difference(outbox)
            if missing:
                raise KeyError(sorted(missing)[0])
        observation_id = observation_id or f"obs_{uuid.uuid4().hex}"
        now = time.time()
        self.db.execute("SAVEPOINT hypothesis_observation")
        try:
            self.db.execute("INSERT INTO hypothesis_observations VALUES (?,?,?,?,?,?,?,?,?,?)", (observation_id, action["hypothesis_id"], action_id, outcome, self._json(evidence_ids), self._json(environment or {}), independent_key, notes, now, owner_member_id))
            self.db.execute("UPDATE hypothesis_actions SET status='completed',finished_at=? WHERE action_id=? AND owner_member_id IS ?", (now, action_id, owner_member_id))
            self._refresh_status(action["hypothesis_id"], owner_member_id=owner_member_id)
            if outbox is not None:
                operation_key = f"member-memory:{observation_id}:{outbox['member_id']}"
                outbox_id = f"outbox_{uuid.uuid4().hex}"
                self.db.execute("INSERT INTO member_memory_outbox(outbox_id,operation_key,observation_id,member_id,memory_id,kind,content,environment,source_event_ids,status,attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',0,NULL,?,?)", (outbox_id, operation_key, observation_id, outbox["member_id"], outbox["memory_id"], outbox["kind"], outbox["content"], self._json(environment or {}), self._json([observation_id]), now, now))
                self.db.execute("INSERT INTO member_memory_outbox_events VALUES (?,?,?,?,?)", (f"mmev_{uuid.uuid4().hex}", outbox_id, "pending", None, now))
            self.db.execute("RELEASE SAVEPOINT hypothesis_observation")
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT hypothesis_observation")
            self.db.execute("RELEASE SAVEPOINT hypothesis_observation")
            raise
        return observation_id

    @serialized_write
    def _refresh_status(self, hypothesis_id: str, *, owner_member_id: str | None = None) -> str:
        rows = self.db.execute("SELECT outcome FROM hypothesis_observations WHERE hypothesis_id=? AND owner_member_id IS ?", (hypothesis_id, owner_member_id)).fetchall()
        outcomes = [row[0] for row in rows]
        if any(x == "supported" for x in outcomes) and not any(x == "rejected" for x in outcomes):
            status = "supported"
        elif any(x == "rejected" for x in outcomes) and not any(x == "supported" for x in outcomes):
            status = "rejected"
        elif outcomes:
            status = "inconclusive"
        else:
            status = "candidate"
        self.db.execute("UPDATE practice_hypotheses SET status=?,updated_at=? WHERE hypothesis_id=? AND owner_member_id IS ?", (status, time.time(), hypothesis_id, owner_member_id))
        return status

    @serialized_write
    def get(self, hypothesis_id: str, *, owner_member_id: str | None = None) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM practice_hypotheses WHERE hypothesis_id=? AND owner_member_id IS ?", (hypothesis_id, owner_member_id)).fetchone()
        if not row:
            raise ValueError("hypothesis_not_found")
        result = dict(row)
        result["observations"] = [dict(item) for item in self.db.execute("SELECT * FROM hypothesis_observations WHERE hypothesis_id=? AND owner_member_id IS ? ORDER BY created_at", (hypothesis_id, owner_member_id))]
        return result
