import io
import json
import pytest
from butian.mcp_server import call_tool, TOOLS, run
from butian.runtime.service import ButianService
from butian.runtime.mcp_guidance import ToolError


@pytest.fixture
def runtime(tmp_path):
    service = ButianService(tmp_path / 'home')
    service.add_member('owner', 'Owner')
    service.store.set_member_token('owner', 'test-only-value')
    service.add_member('other', 'Other')
    service.store.set_member_token('other', 'other-only-value')
    yield service, {'actor_member_id': 'owner', 'token': 'test-only-value', 'member_id': 'owner'}
    service.close()


def test_single_schema_and_catalog_are_bounded(runtime):
    service, _ = runtime
    schema = call_tool(service, 'butian_tool_schema', {'name': 'butian_experiment_create'})
    assert schema['authorization']['available_to_member'] is False
    assert 'dataset_version' in schema['inputSchema']['required']
    page = call_tool(service, 'butian_tools_catalog', {'limit': 3})
    assert len(page['tools']) == 3 and page['next_offset'] == 3
    assert all('inputSchema' not in t for t in page['tools'])
    names = []
    offset = 0
    while True:
        page = call_tool(service, 'butian_tools_catalog', {'offset': offset, 'limit': 30})
        names.extend(t['name'] for t in page['tools'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert names == [t['name'] for t in TOOLS]
    with pytest.raises(ToolError):
        call_tool(service, 'butian_tools_catalog', {'limit': 0})


def test_missing_and_wrong_types_are_structured_without_values(runtime):
    service, auth = runtime
    with pytest.raises(ToolError) as error:
        call_tool(service, 'butian_evidence_record', auth)
    assert {'field': 'path', 'reason': 'required'} in error.value.details['issues']
    assert 'test-only-value' not in json.dumps(error.value.details)
    with pytest.raises(ToolError) as error:
        call_tool(service, 'butian_tools_catalog', {'limit': True})
    assert error.value.details['issues'][0]['reason'] == 'type'


def test_handoff_recovery_preserves_state_and_security(runtime):
    service, auth = runtime
    session = service.store.create_session('solo', 'owner', None, {})
    task = service.store.create_task(session, 'workflow recovery', 'owner')
    args = dict(auth, task_id=task, task_revision=1, status='completed', summary='not done', evidence_ids=[], next_action='review', confidence=0.5)
    with pytest.raises(ToolError) as error:
        call_tool(service, 'butian_task_handoff', args)
    details = error.value.details
    assert details['current']['status'] == 'todo'
    assert details['next_actions'][0]['arguments']['target'] == 'ready'
    assert service.store.list_tasks()[0]['status'] == 'todo'
    with pytest.raises(ValueError) as error:
        call_tool(service, 'butian_task_handoff', dict(args, token='wrong'))
    assert 'current' not in getattr(error.value, 'details', {})
    with pytest.raises(ValueError) as error:
        call_tool(service, 'butian_task_handoff', dict(args, actor_member_id='other', member_id='other', token='other-only-value'))
    assert 'current' not in getattr(error.value, 'details', {})


def test_export_mode_is_not_misreported_as_state(runtime):
    service, auth = runtime
    session = service.store.create_session('solo', 'owner', None, {})
    task = service.store.create_task(session, 'plain task', 'owner')
    with pytest.raises(ToolError, match='external_execution_required') as error:
        call_tool(service, 'butian_execution_export', dict(auth, task_id=task))
    assert error.value.details['missing_conditions'] == ['execution_mode_host_or_manual_provider']
    assert service.store.list_tasks()[0]['status'] == 'todo'


def test_admin_error_does_not_offer_identity_bypass(runtime):
    service, auth = runtime
    with pytest.raises(ToolError, match='admin_authorization_required') as error:
        call_tool(service, 'butian_experiment_create', dict(auth, name='test', task_package={}, dataset_version='v1', code_commit='test', provider_version='test'))
    assert error.value.details['authorization']['grant_via_this_endpoint'] is False
    assert 'current' not in error.value.details


def test_stdio_error_contains_machine_readable_data(tmp_path, monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'butian_evidence_record', 'arguments': {}}})+'\n'))
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    run(tmp_path / 'stdio')
    result = json.loads(output.getvalue())['result']
    assert result['isError'] is True
    error = json.loads(result['content'][0]['text'])
    assert error['details']['code'] == 'invalid_tool_arguments'
    assert any(issue['field'] == 'path' for issue in error['details']['issues'])


def test_unknown_tool_offers_nearest_names(runtime):
    service, _ = runtime
    with pytest.raises(ToolError) as error:
        call_tool(service, 'butian_tool_call', {'name': 'butian_memory_recallz', 'arguments': {}})
    details = error.value.details
    assert details['code'] == 'unknown_tool'
    assert details['did_you_mean'][0] == 'butian_memory_recall'
    assert all(action.get('why') for action in details['next_actions'])


def test_member_not_found_lists_members_and_suggests(runtime):
    service, _ = runtime
    with pytest.raises(ToolError) as error:
        call_tool(service, 'butian_connect', {'member_id': 'owner1'}, state={})
    details = error.value.details
    assert details['missing_conditions'] == ['existing_member']
    assert {'owner', 'other'} <= set(details['members'])
    assert details['did_you_mean']['member_id'] in {'owner', 'other'}
    assert details['next_actions'][0]['arguments']['member_id'] == details['did_you_mean']['member_id']


def test_practice_success_without_evidence_points_at_evidence_record(runtime):
    service, auth = runtime
    args = {**auth, 'memory_id': 'mem_x', 'kind': 'lesson', 'content': 'c',
            'outcome': 'success', 'independent_key': 'k1'}
    with pytest.raises(ToolError) as error:
        call_tool(service, 'butian_memory_practice', args)
    details = error.value.details
    assert details['code'] == 'practice_success_evidence_required'
    assert details['next_actions'][0]['tool'] == 'butian_evidence_record'
    assert details['missing_conditions'] == ['registered_evidence_for_success_outcome']


def test_missing_tool_name_is_named_not_unknown(runtime):
    service, _ = runtime
    with pytest.raises(ToolError) as error:
        call_tool(service, None, {})
    assert error.value.details['code'] == 'tool_name_required'
    assert error.value.details['missing_conditions'] == ['tool_name']


def test_null_arguments_equal_empty_object(runtime):
    service, _ = runtime
    result = call_tool(service, 'butian_status', None)
    assert result['service'] == 'butian'
