import json
import sys
from butian.runtime.service import ButianService


def test_configured_dispatcher_owns_team_root_and_is_not_default_worker(tmp_path):
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'import sys; print(sys.stdin.read())']}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        # Use only these members for the default worker-selection assertion.
        native = [r[0] for r in service.store.db.execute('SELECT member_id FROM members')]
        for member in native:
            service.store.db.execute('DELETE FROM members WHERE member_id=?', (member,))
        for member in ('current', 'dispatcher', 'worker'):
            service.add_member(member, member, provider_id='local-command', model_id='local-command')
        service.set_dispatcher_config('dispatcher')
        goal = service.submit_goal('核验输入数据并交付结果', mode='team', member_id='current', subtasks=['当前成员核验输入数据', '工作成员交付结果'], problem={'acceptance_criteria': ['执行结果须留存证据并通过本用例业务断言']})
        assert goal['session']['dispatcher_member_id'] == 'dispatcher'
        root = service.store.db.execute('SELECT assignee_id FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()
        assert root['assignee_id'] == 'dispatcher'
        assert goal['assignee_id'] == 'dispatcher'
        assert {child['member_id'] for child in goal['children']} == {'current', 'worker'}
        result = service.run_team_goal(goal['task_id'])
        assert result['status'] == 'awaiting_child_reviews'
    finally:
        service.close()
