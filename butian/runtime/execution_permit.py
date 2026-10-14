"""Validate the signed business snapshot before an executor starts user code."""
from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path
from typing import Callable

from .execution_contract import ExecutionCapabilities, ExecutionRequest
from .execution_protocol import canonical

PERMIT_FIELDS = frozenset({
    'member_id', 'task_id', 'problem_revision', 'tool_id', 'version',
    'source_path', 'source_sha256', 'manifest_sha256', 'manifest', 'input_sha256',
    'permissions', 'authorization_id', 'authorization_generation',
    'expires_at', 'executor_id', 'instance_id', 'capability_sha256',
    'timeout', 'output_limit', 'work_budget_bytes',
})


def request_digest(request: ExecutionRequest) -> str:
    return hashlib.sha256(canonical({
        'execution_id': request.execution_id, 'argv': list(request.argv), 'cwd': request.cwd,
        'input_sha256': hashlib.sha256(request.stdin).hexdigest(),
        'timeout': request.timeout, 'output_limit': request.output_limit,
        'permit': dict(request.permit),
    })).hexdigest()


def capability_digest(capabilities: ExecutionCapabilities) -> str:
    from dataclasses import asdict
    return hashlib.sha256(canonical(asdict(capabilities))).hexdigest()


def validate_permit(request: ExecutionRequest, capabilities: ExecutionCapabilities,
                    authorization_active: Callable[[str, int], bool], *, now: float | None = None) -> None:
    capabilities.require()
    permit = request.permit
    if set(permit) != PERMIT_FIELDS:
        raise ValueError('execution_permit_fields_invalid')
    if request_digest(request) != request.request_sha256:
        raise ValueError('execution_request_digest_mismatch')
    expiry = permit['expires_at']
    current = time.time() if now is None else now
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= current or expiry - current > 60:
        raise ValueError('execution_permit_expired')
    if permit['executor_id'] != capabilities.executor_id or permit['instance_id'] != capabilities.instance_id:
        raise ValueError('execution_permit_identity_mismatch')
    if permit['capability_sha256'] != capability_digest(capabilities):
        raise ValueError('execution_permit_capabilities_changed')
    for name in ('member_id', 'task_id', 'tool_id'):
        if not isinstance(permit[name], str) or not permit[name].strip():
            raise ValueError('execution_permit_identity_invalid')
    for name in ('version', 'problem_revision', 'authorization_generation'):
        if isinstance(permit[name], bool) or not isinstance(permit[name], int) or permit[name] < 0:
            raise ValueError('execution_permit_revision_invalid')
    for name in ('source_sha256', 'manifest_sha256', 'input_sha256', 'capability_sha256'):
        digest = permit[name]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('execution_permit_hash_invalid')
    manifest = permit['manifest']
    if not isinstance(manifest, dict) or hashlib.sha256(canonical(manifest)).hexdigest() != permit['manifest_sha256']:
        raise ValueError('execution_permit_manifest_mismatch')
    isolation = manifest.get('isolation', {})
    if not isinstance(isolation, dict):
        raise ValueError('execution_permit_requirements_invalid')
    required = isolation.get('requires', [])
    if not isinstance(required, list) or any(not isinstance(name, str) or not name for name in required):
        raise ValueError('execution_permit_requirements_invalid')
    from .execution_contract import required_execution_features
    capabilities.require(required_execution_features(required))
    if manifest.get('permissions') != permit['permissions']:
        raise ValueError('execution_permit_permissions_mismatch')
    if hashlib.sha256(request.stdin).hexdigest() != permit['input_sha256']:
        raise ValueError('execution_permit_input_mismatch')
    if request.timeout != permit['timeout'] or request.output_limit != permit['output_limit']:
        raise ValueError('execution_permit_budget_mismatch')
    budget = permit['work_budget_bytes']
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0):
        raise ValueError('execution_permit_work_budget_invalid')
    if not isinstance(permit['permissions'], list) or any(not isinstance(x, str) for x in permit['permissions']):
        raise ValueError('execution_permit_permissions_invalid')
    auth_id = permit['authorization_id']
    if not isinstance(auth_id, str) or not auth_id or not authorization_active(auth_id, permit['authorization_generation']):
        raise ValueError('execution_permit_authorization_revoked')
    from .execution_pairing import validate_local_path
    source = Path(permit['source_path'])
    validate_local_path(source, 'execution_permit_source_invalid')
    cwd = Path(request.cwd)
    validate_local_path(cwd, 'execution_permit_cwd_invalid')
    if not cwd.is_absolute() or cwd.resolve() != cwd or not cwd.is_dir():
        raise ValueError('execution_permit_cwd_invalid')
    if not source.is_absolute() or source.is_symlink() or source.resolve() != source or not source.is_file():
        raise ValueError('execution_permit_source_invalid')
    if str(source) not in request.argv:
        raise ValueError('execution_permit_entrypoint_mismatch')
    if hashlib.sha256(source.read_bytes()).hexdigest() != permit['source_sha256']:
        raise ValueError('execution_permit_source_changed')
