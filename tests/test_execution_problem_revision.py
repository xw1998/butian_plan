import json
import sys
import pytest
from butian.runtime.service import ButianService


def test_corrected_execution_can_be_read_again_after_review_and_restart(tmp_path):
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'print("测量结果：0.5%")']}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', '执行者', provider_id='local-command', model_id='local-command')
        created = service.submit_goal('测量', member_id='owner')
        task_id = created['task_id']
        service.update_problem(task_id, 1, {'acceptance_criteria': ['误差小于1%']}, actor_member_id='owner', reason='确认标准')
        service.resume_problem_task(task_id, actor_member_id='owner')
        first = service.run_solo_task(task_id, actor_member_id='owner')
        assert first['task_status'] == 'review'
        session_id = service.store.db.execute('SELECT provider_session_id FROM provider_sessions WHERE task_id=?', (task_id,)).fetchone()[0]
        repeated = service.consume_provider_task(session_id, actor_member_id='owner')
        assert repeated['status'] == 'completed'
        assert repeated['task_status'] == 'review'
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        assert service.store.provider_session(session_id)['problem_revision'] == 2
        assert service.consume_provider_task(session_id, actor_member_id='owner')['task_status'] == 'review'
        service.update_problem(task_id, 2, {'constraints': ['采用新样本']}, actor_member_id='owner', reason='替换样本')
        with pytest.raises(ValueError, match='provider_execution_revision_conflict'):
            service.consume_provider_task(session_id, actor_member_id='owner')
    finally:
        service.close()
