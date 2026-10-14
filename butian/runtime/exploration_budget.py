"""Persistent bounded exploration ledger; costs are declared budget units, not money.

A budget reservation does not confer OS, tool or external-state permissions.
Started attempts retain their charge even if execution fails or becomes unknown.
"""
from __future__ import annotations
import json
import math
import time


class ExplorationBudget:
    DEFAULT = dict(version=1, cost_unit='declared_exploration_unit', max_cost=50,
                   max_actions=50, max_risk=0.2, window_seconds=86400)

    def __init__(self, db):
        self.db = db
        db.execute('CREATE TABLE IF NOT EXISTS exploration_policy (id INTEGER PRIMARY KEY CHECK(id=1), snapshot TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS exploration_member_policy (owner TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS exploration_reservations (action_id TEXT PRIMARY KEY, owner TEXT NOT NULL, cost REAL NOT NULL, risk REAL NOT NULL, started_at REAL NOT NULL, policy_snapshot TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS exploration_legacy_imports (owner TEXT PRIMARY KEY, imported_at REAL NOT NULL)')
        db.execute('INSERT OR IGNORE INTO exploration_policy VALUES (1,?)', (json.dumps(self.DEFAULT, sort_keys=True),))

    def policy(self, owner=None):
        if owner:
            row = self.db.execute('SELECT snapshot FROM exploration_member_policy WHERE owner=?', (owner,)).fetchone()
            if row:
                value = json.loads(row[0])
                self._validate(value)
                return value
        value = json.loads(self.db.execute('SELECT snapshot FROM exploration_policy WHERE id=1').fetchone()[0])
        self._validate(value)
        return value

    @staticmethod
    def _validate(value):
        for key in ('max_cost', 'max_risk', 'window_seconds'):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError('invalid_exploration_budget')
        count = value.get('max_actions')
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or value['max_cost'] < 0 or not 0 <= value['max_risk'] <= 1 or value['window_seconds'] <= 0:
            raise ValueError('invalid_exploration_budget')

    def configure(self, *, max_cost, max_actions, max_risk, window_seconds, owner=None):
        policy = dict(self.DEFAULT, max_cost=max_cost, max_actions=max_actions,
                      max_risk=max_risk, window_seconds=window_seconds)
        self._validate(policy)
        if owner:
            self.db.execute('INSERT INTO exploration_member_policy VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET snapshot=excluded.snapshot', (owner, json.dumps(policy, sort_keys=True)))
        else:
            self.db.execute('UPDATE exploration_policy SET snapshot=? WHERE id=1', (json.dumps(policy, sort_keys=True),))
        return policy

    def import_legacy_member(self, legacy_db, owner):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("legacy_budget_owner_required")
        if self.db.execute("SELECT 1 FROM exploration_legacy_imports WHERE owner=?", (owner,)).fetchone():
            return
        tables = {row[0] for row in legacy_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'exploration_policy', 'exploration_reservations'} <= tables:
            return
        policy_row = legacy_db.execute("SELECT snapshot FROM exploration_policy WHERE id=1").fetchone()
        rows = legacy_db.execute("SELECT action_id,owner,cost,risk,started_at,policy_snapshot FROM exploration_reservations").fetchall()
        for row in rows:
            action_id, old_owner, cost, risk, started_at, snapshot = row
            if not isinstance(action_id, str) or not action_id or old_owner not in (owner, '__team__'):
                raise ValueError("invalid_legacy_budget_reservation")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (cost, risk, started_at)) or cost < 0 or started_at < 0 or not 0 <= risk <= 1:
                raise ValueError("invalid_legacy_budget_reservation")
            self._validate(json.loads(snapshot))
        self.db.execute("SAVEPOINT import_legacy_budget")
        try:
            self.db.execute("UPDATE exploration_policy SET snapshot=snapshot WHERE id=1")
            if self.db.execute("SELECT 1 FROM exploration_legacy_imports WHERE owner=?", (owner,)).fetchone():
                self.db.execute("RELEASE import_legacy_budget")
                return
            if policy_row:
                incoming = json.loads(policy_row[0]); self._validate(incoming)
                existing = self.db.execute("SELECT snapshot FROM exploration_member_policy WHERE owner=?", (owner,)).fetchone()
                if existing and json.loads(existing[0]) != incoming:
                    raise ValueError("legacy_budget_policy_conflict")
                self.db.execute("INSERT OR IGNORE INTO exploration_member_policy VALUES (?,?)", (owner, json.dumps(incoming, sort_keys=True)))
            for row in rows:
                action_id, old_owner, cost, risk, started_at, snapshot = row
                target = self.db.execute("SELECT owner,cost,risk,started_at,policy_snapshot FROM exploration_reservations WHERE action_id=?", (action_id,)).fetchone()
                values = (owner, cost, risk, started_at, snapshot)
                if target and tuple(target) != values:
                    raise ValueError("legacy_budget_reservation_conflict")
                self.db.execute("INSERT OR IGNORE INTO exploration_reservations(action_id,owner,cost,risk,started_at,policy_snapshot) VALUES (?,?,?,?,?,?)", (action_id, *values))
            self.db.execute("INSERT INTO exploration_legacy_imports VALUES (?,?)", (owner, time.time()))
            self.db.execute("RELEASE import_legacy_budget")
        except Exception:
            self.db.execute("ROLLBACK TO import_legacy_budget"); self.db.execute("RELEASE import_legacy_budget"); raise
    def reserve(self, action_id, owner, *, cost, risk, now=None):
        now = time.time() if now is None else now
        if not isinstance(action_id, str) or not action_id or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (cost, risk, now)) or cost < 0 or not 0 <= risk <= 1:
            raise ValueError('invalid_exploration_metrics')
        owner = owner or '__team__'
        self.db.execute('SAVEPOINT exploration_reserve')
        try:
            # Acquire the writer lock before any snapshot or reservation read.
            self.db.execute('UPDATE exploration_policy SET snapshot=snapshot WHERE id=1')
            previous = self.db.execute('SELECT owner,cost,risk FROM exploration_reservations WHERE action_id=?', (action_id,)).fetchone()
            if previous:
                if tuple(previous) != (owner, cost, risk):
                    raise ValueError('exploration_reservation_conflict')
            else:
                policy = self.policy(owner)
                if risk > policy['max_risk']:
                    raise ValueError('exploration_risk_exceeded')
                count, used = self.db.execute('SELECT COUNT(*),COALESCE(SUM(cost),0) FROM exploration_reservations WHERE owner=? AND started_at>?', (owner, now-policy['window_seconds'])).fetchone()
                if count >= policy['max_actions'] or used + cost > policy['max_cost']:
                    raise ValueError('exploration_budget_exceeded')
                self.db.execute('INSERT INTO exploration_reservations VALUES (?,?,?,?,?,?)', (action_id, owner, cost, risk, now, json.dumps(policy, sort_keys=True)))
            self.db.execute('RELEASE SAVEPOINT exploration_reserve')
        except Exception:
            self.db.execute('ROLLBACK TO SAVEPOINT exploration_reserve')
            self.db.execute('RELEASE SAVEPOINT exploration_reserve')
            raise
