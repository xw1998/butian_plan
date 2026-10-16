import json
import pytest
from butian.mcp_server import call_tool, list_tools_page, TOOLS
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

def test_tools_pagination_has_no_loss_or_duplicates():
    names = []
    cursor = None
    while True:
        result = list_tools_page(cursor)
        assert len(result['tools']) <= 8
        names.extend(t['name'] for t in result['tools'])
        cursor = result.get('nextCursor')
        if cursor is None:
            break
    assert len(names) == len(set(names)) == len(TOOLS)
    assert set(names) == {t['name'] for t in TOOLS}
    assert 'butian_collaboration_preflight' in names[:8]
    with pytest.raises(ValueError):
        list_tools_page('tools:1')

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
