import json
from butian.runtime.service import ButianService


def test_cold_memory_dispatch_is_durable_and_does_not_promote(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        try:
            memory.put('method', 'lesson', 'Divide centimeters by 100; reject nonnumeric input.', status='candidate')
            memory.cool('method')
        finally:
            memory.close()
        for _ in range(2):
            service._maintain_member_memories('worker')
        rows = service.store.db.execute("SELECT * FROM tasks WHERE assignee_id='worker'").fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0]['payload'])
        assert payload['memory_review_id'] == 'method'
        assert payload['execution_mode'] == 'host'
        assert payload['requires_evidence'] is True
        assert payload['reproduction_steps'] and payload['success_criteria'] and payload['failure_criteria']
        assert json.loads(rows[0]['authorization_snapshot'])['member_id'] == 'worker'
        task_id = rows[0]['task_id']
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        service._maintain_member_memories('worker')
        assert service.store.db.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id='worker'").fetchone()[0] == 1
        assert service.store.db.execute('SELECT task_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        memory = service.memory('worker')
        try:
            assert memory.get('method')['status'] == 'cold'
        finally:
            memory.close()
    finally:
        service.close()

def test_review_audit_failure_does_not_leave_task_or_dedup_marker(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        try:
            memory.put('method', 'lesson', 'Test a bounded claim.', status='candidate')
            memory.cool('method')
        finally:
            memory.close()
        service.store.db.execute("CREATE TRIGGER reject_review BEFORE INSERT ON events WHEN NEW.event_type='memory_review_due' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            service._maintain_member_memories('worker')
        assert service.store.db.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id='worker'").fetchone()[0] == 0
        assert not service.store.db.in_transaction
        service.store.db.execute('DROP TRIGGER reject_review')
        service._maintain_member_memories('worker')
        assert service.store.db.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id='worker'").fetchone()[0] == 1
    finally:
        service.close()

def test_review_handoff_requires_bound_memory_observation(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        try:
            memory.put('method', 'lesson', 'Test a bounded claim.', status='candidate')
            memory.cool('method')
        finally:
            memory.close()
        service._maintain_member_memories('worker')
        task = service.store.db.execute("SELECT * FROM tasks WHERE assignee_id='worker'").fetchone()
        for revision, state in ((1, 'ready'), (2, 'claimed'), (3, 'running')):
            service.store.transition_task(task['task_id'], revision, state, 'review-bound-' + state)
        proof_path = tmp_path / 'review-proof.txt'
        proof_path.write_text('Independent test did not support the candidate.', encoding='utf-8')
        proof = service.record_evidence(proof_path, owner_member_id='worker', actor_member_id='worker')
        handoff = dict(task_id=task['task_id'], task_revision=4, member_id='worker', status='done', summary='reviewed', evidence_ids=[proof], next_action='review', confidence=0.5)
        with pytest.raises(ValueError, match='memory_review_observation_required'):
            service.submit_handoff(handoff)
        handoff['method_uses'] = [dict(memory_id='method', outcome='failure', evidence_id=proof)]
        assert service.submit_handoff(handoff)
        memory = service.memory('worker')
        try:
            memory.put('method', 'lesson', 'Changed claim requires a new test.', status='candidate')
        finally:
            memory.close()
        with pytest.raises(ValueError, match='memory_review_content_changed'):
            service.submit_handoff(handoff)
    finally:
        service.close()

def test_review_rejects_content_changed_after_handoff(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Original bounded claim.', status='candidate')
        memory.cool('method')
        memory.close()
        service._maintain_member_memories('worker')
        task = service.store.db.execute("SELECT * FROM tasks WHERE assignee_id='worker'").fetchone()
        for revision, state in ((1, 'ready'), (2, 'claimed'), (3, 'running')):
            service.store.transition_task(task['task_id'], revision, state, 'changed-' + state)
        path = tmp_path / 'proof.txt'
        path.write_text('Original claim refuted by independent measurement.')
        proof = service.record_evidence(path, owner_member_id='worker', actor_member_id='worker')
        service.submit_handoff(dict(task_id=task['task_id'], task_revision=4, member_id='worker', status='done', summary='refuted', evidence_ids=[proof], next_action='review', confidence=0.5, method_uses=[dict(memory_id='method', outcome='failure', evidence_id=proof)]))
        service.store.transition_task(task['task_id'], 4, 'review', 'changed-review')
        service.submit_handoff(dict(task_id=task['task_id'], task_revision=5, member_id='worker', status='done', summary='refuted', evidence_ids=[proof], next_action='review', confidence=0.5, method_uses=[dict(memory_id='method', outcome='failure', evidence_id=proof)]))
        revision = 5
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Replacement claim.', status='candidate')
        memory.close()
        with pytest.raises(ValueError, match='memory_review_content_changed'):
            service.review_task(task['task_id'], revision, 'approve', reviewer_member_id='worker', actor_member_id='worker')
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0] != 'done'
    finally:
        service.close()

def test_review_outbox_rejects_replacement_content(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'Replacement claim.', status='candidate')
        memory.close()
        path = tmp_path / 'proof.txt'
        path.write_text('Evidence about original claim only.')
        proof = service.record_evidence(path, owner_member_id='worker', actor_member_id='worker')
        service.store.enqueue_member_memory('bound-review', 'worker', 'method', 'review_method_practice', json.dumps(dict(outcome='failure', evidence_id=proof, memory_content_hash=hashlib.sha256(b'Original claim.').hexdigest())), {}, [])
        results = service.drain_member_memory_outbox()
        assert results[0]['status'] == 'failed'
        assert results[0]['error'] == 'memory_review_content_changed'
        memory = service.memory('worker')
        try:
            assert memory.db.execute('SELECT COUNT(*) FROM practice_observations').fetchone()[0] == 0
            assert memory.get('method')['status'] == 'candidate'
        finally:
            memory.close()
    finally:
        service.close()

def test_cold_review_failure_is_written_once_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        memory.put('method', 'lesson', 'A meter contains ten centimeters.', status='candidate')
        memory.cool('method')
        memory.close()
        service._maintain_member_memories('worker')
        task = service.store.db.execute("SELECT * FROM tasks WHERE assignee_id='worker'").fetchone()
        for revision, state in ((1, 'ready'), (2, 'claimed'), (3, 'running'), (4, 'review')):
            service.store.transition_task(task['task_id'], revision, state, 'failure-' + state)
        import subprocess, sys
        measured = subprocess.check_output([sys.executable, '-I', '-c', 'print(100 / 100)'], text=True).strip()
        assert measured == '1.0'
        path = tmp_path / 'measurement.txt'
        path.write_text('100 centimeters / 100 = ' + measured + ' meters; claim refuted.')
        proof = service.record_evidence(path, owner_member_id='worker', actor_member_id='worker')
        service.submit_handoff(dict(task_id=task['task_id'], task_revision=5, member_id='worker', status='done', summary='refuted by unit calculation', evidence_ids=[proof], next_action='review', confidence=0.9, method_uses=[dict(memory_id='method', outcome='failure', evidence_id=proof)]))
        result = service.review_task(task['task_id'], 5, 'approve', reviewer_member_id='worker', actor_member_id='worker')
        assert result['status'] == 'done'
        observation = service.store.db.execute("SELECT content FROM member_memory_outbox WHERE kind='review_method_practice'").fetchone()
        assert json.loads(observation[0])['memory_content_hash']
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        service.recover_review_learning()
        service.drain_member_memory_outbox()
        memory = service.memory('worker')
        try:
            assert memory.get('method')['status'] == 'candidate'
            rows = memory.db.execute("SELECT outcome FROM practice_observations WHERE memory_id='method'").fetchall()
            assert [row[0] for row in rows] == ['failure']
        finally:
            memory.close()
    finally:
        service.close()


def test_review_recovery_uses_committed_binding(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        original = 'Divide centimeters by 100.'
        memory.put('method', 'lesson', original, status='candidate')
        memory.cool('method')
        memory.close()
        service._maintain_member_memories('worker')
        task = service.store.db.execute("SELECT * FROM tasks WHERE assignee_id='worker'").fetchone()
        for revision, state in ((1, 'ready'), (2, 'claimed'), (3, 'running'), (4, 'review')):
            service.store.transition_task(task['task_id'], revision, state, 'recover-binding-' + state)
        path = tmp_path / 'proof.txt'
        path.write_text('100 centimeters / 100 = 1 meter.', encoding='utf-8')
        proof = service.record_evidence(path, owner_member_id='worker', actor_member_id='worker')
        service.submit_handoff(dict(task_id=task['task_id'], task_revision=5, member_id='worker', status='done', summary='checked', evidence_ids=[proof], next_action='review', confidence=0.9, method_uses=[dict(memory_id='method', outcome='success', evidence_id=proof)]))
        with monkeypatch.context() as patch:
            patch.setattr(service, '_record_review_learning', lambda *a, **k: {'status': 'pending'})
            service.review_task(task['task_id'], 5, 'approve', reviewer_member_id='worker', actor_member_id='worker')
        payload = json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0])
        payload['memory_content_hash'] = hashlib.sha256(b'replacement').hexdigest()
        service.store.db.execute('UPDATE tasks SET payload=? WHERE task_id=?', (json.dumps(payload), task['task_id']))
        service.recover_review_learning()
        row = service.store.db.execute("SELECT content FROM member_memory_outbox WHERE kind='review_method_practice'").fetchone()
        assert json.loads(row[0])['memory_content_hash'] == hashlib.sha256(original.encode('utf-8')).hexdigest()
        memory = service.memory('worker')
        try:
            assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='method'").fetchone()[0] == 1
        finally:
            memory.close()
    finally:
        service.close()
