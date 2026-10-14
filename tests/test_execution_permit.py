import hashlib
from dataclasses import replace

import pytest

from butian.runtime.execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionRequest, REQUIRED_CONTROL
from butian.runtime.execution_permit import capability_digest, request_digest, validate_permit


def fixture(tmp_path):
    source = (tmp_path / 'tool.py').resolve()
    source.write_text('print(1)', encoding='utf-8')
    caps = ExecutionCapabilities('executor', 'instance', {
        name: CapabilityEvidence('supported', 'explicit_self_test', 1, 'instance')
        for name in REQUIRED_CONTROL})
    permit = dict(member_id='m1', task_id='t1', problem_revision=1, tool_id='tool', version=1,
                  source_path=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  manifest_sha256=hashlib.sha256(b'{"permissions":["cpu"]}').hexdigest(), manifest={'permissions': ['cpu']}, input_sha256=hashlib.sha256(b'{}').hexdigest(),
                  permissions=['cpu'], authorization_id='auth', authorization_generation=1,
                  expires_at=130, executor_id='executor', instance_id='instance',
                  capability_sha256=capability_digest(caps), timeout=10, output_limit=1024,
                  work_budget_bytes=None)
    request = ExecutionRequest('e1', '0' * 64, ('python', str(source)), str(tmp_path), b'{}', 10, 1024, permit)
    return replace(request, request_sha256=request_digest(request)), caps, source


def test_valid_permit_checks_authorization_and_source(tmp_path):
    request, caps, _ = fixture(tmp_path)
    seen = []
    validate_permit(request, caps, lambda identity, generation: seen.append((identity, generation)) or True, now=100)
    assert seen == [('auth', 1)]


def test_revocation_denies_start(tmp_path):
    request, caps, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='execution_permit_authorization_revoked'):
        validate_permit(request, caps, lambda *_: False, now=100)


def test_source_change_denies_start(tmp_path):
    request, caps, source = fixture(tmp_path)
    source.write_text('print(2)', encoding='utf-8')
    with pytest.raises(ValueError, match='execution_permit_source_changed'):
        validate_permit(request, caps, lambda *_: True, now=100)


def test_budget_change_denies_start(tmp_path):
    request, caps, _ = fixture(tmp_path)
    changed = replace(request, timeout=20)
    changed = replace(changed, request_sha256=request_digest(changed))
    with pytest.raises(ValueError, match='execution_permit_budget_mismatch'):
        validate_permit(changed, caps, lambda *_: True, now=100)


def test_changed_input_and_expired_permit_denied(tmp_path):
    request, caps, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='execution_request_digest_mismatch'):
        validate_permit(replace(request, stdin=b'other'), caps, lambda *_: True, now=100)
    with pytest.raises(ValueError, match='execution_permit_expired'):
        validate_permit(request, caps, lambda *_: True, now=131)


def test_manifest_snapshot_hash_is_rechecked_before_start(tmp_path):
    request, caps, _ = fixture(tmp_path)
    permit = {**request.permit, 'manifest': {'permissions': ['network']}}
    request = replace(request, permit=permit)
    request = replace(request, request_sha256=request_digest(request))
    with pytest.raises(ValueError, match='execution_permit_manifest_mismatch'):
        validate_permit(request, caps, lambda *_: True, now=100)


def test_executor_denies_unverified_manifest_requirement(tmp_path):
    from butian.runtime.execution_protocol import canonical
    request, caps, _ = fixture(tmp_path)
    manifest = {'permissions': ['cpu'], 'isolation': {'requires': ['network_isolation']}}
    permit = {**request.permit, 'manifest': manifest,
              'manifest_sha256': hashlib.sha256(canonical(manifest)).hexdigest()}
    request = replace(request, permit=permit)
    request = replace(request, request_sha256=request_digest(request))
    with pytest.raises(ValueError, match='execution_capabilities_missing:network_isolation'):
        validate_permit(request, caps, lambda *_: True, now=100)


@pytest.mark.parametrize('target', ['source_parent', 'working_directory'])
def test_permit_rejects_reparse_ancestors_before_start(tmp_path, monkeypatch, target):
    from pathlib import Path
    from types import SimpleNamespace
    request, caps, source = fixture(tmp_path)
    flagged = source.parent if target == 'source_parent' else tmp_path / 'work'
    if target == 'working_directory':
        flagged.mkdir()
        request = replace(request, cwd=str(flagged))
        request = replace(request, request_sha256=request_digest(request))
    original = Path.lstat
    def observed(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == flagged:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    monkeypatch.setattr(Path, 'lstat', observed)
    with pytest.raises(ValueError, match='execution_permit_(source|cwd)_invalid'):
        validate_permit(request, caps, lambda *_: True, now=100)
