"""In-process execution owner for tools and local providers on this machine.

补天 needs a durable answer to "did it run, and with what output": the ledger,
the deadline, the output limit, process-tree stop and confirmed exit. A loopback
HTTP endpoint, a pairing key and a signed envelope exist for an executor on
another machine; on this machine they buy nothing, so the in-process owner is
the default and the paired endpoint stays an explicit opt-in.
"""
from __future__ import annotations

from pathlib import Path

from .execution_owner import LocalExecutionOwner
from .execution_self_test import verify_process_control

EXECUTOR_ID = 'local-in-process'

def build(store, home: Path):
    """Attach an in-process owner; refuse to continue without a real self-test."""
    owner = LocalExecutionOwner(store.db, EXECUTOR_ID, authorization_active=lambda *_: True)
    root = Path(home) / 'executor_self_test'
    root.mkdir(parents=True, exist_ok=True)
    verified = verify_process_control(owner, root)
    if not verified.get('verified'):
        owner.close()
        raise ValueError('local_executor_self_test_failed')
    return owner
