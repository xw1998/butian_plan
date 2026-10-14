import pytest
from test_tools import make_service, propose, MANIFEST, execute_in_task, execution_task_id


def test_tool_verification_rejects_missing_and_changed_evidence(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('measured 42')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        run = execute_in_task(service, 'm1', tool_id, 1, 'measurement', verification_only=True)
        args = dict(independent_key='measurement', execution_id=run['execution_id'], input_hash=run['input_hash'], expected_output=run['expected_output'])
        with pytest.raises(ValueError, match='evidence_not_found'):
            service.verify_tool('m1', tool_id, 1, 'success', evidence_ids=['invented-proof'], **args)
        proof = tmp_path / 'measurement.txt'
        proof.write_text('measured 42\n', encoding='utf-8')
        eid = service.record_evidence(proof, owner_member_id='m1', actor_member_id='m1')
        proof.write_text('changed result\n', encoding='utf-8')
        with pytest.raises(ValueError, match='verification_evidence_invalid'):
            service.verify_tool('m1', tool_id, 1, 'success', evidence_ids=[eid], **args)
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_verifications WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
    finally:
        service.close()


def test_tool_verification_rejects_foreign_and_expired_evidence(tmp_path):
    import time
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('42')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        run = execute_in_task(service, 'm1', tool_id, 1, 'measurement', verification_only=True)
        args = dict(independent_key='measurement', execution_id=run['execution_id'], input_hash=run['input_hash'], expected_output=run['expected_output'])
        proof = tmp_path / 'foreign.txt'
        proof.write_text('42', encoding='utf-8')
        foreign = service.record_evidence(proof, owner_member_id='m2', actor_member_id='m2')
        with pytest.raises(ValueError, match='evidence_access_denied'):
            service.verify_tool('m1', tool_id, 1, 'success', evidence_ids=[foreign], **args)
        own = service.record_evidence(proof, owner_member_id='m1', actor_member_id='m1', expires_at=time.time() + 60)
        service.store.db.execute('UPDATE evidence SET expires_at=? WHERE evidence_id=?', (time.time() - 1, own))
        with pytest.raises(ValueError, match='verification_evidence_invalid'):
            service.verify_tool('m1', tool_id, 1, 'success', evidence_ids=[own], **args)
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_verifications WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
    finally:
        service.close()


def test_shared_structured_output_trial_uses_output_contract(tmp_path):
    from test_tools import registered_tool_proofs
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        manifest = {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py'], 'risk_level': 'low', 'output_contract': {'type': 'object', 'required': ['value'], 'properties': {'value': {'type': 'integer'}}}}
        service.add_tool_version('m1', tool_id, 'print(\'{"value":42}\')', manifest)
        for index in range(4):
            run = execute_in_task(service, 'm1', tool_id, 1, str(index), verification_only=True)
            assert run['expected_output'] == {'value': 42}
            service.verify_tool('m1', tool_id, 1, 'success', independent_key=str(index), task_id=execution_task_id(service, run['execution_id']), environment={'kind': str(index)}, evidence_ids=registered_tool_proofs(service, 'm1', [str(index)]), execution_id=run['execution_id'], input_hash=run['input_hash'], expected_output={'value': 42})
        share = service.share_tool('m1', tool_id, 'm2', permission='team_execute')
        service.accept_tool_share('m2', share['share_id'])
        run = execute_in_task(service, 'm2', tool_id, 1, 'recipient', share_id=share['share_id'])
        result = service.trial_tool('m2', share['share_id'], 'success', independent_key='recipient', evidence_ids=registered_tool_proofs(service, 'm2', ['recipient']), execution_id=run['execution_id'], input_hash=run['input_hash'], expected_output={'value': 42})
        assert result['outcome'] == 'success'
    finally:
        service.close()


def test_repeated_execution_cannot_inflate_independent_successes(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('42')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        run = execute_in_task(service, 'm1', tool_id, 1, 'measurement', verification_only=True)
        proof = tmp_path / 'one-measurement.txt'
        proof.write_text('42', encoding='utf-8')
        eid = service.record_evidence(proof, owner_member_id='m1', actor_member_id='m1')
        for index in range(4):
            service.verify_tool('m1', tool_id, 1, 'success', independent_key=f'relabel-{index}', task_id=execution_task_id(service, run['execution_id']), environment={'kind': str(index)}, evidence_ids=[eid], execution_id=run['execution_id'], input_hash=run['input_hash'], expected_output=run['expected_output'])
        stats = service.tools.stats(tool_id, 1)
        assert stats['successes'] == 1
        assert stats['evidence_bound_successes'] == 1
        assert stats['environment_count'] == 1
        assert stats['independent_task_count'] <= 1
        assert service.tools._tool(tool_id)['status'] != 'active'
    finally:
        service.close()


def test_trial_idempotency_rejects_changed_execution_binding(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        share = {'share_id': 'test-share', 'tool_id': tool_id, 'version': 1}
        args = dict(execution_id='execution-a', input_hash='input-a', expected_output=42)
        first = service.tools._record_trial(share, 'm2', 'success', 'same-key', None, {}, ['proof'], **args)
        replay = service.tools._record_trial(share, 'm2', 'success', 'same-key', None, {}, ['proof'], **args)
        assert replay['verification_id'] == first['verification_id']
        for changed in ({'execution_id': 'execution-b'}, {'input_hash': 'input-b'}, {'expected_output': 43}):
            with pytest.raises(ValueError, match='verification_idempotency_conflict'):
                service.tools._record_trial(share, 'm2', 'success', 'same-key', None, {}, ['proof'], **{**args, **changed})
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_verifications WHERE tool_id=?', (tool_id,)).fetchone()[0] == 1
    finally:
        service.close()


def test_verification_rejects_invented_task_binding(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('42')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        run = service.execute_tool('m1', tool_id, 1, 'measurement', verification_only=True)
        proof = tmp_path / 'measured-result.txt'
        proof.write_text('42', encoding='utf-8')
        eid = service.record_evidence(proof, owner_member_id='m1', actor_member_id='m1')
        with pytest.raises(ValueError, match='verification_task_access_denied'):
            service.verify_tool('m1', tool_id, 1, 'success', independent_key='invented-task', task_id='not-a-real-task', evidence_ids=[eid], execution_id=run['execution_id'], input_hash=run['input_hash'], expected_output=run['expected_output'])
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_verifications WHERE tool_id=?', (tool_id,)).fetchone()[0] == 0
    finally:
        service.close()


def test_stats_excludes_success_after_evidence_becomes_stale(tmp_path):
    service = make_service(tmp_path)
    try:
        tool_id = propose(service)['tool_id']
        service.add_tool_version('m1', tool_id, "print('42')", {**MANIFEST, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py']})
        run = execute_in_task(service, 'm1', tool_id, 1, 'measurement', verification_only=True)
        proof = tmp_path / 'measured-result.txt'
        proof.write_text('42', encoding='utf-8')
        eid = service.record_evidence(proof, owner_member_id='m1', actor_member_id='m1')
        service.verify_tool('m1', tool_id, 1, 'success', independent_key='measured', task_id=execution_task_id(service, run['execution_id']), evidence_ids=[eid], execution_id=run['execution_id'], input_hash=run['input_hash'], expected_output=run['expected_output'])
        assert service.tools.stats(tool_id, 1)['successes'] == 1
        proof.write_text('43', encoding='utf-8')
        assert service.tools.stats(tool_id, 1)['successes'] == 0
        assert service.store.db.execute('SELECT COUNT(*) FROM tool_verifications WHERE tool_id=?', (tool_id,)).fetchone()[0] == 1
    finally:
        service.close()
