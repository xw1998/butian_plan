import pytest
from butian.runtime.service import ButianService

@pytest.mark.parametrize('case', ['other_member', 'inactive', 'nan', 'inf', 'negative', 'over_one', 'boolean'])
def test_handoff_rejects_invalid_identity_state_or_confidence(tmp_path, case):
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.add_member('other', 'Other')
        session = service.create_session('team', 'other')
        task = service.store.create_task(session['session_id'], 'Work', 'worker')
        if case != 'inactive':
            for revision, state in enumerate(('ready', 'claimed', 'running'), 1):
                service.store.transition_task(task, revision, state, f'op-{revision}')
        revision = service.store.db.execute('SELECT revision FROM tasks WHERE task_id=?', (task,)).fetchone()[0]
        confidence = {'nan':float('nan'), 'inf':float('inf'), 'negative':-0.1, 'over_one':1.1, 'boolean':True}.get(case, 0.8)
        handoff = dict(task_id=task, task_revision=revision, member_id='other' if case == 'other_member' else 'worker', status='done', summary='Result', evidence_ids=[], next_action='Review', confidence=confidence)
        with pytest.raises(ValueError):
            service.submit_handoff(handoff)
        assert service.store.db.execute('SELECT COUNT(*) FROM handoffs').fetchone()[0] == 0
    finally:
        service.close()
