import json
import pytest
from butian.mcp_server import CORE_TOOL_NAMES, call_tool, list_tools_page, TOOLS
from butian.runtime.service import ButianService

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path / 'home')
    service.add_member('host', 'Host')
    service.add_member('analyst', 'Analyst')
    service.store.set_member_token('host', 'test-only')
    yield service, {'actor_member_id': 'host', 'token': 'test-only'}
    service.close()

def test_preflight_denies_cross_member_before_provider_inspection(runtime, monkeypatch):
    service, auth = runtime
    monkeypatch.setattr(service, '_preflight_provider', lambda *a: pytest.fail('unauthorized inspection'))
    result = call_tool(service, 'butian_collaboration_preflight', dict(auth, member_id='analyst'))
    assert result['blockers'] == ['member_memory_access_denied']
    assert 'provider' not in result['checks']
    assert not result['ready']

def test_preflight_never_confuses_configuration_with_execution(runtime, monkeypatch):
    service, auth = runtime
    monkeypatch.setattr(service, '_preflight_provider', lambda *a: ('configured', 'model'))
    result = call_tool(service, 'butian_collaboration_preflight', auth)
    assert result['configuration_ready']
    assert not result['ready'] and not result['execution_verified']
    assert result['checks']['evidence_storage']['status'] == 'unchecked'
    assert service.store.list_tasks() == []
    with pytest.raises(ValueError, match='member_authentication_failed'):
        call_tool(service, 'butian_collaboration_preflight', dict(auth, token='bad'))

def test_preflight_missing_provider_and_status(runtime):
    service, auth = runtime
    result = call_tool(service, 'butian_collaboration_preflight', auth)
    assert 'provider_unavailable' in result['blockers']
    assert call_tool(service, 'butian_status', {})['collaboration_ready'] == 'unchecked'

def test_tools_list_defaults_to_core_and_full_is_opt_in(monkeypatch):
    """默认只给闭环必需的工具，避免宿主把 125 份 schema 塞进每次模型请求。"""
    page = list_tools_page()
    names = [t['name'] for t in page['tools']]
    assert 'nextCursor' not in page
    assert len(names) == len(set(names)) == len(CORE_TOOL_NAMES)
    assert set(names) <= {t['name'] for t in TOOLS}
    # 入口在前，推进与收口都在默认清单里。
    assert names[:4] == ['butian_status', 'butian_connect', 'butian_context', 'butian_mode']
    assert {'butian_task_claim', 'butian_task_transition', 'butian_task_review',
            'butian_execution_submit', 'butian_context_compose', 'butian_evidence_record',
            'butian_task_handoff'} <= set(names)
    # 高级工具不在清单里，但可以经 butian_tool_call 调到。
    assert 'butian_tool_call' in names
    assert 'butian_dream_run' not in names
    with pytest.raises(ValueError):
        list_tools_page('tools:1')
    monkeypatch.setenv('BUTIAN_MCP_TOOLSET', 'full')
    assert len(list_tools_page()['tools']) == len(TOOLS)

def test_tools_pagination_still_available_when_limit_is_requested(monkeypatch):
    monkeypatch.setenv('BUTIAN_MCP_TOOLSET', 'full')
    names, cursor = [], None
    while True:
        result = list_tools_page(cursor, 8)
        assert len(result['tools']) <= 8
        names.extend(t['name'] for t in result['tools'])
        cursor = result.get('nextCursor')
        if cursor is None:
            break
    assert len(names) == len(set(names)) == len(TOOLS)
    with pytest.raises(ValueError):
        list_tools_page(None, 0)

def test_start_is_blocked_without_mutation(runtime):
    service, auth = runtime
    result = call_tool(service, 'butian_collaboration_start', dict(auth, goal='review', problem={'acceptance_criteria': ['evidence-backed review']}))
    assert result['status'] == 'blocked' and not result['task_created']
    assert service.store.list_tasks() == []
    result = call_tool(service, 'butian_collaboration_start', dict(auth, goal='review'))
    assert result['status'] == 'needs_clarification'
    assert result['clarification_questions']
    result = call_tool(service, 'butian_collaboration_start', dict(auth, member_id='analyst', goal='review', problem={'acceptance_criteria': ['review']}))
    assert result['status'] == 'blocked'
    assert service.store.list_tasks() == []

def test_missing_provider_offers_local_plan_that_is_not_evidence(runtime):
    service, auth = runtime
    result = call_tool(service, 'butian_collaboration_preflight',
                       dict(auth, goal='把回归跑通并修掉失败用例'))
    assert 'provider_unavailable' in result['blockers']
    plan = result['local_plan']
    assert plan['mode'] == 'local' and plan['basis'] == 'keyword_rules'
    # 降级计划绝不能进证据链，也不能被读成完成度。
    assert plan['evidence_eligible'] is False
    assert plan['not_a_substitute_for']
    assert plan['warning']
    assert {'defect_reproduction', 'verification'} <= set(plan['matched_rules'])
    tools = [step.get('tool') for step in plan['steps'] if step.get('tool')]
    assert tools[0] == 'butian_goal_submit'
    assert tools[-2:] == ['butian_evidence_record', 'butian_task_handoff']
    assert [step['step'] for step in plan['steps']] == list(range(1, len(plan['steps']) + 1))
    # 只是脚本：不建任务、不动状态。
    assert service.store.list_tasks() == []
    assert 'local_plan' not in call_tool(service, 'butian_collaboration_preflight', auth)

def test_local_plan_without_keywords_still_gives_the_generic_chain():
    from butian.runtime.local_plan import plan
    result = plan('整理一下', acceptance_criteria=['列出清单'])
    assert result['matched_rules'] == []
    assert len(result['steps']) == 4
    assert result['steps'][0]['arguments']['acceptance_criteria'] == ['列出清单']

def test_start_blocked_still_returns_a_local_plan(runtime):
    service, auth = runtime
    result = call_tool(service, 'butian_collaboration_start',
                       dict(auth, goal='写一份接口文档',
                            problem={'acceptance_criteria': ['文档能打开']}))
    assert result['status'] == 'blocked' and not result['task_created']
    assert result['local_plan']['basis'] == 'keyword_rules'
    assert 'document_deliverable' in result['local_plan']['matched_rules']
    assert service.store.list_tasks() == []
