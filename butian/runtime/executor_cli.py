"""Operator-only executor commands; no model/provider configuration is loaded."""
from __future__ import annotations

import json
import os
import tempfile
import sqlite3
from pathlib import Path

from .execution_pairing import create_pairing, load_pairing, validate_local_path
from .execution_protocol import MessageVerifier
from .execution_transport import LoopbackExecutionServer
from .execution_owner import LocalExecutionOwner


def run_executor(args, home: Path) -> int:
    root = home / 'executor'
    validate_local_path(root, 'executor_home_invalid')
    if root.is_symlink():
        raise ValueError('executor_home_invalid')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential = root / 'pairing.bin'
    if args.executor_command == 'pair':
        result = create_pairing(credential, args.executor_id)
        print(json.dumps({'status': 'ok', 'data': result}))
        return 0
    identity, key = load_pairing(credential)
    validate_local_path(root / 'executor.db', 'executor_database_path_invalid')
    db = sqlite3.connect(root / 'executor.db', isolation_level=None, check_same_thread=False)
    # Pair-authenticated short-lived leases are checked by the owner; business
    # authorization must be revalidated before granting or renewing each lease.
    owner = LocalExecutionOwner(db, identity, authorization_active=lambda *_: True)
    server = None
    try:
        if args.self_test:
            from .execution_self_test import verify_process_control
            result = verify_process_control(owner, root)
            print(json.dumps({'status': 'ok' if result['verified'] else 'error', 'data': result}))
            if not result['verified']:
                return 1
        if args.executor_command == 'self-test':
            return 0
        server = LoopbackExecutionServer(('127.0.0.1', args.port), owner,
                                         MessageVerifier(db, key, identity, owner.instance_id))
        metadata = {'version': 1, 'executor_id': identity, 'instance_id': owner.instance_id,
                    'endpoint': 'http://127.0.0.1:' + str(server.server_port)}
        # Public metadata carries no pairing key or member credentials.
        metadata_path = root / 'connection.json'
        validate_local_path(metadata_path, 'executor_connection_path_invalid')
        if metadata_path.is_symlink():
            raise ValueError('executor_connection_path_invalid')
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root,
                                         prefix='connection-', suffix='.tmp', delete=False) as output:
            json.dump(metadata, output)
            output.flush()
            os.fsync(output.fileno())
            temporary_path = output.name
        os.replace(temporary_path, metadata_path)
        print(json.dumps({'status': 'ok', 'data': metadata}), flush=True)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.server_close()
        owner.close()
        db.close()
