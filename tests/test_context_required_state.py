import pytest
from butian.runtime.service import ButianService


def test_required_task_cannot_be_replaced_by_keys(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.store.add_member('guard', '执行者')
        session = service.store.create_session('solo', 'guard', None, {})
        task = service.store.create_task(session, '保持约束', assignee_id='guard', payload={'constraints': ['禁止发布'], 'unknowns': ['验收条件待确认']})
        with pytest.raises(ValueError, match='context_budget_too_small'):
            service.compose_context('guard', '继续', task_id=task, budgets={'L1': 40})
        with pytest.raises(ValueError, match='context_budget_too_small'):
            service.compose_context('guard', '继续', task_id=task, budgets={'WINDOW': 40})
        text, report = service.compose_context('guard', '继续', task_id=task)
        assert task in text and '禁止发布' in text and '验收条件待确认' in text
        assert 'L1' not in report.trimmed
    finally:
        service.close()


def test_history_output_can_be_referenced_without_losing_review(tmp_path):
    import json
    service = ButianService(tmp_path)
    try:
        service.store.add_member('guard', '执行者')
        session = service.store.create_session('solo', 'guard', None, {})
        task = service.store.create_task(session, '修复边界', assignee_id='guard', payload={'constraints': ['禁止发布'], 'review_notes': '补边界证据', 'provider_output': '旧执行全文' * 3000, 'artifact_path': 'persisted-result.txt', 'evidence_id': 'evidence-existing'})
        text, report = service.compose_context('guard', '返工', task_id=task)
        state = json.loads(text.split('【L1 当前任务】\n')[1].split('\n\n')[0])
        assert state['payload']['constraints'] == ['禁止发布']
        assert state['payload']['review_notes'] == '补边界证据'
        assert state['payload']['evidence_id'] == 'evidence-existing'
        assert state['payload']['context_omitted_fields']['provider_output']['sha256']
        assert state['payload']['context_omitted_fields']['provider_output']['recover_from'] == 'tasks.payload'
        assert 'L1' in report.trimmed
    finally:
        service.close()

def test_recent_correction_cannot_disappear_under_budget(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('guard', '执行者')
        session = service.store.create_session('solo', 'guard', None, {})
        task = service.store.create_task(session, '继续工作', assignee_id='guard')
        service.store.append_event(task, 'user_correction', {'constraint': '未经确认绝不能发布' * 100}, 'correction-budget')
        with pytest.raises(ValueError, match='context_budget_too_small'):
            service.compose_context('guard', '继续', task_id=task, budgets={'L2': 80})
        with pytest.raises(ValueError, match='context_budget_too_small'):
            service.compose_context('guard', '继续', task_id=task, budgets={'WINDOW': 500, 'L2': 4000})
    finally:
        service.close()

def test_identity_is_required_and_l4_indexes_older_events(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('guard', '执行者', persona={'persona': '必须保留的人格约束' * 100})
        session = service.store.create_session('solo', 'guard', None, {})
        task = service.store.create_task(session, '核对历史', assignee_id='guard')
        first = service.store.append_event(task, 'decision_recorded', {'decision': '首轮决定'}, 'old-decision')
        for i in range(6):
            service.store.append_event(task, 'observation', {'index': i}, f'observation-{i}')
        text, report = service.compose_context('guard', '继续', task_id=task)
        assert '【L4 历史索引】' in text and first in text
        assert '【身份与权限】' in text and '必须保留的人格约束' in text
        with pytest.raises(ValueError, match='context_budget_too_small'):
            service.compose_context('guard', '继续', task_id=task, budgets={'WINDOW': 800})
    finally:
        service.close()


def test_old_correction_and_decision_survive_event_churn_and_restart(tmp_path):
    service = ButianService(tmp_path)
    service.add_member('guard', '执行者')
    session = service.store.create_session('solo', 'guard', None, {})
    task = service.store.create_task(session, '继续工作', assignee_id='guard')
    service.store.append_event(task, 'user_correction', {'constraint': '禁止自动发布，必须核对真实证据'}, 'persistent-correction')
    service.store.append_event(task, 'decision_recorded', {'decision': '采用标准MCP客户端验收'}, 'persistent-decision')
    for i in range(12):
        service.store.append_event(task, 'observation', {'index': i}, f'churn-{i}')
    service.close()
    service = ButianService(tmp_path)
    try:
        text, report = service.compose_context('guard', '继续', task_id=task, budgets={'L4': 0})
        assert '禁止自动发布，必须核对真实证据' in text
        assert '采用标准MCP客户端验收' in text
        with pytest.raises(ValueError, match='context_budget_too_small'):
            service.compose_context('guard', '继续', task_id=task, budgets={'L2': 80, 'L4': 0})
    finally:
        service.close()


def test_task_authorization_is_frozen_after_member_policy_change(tmp_path):
    import json
    service = ButianService(tmp_path)
    try:
        service.add_member('guard', '执行者', persona={'tool_policy': {'default': 'deny_until_authorized'}})
        session = service.store.create_session('solo', 'guard', None, {})
        task = service.store.create_task(session, '权限不能扩大', assignee_id='guard')
        service.store.db.execute('UPDATE members SET tool_policy=? WHERE member_id=?', (json.dumps({'default': 'allow', 'permissions': ['execute', 'cpu']}), 'guard'))
        from butian.runtime.context import ContextComposer
        package = ContextComposer(service.store).task_package(task)
        assert package['authorization_snapshot']['tool_policy']['default'] == 'deny_until_authorized'
        with pytest.raises(ValueError, match='task_tool_authorization_denied'):
            service.tools.assert_task_authorization('guard', task, ['execute'])
    finally:
        service.close()


def test_task_authorization_allows_frozen_scope_but_honors_revocation(tmp_path):
    import json
    service = ButianService(tmp_path)
    service.add_member('guard', '执行者', persona={'tool_policy': {'default': 'deny_until_authorized', 'permissions': ['execute']}})
    session = service.store.create_session('solo', 'guard', None, {})
    task = service.store.create_task(session, '冻结执行权限', assignee_id='guard')
    service.close()
    service = ButianService(tmp_path)
    try:
        service.tools.assert_task_authorization('guard', task, ['execute'])
        with pytest.raises(ValueError, match='task_tool_authorization_denied'):
            service.tools.assert_task_authorization('guard', task, ['execute', 'cpu'])
        service.store.db.execute('UPDATE members SET tool_policy=? WHERE member_id=?', (json.dumps({'default': 'deny_until_authorized'}), 'guard'))
        with pytest.raises(ValueError, match='task_tool_authorization_denied'):
            service.tools.assert_task_authorization('guard', task, ['execute'])
    finally:
        service.close()

def test_claim_binds_executor_without_expanding_frozen_scope(tmp_path):
    import json
    service = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', '派活者', persona={'tool_policy': {'permissions': ['execute']}})
        service.add_member('worker', '执行者', persona={'tool_policy': {'permissions': ['execute', 'cpu']}})
        session = service.store.create_session('team', 'dispatcher', None, {})
        task = service.store.create_task(session, '领取并执行')
        service.store.transition_task(task, 1, 'ready', 'claim-ready')
        service.store.claim_task(task, 'worker', internal=True)
        snapshot = json.loads(service.store.db.execute('SELECT authorization_snapshot FROM tasks WHERE task_id=?', (task,)).fetchone()[0])
        assert snapshot['member_id'] == 'worker'
        assert snapshot['tool_policy']['permissions'] == ['execute']
        service.tools.assert_task_authorization('worker', task, ['execute'])
        with pytest.raises(ValueError, match='task_tool_authorization_denied'):
            service.tools.assert_task_authorization('worker', task, ['execute', 'cpu'])
    finally:
        service.close()

def test_open_question_survives_event_churn_and_budget(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('guard', '执行者')
        session = service.store.create_session('solo', 'guard', None, {})
        task = service.store.create_task(session, '等待澄清', assignee_id='guard')
        service.store.db.execute('INSERT INTO questions VALUES (?,?,?,?,?,?,?,?)', ('open-question', task, 1, 'guard', '请确认测量单位与验收阈值', None, 'open', 1))
        for i in range(12):
            service.store.append_event(task, 'observation', {'index': i}, f'question-churn-{i}')
        text, _ = service.compose_context('guard', '继续', task_id=task, budgets={'L4': 0})
        assert '请确认测量单位与验收阈值' in text
        with pytest.raises(ValueError, match='context_budget_too_small'):
            service.compose_context('guard', '继续', task_id=task, budgets={'L2': 40})
    finally:
        service.close()


def test_large_ordinary_events_do_not_block_required_correction(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('guard', '执行者')
        session = service.store.create_session('solo', 'guard', None, {})
        task = service.store.create_task(session, '新的测量样本', assignee_id='guard')
        service.store.append_event(task, 'user_correction', {'constraint': '必须使用新样本2.3米'}, 'required-new-sample')
        for index in range(5):
            service.store.append_event(task, 'observation', {'output': '普通历史输出' * 200}, f'large-observation-{index}')
        text, report = service.compose_context('guard', '汇总', task_id=task)
        assert '必须使用新样本2.3米' in text
        assert 'butian_context_recover' in text
        assert report.layers['L2'] <= 2000
        assert 'L2' in report.trimmed
    finally:
        service.close()
