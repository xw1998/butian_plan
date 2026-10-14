"""Load only explicitly selected executor metadata; never try another backend."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .execution_pairing import load_pairing, validate_local_path
from .execution_protocol import MessageVerifier
from .execution_transport import LoopbackExecutionClient


def configured_executor(home: Path, connection: str):
    path = Path(connection)
    validate_local_path(path, 'executor_connection_path_invalid')
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError('executor_connection_path_invalid')
    try:
        metadata = json.loads(path.read_text(encoding='utf-8'))
        if set(metadata) != {'version', 'executor_id', 'instance_id', 'endpoint'} or metadata['version'] != 1:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('executor_connection_metadata_invalid') from None
    identity, key = load_pairing(path.parent / 'pairing.bin')
    if identity != metadata['executor_id']:
        raise ValueError('executor_pairing_identity_mismatch')
    client_root = Path(home) / 'executor_clients'
    validate_local_path(client_root, 'executor_client_home_invalid')
    if client_root.is_symlink():
        raise ValueError('executor_client_home_invalid')
    client_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    import hashlib
    filename = hashlib.sha256(identity.encode()).hexdigest() + '.db'
    db_path = client_root / filename
    validate_local_path(db_path, 'executor_client_database_invalid')
    if db_path.is_symlink():
        raise ValueError('executor_client_database_invalid')
    db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    try:
        client = LoopbackExecutionClient(metadata['endpoint'], MessageVerifier(db, key, identity, metadata['instance_id']))
        actual = client.capabilities()
        if actual.executor_id != identity or actual.instance_id != metadata['instance_id']:
            raise ValueError('executor_connection_identity_mismatch')
        client._owned_database = db
        return client
    except BaseException:
        db.close()
        raise
