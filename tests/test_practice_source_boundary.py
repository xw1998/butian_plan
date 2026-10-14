import pytest
from butian.runtime.service import ButianService

@pytest.mark.parametrize('source', ['missing', 'foreign'])
def test_practice_rejects_unverified_source_events(tmp_path, monkeypatch, source):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.add_member('other', 'Other')
        event = 'does-not-exist'
        if source == 'foreign':
            service.store.append_event('other', 'private_observation', {}, 'other-observation')
            event = service.store.db.execute("SELECT event_id FROM events WHERE idempotency_key='other-observation'").fetchone()[0]
        with pytest.raises(ValueError, match='invalid_memory_source_events'):
            service.record_practice_outcome('worker', memory_id='method', kind='lesson', content='Unverified observation', outcome='unknown', independent_key='source-boundary', source_event_ids=[event])
        memory = service.memory('worker')
        try:
            assert memory.get('method') is None
        finally:
            memory.close()
    finally:
        service.close()

@pytest.mark.parametrize('source', ['incomplete', 'task'])
def test_practice_committed_task_source_boundary(tmp_path, monkeypatch, source):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        aggregate = 'worker'
        if source == 'task':
            session = service.create_session('solo', current_member_id='worker', dispatcher_member_id='worker')
            aggregate = service.store.create_task(session['session_id'], 'source task', assignee_id='worker')
        event = service.store.append_event(aggregate, 'observation', {}, 'boundary-event')
        if source == 'incomplete':
            service.store.db.execute("UPDATE events SET commit_state='incomplete' WHERE event_id=?", (event,))
            with pytest.raises(ValueError, match='invalid_memory_source_events'):
                service.record_practice_outcome('worker', memory_id='method', kind='lesson', content='Observation', outcome='unknown', independent_key='boundary', source_event_ids=[event])
            memory = service.memory('worker')
            try:
                assert memory.get('method') is None
            finally:
                memory.close()
        else:
            service.record_practice_outcome('worker', memory_id='method', kind='lesson', content='Observation', outcome='unknown', independent_key='boundary', source_event_ids=[event])
            memory = service.memory('worker')
            try:
                assert memory.get('method') is not None
            finally:
                memory.close()
    finally:
        service.close()


@pytest.mark.parametrize('actor,allowed', [('worker', True), ('dispatcher', True), ('foreign', False)])
def test_task_source_practice_authorizes_only_assignee_and_dispatcher(tmp_path, monkeypatch, actor, allowed):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        for member in ('worker', 'dispatcher', 'foreign'):
            service.add_member(member, member)
        session = service.create_session('team', current_member_id='dispatcher', dispatcher_member_id='dispatcher')
        task = service.store.create_task(session['session_id'], 'independent task source boundary', assignee_id='worker')
        event = service.store.append_event(task, 'observation', {}, 'role-boundary-event')
        args = dict(memory_id='role-method', kind='lesson', content='Committed task observation', outcome='unknown', independent_key='role-boundary', source_event_ids=[event])
        if allowed:
            service.record_practice_outcome(actor, **args)
        else:
            with pytest.raises(ValueError, match='invalid_memory_source_events'):
                service.record_practice_outcome(actor, **args)
        memory = service.memory(actor)
        try:
            assert (memory.get('role-method') is not None) == allowed
        finally:
            memory.close()
    finally:
        service.close()
