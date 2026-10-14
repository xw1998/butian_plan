import json
import sys
import pytest
from butian.runtime.service import ButianService

def test_dispatcher_generates_domain_plan_before_dispatch(tmp_path):
    code = "import json,sys; s=sys.stdin.read(); print(json.dumps({'subtasks':['核算断面面积并保存测量证据','复核流速不确定度与量程边界']}))"
    (tmp_path/'providers.json').write_text(json.dumps({'providers':[{'type':'local-command','command':[sys.executable,'-c',code]}]}), encoding='utf-8')
    svc=ButianService(tmp_path)
    try:
        for member in ('dispatcher','area','velocity'):
            svc.add_member(member, member, provider_id='local-command')
        goal=svc.submit_goal('计算河道流量并复核不确定度',mode='team',member_id='dispatcher',member_ids=['area','velocity'],problem={'acceptance_criteria':['面积与流速有原始测量证据']})
        titles=[child['instruction'] for child in goal['children']]
        assert titles==['核算断面面积并保存测量证据','复核流速不确定度与量程边界']
        assert svc.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='team_plan_generated'").fetchone()[0]==1
    finally:
        svc.close()

def test_no_dispatcher_provider_requires_explicit_host_plan(tmp_path):
    svc=ButianService(tmp_path)
    try:
        svc.add_member('dispatcher','派活者')
        svc.add_member('worker','执行者',provider_id='manual')
        with pytest.raises(ValueError, match='team_plan_required'):
            svc.submit_goal('复核仪器',mode='team',member_id='dispatcher',member_ids=['worker'],problem={'acceptance_criteria':['实测合格']})
        assert not svc.store.list_tasks()
    finally:
        svc.close()

def test_invalid_generated_plan_never_dispatches(tmp_path):
    (tmp_path/'providers.json').write_text(json.dumps({'providers':[{'type':'local-command','command':[sys.executable,'-c','print("unsupported answer")']}]}),encoding='utf-8')
    svc=ButianService(tmp_path)
    try:
        svc.add_member('dispatcher','派活者',provider_id='local-command')
        svc.add_member('worker','执行者',provider_id='local-command')
        with pytest.raises(ValueError, match='invalid_generated_team_plan'):
            svc.submit_goal('复核仪器',mode='team',member_id='dispatcher',member_ids=['worker'],problem={'acceptance_criteria':['实测合格']})
        assert not svc.store.list_tasks()
    finally:
        svc.close()


def test_standard_mcp_goal_submit_accepts_team_plan(tmp_path):
    from butian.mcp_server import call_tool
    (tmp_path/'providers.json').write_text(json.dumps({'providers':[{'type':'local-command','command':[sys.executable,'-c','import sys; print(sys.stdin.read())']}]}), encoding='utf-8')
    svc=ButianService(tmp_path)
    try:
        svc.add_member('dispatcher','派活者', provider_id='local-command')
        svc.add_member('worker','执行者', provider_id='local-command')
        svc.store.set_member_token('dispatcher','secret')
        result=call_tool(svc, 'butian_goal_submit', {'actor_member_id':'dispatcher','token':'secret','goal':'验证数据','mode':'team','member_ids':['worker'],'subtasks':['核验输入并保存证据'],'problem':{'acceptance_criteria':['必须留存证据']}})
        assert result['status']=='awaiting_host'
        assert result['children'][0]['instruction']=='核验输入并保存证据'
    finally:
        svc.close()


def test_mcp_goal_rejects_invalid_mode_and_dispatcher_identity(tmp_path):
    from butian.mcp_server import call_tool
    svc=ButianService(tmp_path)
    try:
        svc.add_member('owner','目标所有者')
        svc.add_member('other','其他成员')
        svc.store.set_member_token('owner','secret')
        args={'actor_member_id':'owner','token':'secret','goal':'验证目标','problem':{'acceptance_criteria':['有证据']}}
        with pytest.raises(ValueError,match='invalid_session_mode'):
            call_tool(svc,'butian_goal_submit',dict(args,mode='invalid'))
        with pytest.raises(ValueError,match='dispatcher_identity_mismatch'):
            call_tool(svc,'butian_goal_submit',dict(args,mode='team',dispatcher_member_id='other'))
        assert not svc.store.list_tasks()
    finally:
        svc.close()

