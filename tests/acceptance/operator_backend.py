"""Explicit operator-terminal dependency injection; never enabled by discovery."""
from __future__ import annotations

import os
import json
import sqlite3
import threading
from pathlib import Path

from butian.runtime.execution_dispatch import ExecutionDispatch
from butian.runtime.execution_owner import LocalExecutionOwner
from butian.runtime.execution_protocol import MessageVerifier
from butian.runtime.execution_runner import ExecutionRunner
from butian.runtime.execution_self_test import verify_process_control
from butian.runtime.execution_transport import LoopbackExecutionClient, LoopbackExecutionServer


class OperatorBackend:
    def __init__(self, root: Path, *, operator_confirm: bool = False):
        if os.name != 'nt' or (operator_confirm is not True and os.environ.get('BUTIAN_OPERATOR_ACCEPTANCE') != '1'):
            raise ValueError('operator_backend_requires_explicit_terminal_confirmation')
        root.mkdir(parents=True, exist_ok=False)
        self.db = sqlite3.connect(root / 'executor.db', isolation_level=None, check_same_thread=False)
        self.owner = self.server = self.thread = self.client_db = None
        try:
            self.owner = LocalExecutionOwner(self.db, 'operator-business-tests', authorization_active=lambda *_: True)
            verified = verify_process_control(self.owner, root)
            if not verified['verified']:
                raise RuntimeError('operator_business_control_self_test_failed')
            from butian.runtime.execution_pairing import create_pairing, load_pairing
            create_pairing(root / 'pairing.bin', self.owner.executor_id)
            identity, key = load_pairing(root / 'pairing.bin')
            if identity != self.owner.executor_id:
                raise ValueError('operator_backend_pairing_identity_mismatch')
            self.server = LoopbackExecutionServer(('127.0.0.1', 0), self.owner,
                MessageVerifier(self.db, key, self.owner.executor_id, self.owner.instance_id))
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.client_db = sqlite3.connect(root / 'client.db', isolation_level=None, check_same_thread=False)
            self.client = LoopbackExecutionClient('http://127.0.0.1:' + str(self.server.server_port),
                MessageVerifier(self.client_db, key, self.owner.executor_id, self.owner.instance_id))
            self.client.capabilities().require()
            self.connection_path = (root / 'connection.json').resolve()
            with self.connection_path.open('x', encoding='utf-8') as metadata:
                json.dump({'version': 1, 'executor_id': self.owner.executor_id,
                    'instance_id': self.owner.instance_id,
                    'endpoint': 'http://127.0.0.1:' + str(self.server.server_port)}, metadata)
        except BaseException as initialization_error:
            owner_confirmed = self.owner is None
            try:
                if self.owner is not None:
                    self.owner.close()
                    owner_confirmed = True
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as cleanup_error:
                initialization_error.add_note('owner_cleanup_unconfirmed:' + type(cleanup_error).__name__)
            try:
                if self.server is not None:
                    if self.thread is not None and self.thread.is_alive():
                        self.server.shutdown()
                    self.server.server_close()
                if self.thread is not None:
                    self.thread.join(timeout=5)
                    if self.thread.is_alive():
                        raise RuntimeError('operator_backend_shutdown_unconfirmed')
                if self.client_db is not None:
                    self.client_db.close()
                if owner_confirmed:
                    self.db.close()
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as cleanup_error:
                initialization_error.add_note('backend_cleanup_unconfirmed:' + type(cleanup_error).__name__)
            raise

    def attach(self, service):
        if service.execution_runner is not None:
            raise ValueError('operator_backend_cannot_replace_configured_executor')
        runner = ExecutionRunner(ExecutionDispatch(service.store.db, self.client, write_lock=service.store._write_lock))
        service.execution_runner = runner
        service.tools.execution_runner = runner
        from butian.adapters.local import LocalCommandProvider
        from butian.runtime.provider_execution import provider_request_factory
        factory = provider_request_factory(service.store, service.home, runner)
        for provider_id in service.providers.list():
            provider = service.providers.get(provider_id)
            if isinstance(provider, LocalCommandProvider):
                provider.execution_runner = runner
                provider.execution_request_factory = factory
        return runner

    def close(self):
        owner_error = None
        try:
            self.owner.close()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            owner_error = exc
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('operator_backend_shutdown_unconfirmed') from owner_error
        self.client_db.close()
        # Keep the owner's ledger open while ownership remains unresolved.
        if owner_error is not None:
            raise owner_error
        self.db.close()
