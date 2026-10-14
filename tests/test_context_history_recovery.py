import hashlib
import json
import pytest
from butian.runtime.service import ButianService
from butian.mcp_server import call_tool


def test_history_recovery_returns_original_hash_and_checks_access(tmp_path):
    service = ButianService(tmp_path)
    try:
        for member in ('owner', 'outsider'):
            service.add_member(member, member)
            service.store.set_member_token(member, 'test-' + member)
        session = service.store.create_session('solo', 'owner', None, {})
        task = service.store.create_task(session, 'recover history', assignee_id='owner', payload={'provider_output': '完整结果' * 3000})
        event = service.store.append_event(task, 'decision_recorded', {'decision': '保留原文'}, 'original-event')
        auth = {'member_id': 'owner', 'actor_member_id': 'owner', 'token': 'test-owner', 'task_id': task}
        result = call_tool(service, 'butian_context_recover', {**auth, 'event_id': event})
        assert result['payload'] == {'decision': '保留原文'}
        encoded = json.dumps(result['payload'], ensure_ascii=False, separators=(',', ':'))
        assert result['sha256'] == hashlib.sha256(encoded.encode()).hexdigest()
        other_task = service.store.create_task(session, 'other', assignee_id='owner')
        other_event = service.store.append_event(other_task, 'decision_recorded', {'private': True}, 'other-event')
        with pytest.raises(ValueError, match='history_event_not_found'):
            call_tool(service, 'butian_context_recover', {**auth, 'event_id': other_event})
        service.store.db.execute("UPDATE events SET commit_state='pending' WHERE event_id=?", (event,))
        with pytest.raises(ValueError, match='history_event_not_found'):
            call_tool(service, 'butian_context_recover', {**auth, 'event_id': event})
        with pytest.raises(ValueError, match='history_payload_field_not_allowed'):
            call_tool(service, 'butian_context_recover', {**auth, 'payload_field': 'authorization_snapshot'})
        with pytest.raises(ValueError, match='history_recovery_selector_required'):
            call_tool(service, 'butian_context_recover', {**auth, 'event_id': event, 'payload_field': 'results'})
        result = call_tool(service, 'butian_context_recover', {**auth, 'payload_field': 'provider_output'})
        assert result['value'] == '完整结果' * 3000
        with pytest.raises(ValueError, match='task_access_denied'):
            call_tool(service, 'butian_context_recover', {**auth, 'member_id': 'outsider', 'actor_member_id': 'outsider', 'token': 'test-outsider', 'event_id': event})
        with pytest.raises(ValueError, match='history_event_not_found'):
            call_tool(service, 'butian_context_recover', {**auth, 'event_id': 'missing'})
    finally:
        service.close()