def test_standard_host_team_needs_no_provider(tmp_path):
    from butian.mcp_server import call_tool
    svc = ButianService(tmp_path)
    try:
        for mid in ('dispatcher', 'worker'):
            svc.add_member(mid, mid)
            svc.store.set_member_token(mid, 'secret-' + mid)
        result = call_tool(svc, 'butian_goal_submit', {'actor_member_id': 'dispatcher', 'token': 'secret-dispatcher', 'goal': '核验125厘米换算', 'mode': 'team', 'member_ids': ['worker'], 'subtasks': ['实测125厘米等于1.25米并保存证据'], 'problem': {'acceptance_criteria': ['真实换算证据']}})
        assert result['status'] == 'awaiting_host'
        child = result['children'][0]['task_id']
        exported = svc.export_execution(child, actor_member_id='worker')
        assert exported['status'] == 'awaiting_external_execution'
        assert svc.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
    finally:
        svc.close()

def test_host_team_root_cannot_export_before_children_reviewed(tmp_path):
    from butian.mcp_server import call_tool
    svc = ButianService(tmp_path)
    try:
        for mid in ('dispatcher', 'worker'):
            svc.add_member(mid, mid)
        svc.store.set_member_token('dispatcher', 'team-token')
        result = call_tool(svc, 'butian_goal_submit', {'actor_member_id': 'dispatcher', 'token': 'team-token', 'goal': '核验换算', 'mode': 'team', 'member_ids': ['worker'], 'subtasks': ['核验125厘米'], 'problem': {'acceptance_criteria': ['保存换算证据']}})
        with pytest.raises(ValueError, match='team_children_not_reviewed'):
            svc.export_execution(result['task_id'], actor_member_id='dispatcher')
    finally:
        svc.close()


def test_host_team_plan_survives_clarification_and_restart(tmp_path):
    svc = ButianService(tmp_path)
    try:
        svc.add_member('dispatcher', '派活者')
        svc.add_member('worker', '执行者')
        result = svc.submit_host_team_goal('核验125厘米', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['换算为米并保存测量证据'])
        root = result['task_id']
        assert result['status'] == 'needs_clarification'
        svc.update_problem(root, result['problem']['revision'], {'acceptance_criteria': ['结果为1.25米且有证据']}, actor_member_id='dispatcher', reason='用户补充验收标准')
    finally:
        svc.close()
    svc = ButianService(tmp_path)
    try:
        svc.resume_problem_task(root, actor_member_id='dispatcher')
        children = svc.store.db.execute('SELECT task_id,title,assignee_id FROM tasks WHERE parent_id=?', (root,)).fetchall()
        assert len(children) == 1
        assert children[0]['title'] == '换算为米并保存测量证据'
        assert children[0]['assignee_id'] == 'worker'
        assert svc.export_execution(children[0]['task_id'], actor_member_id='worker')['status'] == 'awaiting_external_execution'
        svc.resume_problem_task(root, actor_member_id='dispatcher')
        assert svc.store.db.execute('SELECT COUNT(*) FROM tasks WHERE parent_id=?', (root,)).fetchone()[0] == 1
    finally:
        svc.close()


def test_team_problem_revision_invalidates_prepared_summary(tmp_path):
    import json
    svc = ButianService(tmp_path)
    try:
        svc.add_member('dispatcher', '派活者')
        svc.add_member('worker', '执行者')
        result = svc.submit_host_team_goal('核验长度', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['测量长度'], problem={'acceptance_criteria': ['实测证据']})
        root = result['task_id']
        row = svc.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (root,)).fetchone()
        payload = json.loads(row['payload'])
        payload['results'] = [{'output': '旧汇总'}]
        svc.store.db.execute('UPDATE tasks SET payload=? WHERE task_id=?', (json.dumps(payload), root))
        svc.update_problem(root, 1, {'constraints': ['换用新样本']}, actor_member_id='dispatcher', reason='样本修订')
        current = json.loads(svc.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (root,)).fetchone()[0])
        assert not current.get('results')
        assert current['invalidated_team_summary']['results'] == [{'output': '旧汇总'}]
    finally:
        svc.close()


def test_revised_team_rejects_old_completed_children(tmp_path):
    svc = ButianService(tmp_path)
    try:
        svc.add_member('dispatcher', '派活者')
        svc.add_member('worker', '执行者')
        goal = svc.submit_host_team_goal('测量原样本', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['测量长度'], problem={'acceptance_criteria': ['保存实测证据']})
        root = goal['task_id']
        svc.store.db.execute("UPDATE tasks SET status='done' WHERE parent_id=?", (root,))
        svc.update_problem(root, 1, {'goal': '测量新样本'}, actor_member_id='dispatcher', reason='更换样本')
        with pytest.raises(ValueError, match='team_child_problem_stale'):
            svc.run_team_goal(root)
        payload = json.loads(svc.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (root,)).fetchone()[0])
        assert not payload.get('results')
    finally:
        svc.close()


