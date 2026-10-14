import json
import pytest
from butian.mcp_server import call_tool
from butian.runtime.service import ButianService

def test_host_business_use_promotes_and_counterevidence_disputes_after_restart(tmp_path):
    service = ButianService(tmp_path)
    auth = dict(actor_member_id='worker', token='test-worker')
    try:
        service.add_member('worker', '执行者')
        service.store.set_member_token('worker', auth['token'])
        memory = service.memory('worker')
        try:
            memory.put('unit-method', 'lesson', '测量合并前先把厘米换算成米', status='candidate')
        finally:
            memory.close()
        for index, (centimeters, meters, outcome) in enumerate(((125, 1.25, 'success'), (230, 2.3, 'success'), (300, 30, 'failure'))):
            goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='测量单位换算', problem={'acceptance_criteria': ['一米等于一百厘米']}))
            package = service.export_execution(goal['task_id'], actor_member_id='worker')
            assert '厘米换算成米' in package['context']
            expected = centimeters / 100
            observed = meters
            assert (observed == expected) == (outcome == 'success')
            path = tmp_path / f'conversion-{index}.json'
            path.write_text(json.dumps(dict(centimeters=centimeters, observed_meters=observed, expected_meters=expected)), encoding='utf-8')
            evidence = service.record_evidence(path, owner_member_id='worker', actor_member_id='worker')
            submitted = call_tool(service, 'butian_execution_submit', dict(auth, task_id=goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id=f'conversion-{index}', output=path.read_text(), evidence_ids=[evidence], method_uses=[{'memory_id': 'unit-method', 'outcome': outcome, 'evidence_id': evidence}]))
            service.review_task(goal['task_id'], submitted['revision'], 'approve', reviewer_member_id='worker', actor_member_id='worker')
            handoff = json.loads(service.store.db.execute('SELECT payload FROM handoffs WHERE task_id=?', (goal['task_id'],)).fetchone()[0])
            assert handoff['method_uses'][0]['outcome'] == outcome
            memory = service.memory('worker')
            try:
                assert memory.get('unit-method')['status'] == ('candidate' if index == 0 else 'warm' if index == 1 else 'disputed')
            finally:
                memory.close()
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        memory = service.memory('worker')
        try:
            assert memory.get('unit-method')['status'] == 'disputed'
            assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='unit-method'").fetchone()[0] == 3
        finally:
            memory.close()
    finally:
        service.close()


def test_host_method_result_cannot_apply_to_replacement_content(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    auth = dict(actor_member_id='worker', token='test-worker')
    try:
        service.add_member('worker', 'Worker')
        service.store.set_member_token('worker', auth['token'])
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 100.', status='candidate')
        memory.close()
        goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='Unit conversion', problem={'acceptance_criteria': ['100 centimeters equals 1 meter']}))
        package = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        path = tmp_path / 'proof.txt'
        path.write_text('100 / 100 = 1', encoding='utf-8')
        evidence = service.record_evidence(path, owner_member_id='worker', actor_member_id='worker')
        submitted = call_tool(service, 'butian_execution_submit', dict(auth, task_id=goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='conversion', output='1 meter', evidence_ids=[evidence], method_uses=[dict(memory_id='method', outcome='success', evidence_id=evidence)]))
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 10.', status='candidate')
        memory.close()
        service.review_task(goal['task_id'], submitted['revision'], 'approve', reviewer_member_id='worker', actor_member_id='worker')
        memory = service.memory('worker')
        try:
            assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='method'").fetchone()[0] == 0
        finally:
            memory.close()
    finally:
        service.close()


def test_method_submission_does_not_mutate_request_and_replays(tmp_path, monkeypatch):
    import copy
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide by 100.', status='candidate')
        memory.close()
        service.store.set_member_token('worker', 'replay-token')
        goal = call_tool(service, 'butian_goal_submit', dict(actor_member_id='worker', token='replay-token', goal='Convert units', problem={'acceptance_criteria': ['100 cm = 1 m']}))
        package = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        proof = tmp_path / 'proof.txt'
        proof.write_text('100 cm = 1 m', encoding='utf-8')
        evidence = service.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
        args = dict(expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='method-replay', output='1 m', evidence_ids=[evidence], actor_member_id='worker', method_uses=[dict(memory_id='method', outcome='success', evidence_id=evidence)])
        original = copy.deepcopy(args)
        result = service.submit_execution(goal['task_id'], **args)
        assert args == original
        assert service.submit_execution(goal['task_id'], **original) == result
        assert service.submit_execution(goal['task_id'], **args) == result
    finally:
        service.close()


