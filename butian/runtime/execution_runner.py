"""Synchronous business facade over a durable asynchronous execution owner."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict

from .execution_contract import ExecutionRequest, TERMINAL_STATES
from .execution_dispatch import ExecutionDispatch
from .execution_permit import request_digest


class ExecutionRunner:
    def __init__(self, dispatch: ExecutionDispatch):
        self.dispatch = dispatch
        self.adapter = dispatch.adapter
        self._lock = threading.RLock()
        self._active: set[str] = set()

    def run(self, request: ExecutionRequest, *, authorization_valid, on_observation=None):
        if request_digest(request) != request.request_sha256:
            raise ValueError('execution_request_digest_mismatch')
        self.dispatch.assert_not_cancelled(request.execution_id)
        if not authorization_valid():
            raise ValueError('execution_permit_authorization_revoked')
        permit = request.permit
        # The transaction-bound business validation happens before this network
        # call. The short lease bounds disconnection and revocation propagation.
        try:
            with self.dispatch._lock:
                self.dispatch.assert_not_cancelled(request.execution_id)
                self.adapter.authorize(permit['authorization_id'], permit['authorization_generation'], permit['expires_at'])
                with self._lock:
                    self._active.add(request.execution_id)
                observation = self.dispatch.start(request)
            lease_due = time.monotonic() + 2
            stop_sent = False
            reconciliation_deadline = time.monotonic() + request.timeout + 15
            waiter = threading.Event()
            while observation.state not in TERMINAL_STATES:
                if on_observation:
                    on_observation(observation)
                now = time.monotonic()
                if not authorization_valid() and not stop_sent:
                    self.adapter.revoke(permit['authorization_id'], permit['authorization_generation'])
                    stop_sent = True
                if now >= lease_due and not stop_sent:
                    # A fresh lease is granted only after business authorization
                    # is checked, never from a transport reconnect alone.
                    if authorization_valid():
                        self.adapter.authorize(permit['authorization_id'], permit['authorization_generation'], time.time() + 30)
                        self.adapter.renew(request.execution_id, 10)
                        lease_due = now + 2
                if now >= reconciliation_deadline:
                    raise RuntimeError('execution_owner_exit_unconfirmed')
                waiter.wait(0.02)
                observation = self.dispatch.reconcile(request.execution_id)
            return observation
        finally:
            # An unconfirmed execution remains in persistent dispatch and must
            # remain a shutdown blocker even after this caller stops waiting.
            try:
                confirmed = self.adapter.inspect(request.execution_id)
            except Exception:
                confirmed = None
            if confirmed is not None and confirmed.tree_exit_confirmed:
                with self._lock:
                    self._active.discard(request.execution_id)

    def cancel(self, execution_id: str, *, reason: str):
        observation = self.dispatch.cancel(execution_id, reason=reason)
        with self.dispatch._lock:
            dispatched = self.dispatch.db.execute(
                'SELECT 1 FROM execution_dispatch WHERE execution_id=?', (execution_id,)).fetchone()
        if dispatched is None:
            return observation
        deadline = time.monotonic() + 5
        while observation.state not in TERMINAL_STATES and time.monotonic() < deadline:
            threading.Event().wait(0.02)
            observation = self.dispatch.reconcile(execution_id)
        return observation

    def unresolved(self) -> list[str]:
        with self.dispatch._lock:
            rows = self.dispatch.db.execute("SELECT execution_id, last_state, delivery_state, last_observation FROM execution_dispatch WHERE last_state IN ('accepted','running','cancelling','unknown') OR delivery_state!='acknowledged'").fetchall()
            unresolved = set()
            for execution_id, state, delivery, payload in rows:
                confirmed = False
                if state == 'unknown' and delivery == 'acknowledged':
                    try:
                        observation = json.loads(payload)
                        confirmed = (observation.get('execution_id') == execution_id
                            and observation.get('state') == 'unknown'
                            and observation.get('tree_exit_confirmed') is True)
                    except (TypeError, ValueError, AttributeError):
                        pass
                if not confirmed:
                    unresolved.add(execution_id)
            with self._lock:
                return sorted(unresolved | self._active)

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            ids = self.unresolved()
            if not ids:
                return True
            for execution_id in ids:
                try:
                    observation = self.dispatch.reconcile(execution_id)
                    if observation.tree_exit_confirmed:
                        with self._lock:
                            self._active.discard(execution_id)
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                return not self.unresolved()
            threading.Event().wait(0.02)