def test_revised_team_resume_creates_current_children_once(tmp_path):
    svc = ButianService(tmp_path)
    try:
        svc.add_member('dispatcher', '派活者')
        svc.add_member('worker', '执行者')
        goal = svc.submit_host_team_goal('原样本', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['测量长度'], problem={'acceptance_criteria': ['保存证据']})
        root = goal['task_id']
        old = goal['children'][0]['task_id']
        svc.store.db.execute("UPDATE tasks SET status='done' WHERE task_id=?", (old,))
        svc.update_problem(root, 1, {'goal': '新样本'}, actor_member_id='dispatcher', reason='更换样本')
        svc.resume_problem_task(root, actor_member_id='dispatcher')
        svc.resume_problem_task(root, actor_member_id='dispatcher')
        rows = svc.store.db.execute('SELECT * FROM tasks WHERE parent_id=?', (root,)).fetchall()
        assert len(rows) == 2
        active = [row for row in rows if not json.loads(row['payload']).get('superseded_by_problem_revision')]
        assert len(active) == 1
        assert active[0]['task_id'] != old
        assert json.loads(active[0]['payload'])['parent_problem_revision'] == 2
        package = svc.export_execution(active[0]['task_id'], actor_member_id='worker')
        assert '新样本' in package['context']
        assert svc.run_team_goal(root)['status'] != 'awaiting_external_execution'
    finally:
        svc.close()


def test_two_runtimes_resume_revised_team_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    svc = ButianService(tmp_path)
    svc.add_member('dispatcher', '派活者')
    svc.add_member('worker', '执行者')
    goal = svc.submit_host_team_goal('原样本', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['测量长度'], problem={'acceptance_criteria': ['保存证据']})
    root = goal['task_id']
    svc.update_problem(root, 1, {'goal': '新样本'}, actor_member_id='dispatcher', reason='更换样本')
    svc.close()
    barrier = threading.Barrier(2)
    def resume(_):
        local = ButianService(tmp_path)
        try:
            barrier.wait(timeout=10)
            return local.resume_problem_task(root, actor_member_id='dispatcher')
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(resume, range(2)))
    svc = ButianService(tmp_path)
    try:
        rows = svc.store.db.execute('SELECT payload FROM tasks WHERE parent_id=?', (root,)).fetchall()
        assert len([row for row in rows if not json.loads(row['payload']).get('superseded_by_problem_revision')]) == 1
    finally:
        svc.close()


def test_host_can_supply_missing_plan_after_clarification(tmp_path):
    from butian.mcp_server import call_tool
    svc = ButianService(tmp_path)
    try:
        svc.add_member('dispatcher', 'Dispatcher')
        svc.add_member('worker', 'Worker')
        svc.store.set_member_token('dispatcher', 'resume-test-token')
        goal = call_tool(svc, 'butian_goal_submit', {'goal': 'Measure length', 'mode': 'team', 'member_ids': ['worker'], 'actor_member_id': 'dispatcher', 'token': 'resume-test-token'})
        root = goal['task_id']
        svc.update_problem(root, goal['problem']['revision'], {'acceptance_criteria': ['Preserve measurement evidence']}, actor_member_id='dispatcher', reason='Define acceptance')
    finally:
        svc.close()
    svc = ButianService(tmp_path)
    try:
        resumed = call_tool(svc, 'butian_problem_resume', {'task_id': root, 'actor_member_id': 'dispatcher', 'token': 'resume-test-token', 'subtasks': ['Measure and preserve evidence']})
        assert resumed['status'] == 'ready'
        rows = svc.store.db.execute('SELECT task_id,assignee_id,title,payload FROM tasks WHERE parent_id=?', (root,)).fetchall()
        assert len(rows) == 1
        assert rows[0]['assignee_id'] == 'worker'
        assert rows[0]['title'] == 'Measure and preserve evidence'
        assert json.loads(rows[0]['payload'])['execution_mode'] == 'host'
        assert svc.export_execution(rows[0]['task_id'], actor_member_id='worker')['status'] == 'awaiting_external_execution'
        assert svc.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
    finally:
        svc.close()


def test_resume_rejects_plan_when_current_children_already_exist(tmp_path):
    svc = ButianService(tmp_path)
    try:
        svc.add_member('dispatcher', 'Dispatcher')
        svc.add_member('worker', 'Worker')
        goal = svc.submit_host_team_goal('Measure', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['Original measurement'], problem={'acceptance_criteria': ['Preserve evidence']})
        with pytest.raises(ValueError, match='team_plan_already_dispatched'):
            svc.resume_problem_task(goal['task_id'], actor_member_id='dispatcher', subtasks=['Different measurement'])
        assert svc.store.db.execute('SELECT title FROM tasks WHERE parent_id=?', (goal['task_id'],)).fetchone()[0] == 'Original measurement'
        assert not svc.store.db.in_transaction
    finally:
        svc.close()