def test_legacy_method_outbox_without_version_cannot_change_memory(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Replacement method.', status='candidate')
        memory.close()
        proof = tmp_path / 'legacy-proof.txt'
        proof.write_text('Old method measurement', encoding='utf-8')
        evidence = service.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
        service.store.enqueue_member_memory('legacy-review-method', 'worker', 'method', 'review_method_practice', json.dumps(dict(outcome='success', evidence_id=evidence)), {}, [])
        result = service.drain_member_memory_outbox()
        memory = service.memory('worker')
        try:
            assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='method'").fetchone()[0] == 0
            assert any(item.get('error') == 'method_version_snapshot_missing' for item in result)
            assert any(item['status'] == 'failed' for item in result)
            assert service.drain_member_memory_outbox() == []
        finally:
            memory.close()
    finally:
        service.close()


def test_method_changed_between_export_and_submission_is_rejected(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.store.set_member_token('worker', 'snapshot-token')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 100.', status='candidate')
        memory.close()
        goal = call_tool(service, 'butian_goal_submit', dict(actor_member_id='worker', token='snapshot-token', goal='Convert centimeters', problem={'acceptance_criteria': ['100 cm = 1 m']}))
        package = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 10.', status='candidate')
        memory.close()
        proof = tmp_path / 'proof.txt'
        proof.write_text('100 cm = 1 m')
        evidence = service.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
        with pytest.raises(ValueError, match='execution_method_content_changed'):
            service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='stale-method', output='1 m', evidence_ids=[evidence], actor_member_id='worker', method_uses=[dict(memory_id='method', outcome='success', evidence_id=evidence)])
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()[0] == 'running'
    finally:
        service.close()

def test_method_archived_after_export_is_rejected(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.store.set_member_token('worker', 'snapshot-token')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 100.', status='candidate')
        memory.close()
        goal = call_tool(service, 'butian_goal_submit', dict(actor_member_id='worker', token='snapshot-token', goal='Convert centimeters', problem={'acceptance_criteria': ['100 cm = 1 m']}))
        package = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        memory = service.memory('worker')
        memory.db.execute("UPDATE memories SET status='archived' WHERE memory_id='method'")
        memory.db.commit()
        memory.close()
        proof = tmp_path / 'proof.txt'
        proof.write_text('100 cm = 1 m')
        evidence = service.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
        with pytest.raises(ValueError, match='execution_method_unavailable'):
            service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='archived-method', output='1 m', evidence_ids=[evidence], actor_member_id='worker', method_uses=[dict(memory_id='method', outcome='success', evidence_id=evidence)])
    finally:
        service.close()

def test_explicit_method_snapshot_survives_restart_and_repeat_export(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('worker', 'Worker')
    service.store.set_member_token('worker', 'snapshot-token')
    memory = service.memory('worker')
    memory.put('method', 'lesson', 'Divide centimeters by 100.', status='candidate')
    memory.close()
    goal = call_tool(service, 'butian_goal_submit', dict(actor_member_id='worker', token='snapshot-token', goal='Convert centimeters', problem={'acceptance_criteria': ['100 cm = 1 m']}))
    package = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
    service.close()
    service = ButianService(tmp_path)
    try:
        repeated = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        assert repeated['method_snapshot'] == package['method_snapshot']
        assert repeated['task_revision'] == package['task_revision']
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 10.', status='candidate')
        memory.close()
        with pytest.raises(ValueError, match='execution_method_snapshot_changed_requires_restart'):
            service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        row = service.store.db.execute('SELECT hashes FROM execution_method_snapshots WHERE task_id=? AND task_revision=?', (goal['task_id'], package['task_revision'])).fetchone()
        assert json.loads(row['hashes']) == package['method_snapshot']
    finally:
        service.close()

def test_method_invalidated_after_export_is_rejected(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.store.set_member_token('worker', 'snapshot-token')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 100.', status='candidate')
        memory.close()
        goal = call_tool(service, 'butian_goal_submit', dict(actor_member_id='worker', token='snapshot-token', goal='Convert centimeters', problem={'acceptance_criteria': ['100 cm = 1 m']}))
        package = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        memory = service.memory('worker')
        memory.db.execute("UPDATE memories SET status='invalidated' WHERE memory_id='method'")
        memory.db.commit()
        memory.close()
        proof = tmp_path / 'proof.txt'
        proof.write_text('100 cm = 1 m')
        evidence = service.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
        with pytest.raises(ValueError, match='execution_method_unavailable'):
            service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='archived-method', output='1 m', evidence_ids=[evidence], actor_member_id='worker', method_uses=[dict(memory_id='method', outcome='success', evidence_id=evidence)])
    finally:
        service.close()


@pytest.mark.parametrize('field,value', [('actor_member_id', 'different-member'), ('problem_revision', 999)])
def test_method_snapshot_identity_and_problem_are_bound(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.store.set_member_token('worker', 'snapshot-token')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Divide centimeters by 100.', status='candidate')
        memory.close()
        goal = call_tool(service, 'butian_goal_submit', dict(actor_member_id='worker', token='snapshot-token', goal='Convert centimeters', problem={'acceptance_criteria': ['100 cm = 1 m']}))
        package = service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['method'])
        service.store.db.execute(f'UPDATE execution_method_snapshots SET {field}=? WHERE task_id=?', (value, goal['task_id']))
        proof = tmp_path / 'proof.txt'
        proof.write_text('100 cm = 1 m')
        evidence = service.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
        with pytest.raises(ValueError, match='execution_method_snapshot_binding_changed'):
            service.submit_execution(goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='bound-method', output='1 m', evidence_ids=[evidence], actor_member_id='worker', method_uses=[dict(memory_id='method', outcome='success', evidence_id=evidence)])
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()[0] == 'running'
    finally:
        service.close()


def test_explicit_method_budget_rejection_preserves_task(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.store.set_member_token('worker', 'budget-test-token')
        memory = service.memory('worker')
        memory.put('large-method', 'lesson', 'x' * 12000, status='candidate')
        memory.close()
        goal = call_tool(service, 'butian_goal_submit', dict(actor_member_id='worker', token='budget-test-token', goal='Convert units', problem={'acceptance_criteria': ['100 cm = 1 m']}))
        before = dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone())
        with pytest.raises(ValueError, match='execution_method_context_budget_exceeded'):
            service.export_execution(goal['task_id'], actor_member_id='worker', method_ids=['large-method'])
        after = dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone())
        assert after == before
    finally:
        service.close()