def test_provider_plans_after_clarification_without_holding_database_lock(tmp_path):
    code = "import json,sys; sys.stdin.read(); print(json.dumps({'subtasks':['Measure area and preserve evidence']}))"
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', code]}]}), encoding='utf-8')
    svc = ButianService(tmp_path)
    try:
        for member in ('dispatcher', 'area', 'unselected'):
            svc.add_member(member, member, provider_id='local-command')
        goal = svc.submit_goal('Measure area', mode='team', member_id='dispatcher', member_ids=['area'])
        svc.update_problem(goal['task_id'], 1, {'acceptance_criteria': ['Preserve area evidence']}, actor_member_id='dispatcher', reason='Clarify acceptance')
        original = svc._plan_team_goal
        def checked(*args, **kwargs):
            assert not svc.store.db.in_transaction
            return original(*args, **kwargs)
        svc._plan_team_goal = checked
        result = svc.resume_problem_task(goal['task_id'], actor_member_id='dispatcher')
        assert result['status'] == 'ready'
        children = svc.store.db.execute('SELECT assignee_id,title FROM tasks WHERE parent_id=?', (goal['task_id'],)).fetchall()
        assert [(row['assignee_id'], row['title']) for row in children] == [('area', 'Measure area and preserve evidence')]
        payload = json.loads(svc.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()[0])
        assert payload['team_plan'][0]['member_id'] == 'area'
        assert payload['team_planning']['problem_revision'] == 2
    finally:
        svc.close()


def test_resume_generated_plan_rejects_revision_changed_during_planning(tmp_path):
    code = "import json,sys; sys.stdin.read(); print(json.dumps({'subtasks':['Preserve evidence']}))"
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', code]}]}), encoding='utf-8')
    svc = ButianService(tmp_path)
    other = ButianService(tmp_path)
    try:
        for member in ('dispatcher', 'worker'):
            svc.add_member(member, member, provider_id='local-command')
        goal = svc.submit_goal('Measure', mode='team', member_id='dispatcher', member_ids=['worker'])
        root = goal['task_id']
        svc.update_problem(root, 1, {'acceptance_criteria': ['Evidence required']}, actor_member_id='dispatcher', reason='Clarify')
        original = svc._plan_team_goal
        def interleaved(*args, **kwargs):
            result = original(*args, **kwargs)
            other.update_problem(root, 2, {'constraints': ['Use new sample']}, actor_member_id='dispatcher', reason='Revise during planning')
            return result
        svc._plan_team_goal = interleaved
        with pytest.raises(ValueError, match='team_planning_revision_conflict'):
            svc.resume_problem_task(root, actor_member_id='dispatcher')
        assert svc.store.db.execute('SELECT COUNT(*) FROM tasks WHERE parent_id=?', (root,)).fetchone()[0] == 0
        payload = json.loads(svc.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (root,)).fetchone()[0])
        assert not payload.get('team_plan')
        assert svc.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='team_plan_generated'").fetchone()[0] == 0
        assert not svc.store.db.in_transaction
    finally:
        other.close()
        svc.close()


def test_concurrent_generated_resume_publishes_only_one_plan(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    code = "import json,sys; sys.stdin.read(); print(json.dumps({'subtasks':['Preserve evidence']}))"
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', code]}]}), encoding='utf-8')
    svc = ButianService(tmp_path)
    try:
        for member in ('dispatcher', 'worker'):
            svc.add_member(member, member, provider_id='local-command')
        root = svc.submit_goal('Measure', mode='team', member_id='dispatcher', member_ids=['worker'])['task_id']
        svc.update_problem(root, 1, {'acceptance_criteria': ['Evidence required']}, actor_member_id='dispatcher', reason='Clarify')
    finally:
        svc.close()
    barrier = threading.Barrier(2)
    def resume(_):
        local = ButianService(tmp_path)
        original = local._plan_team_goal
        def synchronized(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(timeout=15)
            return result
        local._plan_team_goal = synchronized
        try:
            return local.resume_problem_task(root, actor_member_id='dispatcher')['status']
        except ValueError as exc:
            return str(exc)
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(resume, range(2)))
    assert sorted(outcomes) == ['ready', 'team_planning_revision_conflict']
    svc = ButianService(tmp_path)
    try:
        assert svc.store.db.execute('SELECT COUNT(*) FROM tasks WHERE parent_id=?', (root,)).fetchone()[0] == 1
        assert svc.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='team_plan_generated'").fetchone()[0] == 1
        assert svc.store.db.execute("SELECT COUNT(*) FROM events WHERE event_type='team_goal_dispatched'").fetchone()[0] == 1
        assert not svc.store.db.in_transaction
    finally:
        svc.close()
