import threading

import pytest

from butian.runtime.store import Store


def test_parent_task_must_belong_to_same_session(tmp_path):
    store = Store(tmp_path / 'runtime.db')
    try:
        first = store.create_session('solo')
        second = store.create_session('solo')
        parent = store.create_task(first, 'private parent')
        with pytest.raises(ValueError, match='parent_session_mismatch'):
            store.create_task(second, 'foreign child', parent_id=parent)
        assert len(store.list_tasks()) == 1
    finally:
        store.close()


def test_old_provider_session_cannot_publish_after_new_session(tmp_path):
    from butian.runtime.service import ButianService
    import json
    import sys
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'print("old")']}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker', provider_id='local-command')
        task = service.submit_goal('stale', member_id='worker', problem={'acceptance_criteria': ['执行结果须留存证据并通过本用例业务断言']})
        old = task['execution']['provider_session_id']
        service.store.create_provider_session('worker', 'local-command', task_id=task['task_id'], prompt='new')
        service.provider_runtime.consume(old, service.providers.get('local-command'))
        with pytest.raises(ValueError, match='provider_execution_superseded'):
            service.consume_provider_task(old)
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()['status'] == 'running'
    finally:
        service.close()


def test_claim_waits_for_shared_transaction_lock(tmp_path):
    store = Store(tmp_path / 'runtime.db')
    finished = threading.Event()
    entered = threading.Event()
    errors = []
    session = store.create_session('solo')
    task = store.create_task(session, 'claim')
    store.transition_task(task, 1, 'ready', 'ready')

    def claim():
        entered.set()
        try:
            store.claim_task(task, 'worker', internal=True)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=claim)
    try:
        with store._write_lock:
            worker.start()
            assert entered.wait(2)
            assert not finished.wait(0.2), 'claim bypassed shared transaction lock'
        worker.join(2)
        assert finished.is_set()
        assert not errors
    finally:
        worker.join(2)
        store.close()


def test_superseded_provider_is_rejected_before_process_execution(tmp_path):
    import json
    import sys
    from butian.runtime.service import ButianService
    marker = tmp_path / 'executed.txt'
    command = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("executed"); print("old")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker', provider_id='local-command')
        goal = service.submit_goal('stale', member_id='worker', problem={'acceptance_criteria': ['执行结果须留存证据并通过本用例业务断言']})
        old = goal['execution']['provider_session_id']
        service.store.create_provider_session('worker', 'local-command', task_id=goal['task_id'], prompt='new')
        with pytest.raises(ValueError, match='provider_execution_superseded'):
            service.consume_provider_task(old)
        assert not marker.exists()
    finally:
        service.close()


def test_mcp_cannot_attach_self_to_foreign_session(tmp_path):
    from butian.runtime.service import ButianService
    from butian.mcp_server import call_tool
    service = ButianService(tmp_path)
    try:
        service.add_member('owner', 'Owner')
        service.add_member('outsider', 'Outsider')
        service.store.set_member_token('outsider', 'test-only-token')
        session = service.create_session('solo', current_member_id='owner')['session_id']
        with pytest.raises(ValueError, match='task_create_forbidden'):
            call_tool(service, 'butian_task_create', {'actor_member_id': 'outsider', 'token': 'test-only-token', 'session_id': session, 'title': 'foreign', 'assignee_id': 'outsider'})
        assert not service.store.list_tasks()
    finally:
        service.close()


def test_hypothesis_ledger_waits_for_store_transaction(tmp_path):
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    entered, finished = threading.Event(), threading.Event()
    errors = []
    def write():
        entered.set()
        try:
            service.hypotheses.add_prior('user', 'title', 'claim', owner_member_id='owner')
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = threading.Thread(target=write)
    try:
        with service.store._write_lock:
            worker.start()
            assert entered.wait(2)
            assert not finished.wait(0.2), 'hypothesis ledger bypassed shared transaction lock'
        worker.join(2)
        assert finished.is_set() and not errors
    finally:
        worker.join(2)
        service.close()


def test_provider_concurrent_consumption_is_rejected(tmp_path):
    from butian.runtime.provider_runtime import ProviderRuntime
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / 'runtime.db')
    session = store.create_provider_session('worker', 'probe', prompt='work')
    entered, release = threading.Event(), threading.Event()
    errors = []
    class BlockingProvider:
        def stream(self, session_id, prompt):
            entered.set()
            assert release.wait(3)
            yield ProviderEvent(event_id='done', kind='status', status='completed')
    runtime = ProviderRuntime(store)
    def first():
        try:
            runtime.consume(session, BlockingProvider())
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=first)
    try:
        thread.start()
        assert entered.wait(2)
        with pytest.raises(ValueError, match='provider_consume_in_progress'):
            runtime.consume(session, BlockingProvider())
    finally:
        release.set()
        thread.join(4)
        store.close()
    assert not errors


def test_user_answer_records_event_in_task_transaction(tmp_path):
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        session = service.store.create_session('solo')
        task = service.store.create_task(session, 'question', assignee_id='worker')
        service.store.transition_task(task, 1, 'ready', 'ready-question')
        lease = service.store.claim_task(task, 'worker', internal=True)
        service.store.transition_task(task, lease['revision'], 'running', 'running-question')
        question = service.ask_user(task, lease['revision'] + 1, 'worker', 'input?')
        service.answer_user(question, 'answer')
        rows = service.store.db.execute("SELECT * FROM events WHERE aggregate_id=? AND event_type='user_question_answered'", (task,)).fetchall()
        assert len(rows) == 1
    finally:
        service.close()


def test_solo_answer_can_resume_with_new_context(tmp_path):
    import json
    import sys
    from butian.runtime.service import ButianService
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'print("resumed")']}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker', provider_id='local-command')
        goal = service.submit_goal('original', member_id='worker', problem={'acceptance_criteria': ['执行结果须留存证据并通过本用例业务断言']})
        task = service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()
        question = service.ask_user(task['task_id'], task['revision'], 'worker', 'input?')
        service.answer_user(question, 'new-answer')
        result = service.run_solo_task(task['task_id'], actor_member_id='worker')
        assert result['task_status'] == 'review'
        latest = service.store.db.execute('SELECT prompt FROM provider_sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1', (task['task_id'],)).fetchone()
        assert 'new-answer' in latest['prompt']
    finally:
        service.close()


def test_provider_consumption_lock_blocks_other_process(tmp_path):
    import subprocess
    import sys
    from butian.runtime.provider_runtime import ProviderRuntime
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / 'runtime.db')
    session = store.create_provider_session('worker', 'probe', prompt='work')
    entered, release = threading.Event(), threading.Event()
    errors = []
    class BlockingProvider:
        def stream(self, *_args):
            entered.set()
            assert release.wait(15)
            yield ProviderEvent(event_id='done', kind='status', status='completed')
    def consume():
        try:
            ProviderRuntime(store).consume(session, BlockingProvider())
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=consume)
    try:
        thread.start()
        assert entered.wait(2)
        code = '''import sys
from butian.runtime.store import Store
from butian.runtime.provider_runtime import ProviderRuntime
class Never:
    def stream(self, *args):
        raise AssertionError('stream started twice')
s = Store(sys.argv[1])
try:
    ProviderRuntime(s).consume(sys.argv[2], Never())
except ValueError as e:
    assert str(e) == 'provider_consume_in_progress', str(e)
else:
    raise AssertionError('second process accepted')
finally:
    s.close()
'''
        result = subprocess.run([sys.executable, '-c', code, str(store.path), session], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    finally:
        release.set()
        thread.join(16)
        store.close()
    assert not errors


def test_provider_start_rejects_revision_change_during_remote_creation(tmp_path):
    from butian.runtime.provider_runtime import ProviderRuntime
    from butian.adapters.base import ProviderResult
    from butian.core.models import ModelSelection
    store = Store(tmp_path / 'runtime.db')
    session = store.create_session('solo')
    task = store.create_task(session, 'start', assignee_id='worker')
    store.transition_task(task, 1, 'ready', 'start-ready')
    lease = store.claim_task(task, 'worker', internal=True)
    store.transition_task(task, lease['revision'], 'running', 'start-running')
    cancelled = []
    class Provider:
        provider_id = 'probe'
        def select_model(self, requested):
            return ModelSelection(requested, 'probe', 'exact')
        def create_child_session(self, member, prompt, model):
            current = store.db.execute('SELECT revision FROM tasks WHERE task_id=?', (task,)).fetchone()
            store.transition_task(task, current['revision'], 'waiting_user', 'intervening-question')
            return ProviderResult('running', provider_event_id='remote-created')
        def cancel(self, remote):
            cancelled.append(remote)
            return ProviderResult('cancelled')
    class Registry:
        def get(self, provider_id):
            return Provider()
    try:
        with pytest.raises(ValueError, match='provider_execution_revision_conflict'):
            ProviderRuntime(store, Registry()).start('worker', 'work', task_id=task, provider_id='probe')
        assert cancelled == ['remote-created']
        assert store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
        assert store.db.execute('SELECT status FROM tasks WHERE task_id=?', (task,)).fetchone()[0] == 'waiting_user'
    finally:
        store.close()


def test_provider_cancelled_during_stream_preserves_cancelled_terminal(tmp_path):
    from butian.runtime.provider_runtime import ProviderRuntime
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / 'runtime.db')
    sid = store.create_provider_session('worker', 'probe', prompt='work')
    class Provider:
        def stream(self, remote, prompt):
            store.finish_provider_session(sid, 'cancelled', 'explicit cancellation')
            yield ProviderEvent('late-completion', 'status', status='completed')
    try:
        result = ProviderRuntime(store).consume(sid, Provider())
        assert result['status'] == 'cancelled'
        assert result['reason'] == 'explicit cancellation'
    finally:
        store.close()


def test_mcp_dispatcher_can_consume_assigned_provider_without_new_prompt(tmp_path):
    import json
    import sys
    from butian.runtime.service import ButianService
    from butian.mcp_server import call_tool, TOOLS
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'print("delegated-output")']}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', 'Dispatcher', provider_id='local-command')
        service.add_member('worker', 'Worker', provider_id='local-command')
        service.store.set_member_token('dispatcher', 'fixture-token')
        task = service.submit_goal('delegated', member_id='worker', dispatcher_member_id='dispatcher', problem={'acceptance_criteria': ['执行结果须留存证据并通过本用例业务断言']})
        result = call_tool(service, 'butian_provider_consume', {'provider_session_id': task['execution']['provider_session_id'], 'actor_member_id': 'dispatcher', 'token': 'fixture-token'})
        assert result['status'] == 'completed'
        assert 'delegated-output' in result['text']
        schema = next(t['inputSchema'] for t in TOOLS if t['name'] == 'butian_provider_consume')
        assert 'prompt' not in schema['required']
    finally:
        service.close()


def test_failed_old_start_does_not_mark_new_execution_unknown(tmp_path, monkeypatch):
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        sid = service.store.create_session('solo', dispatcher_member_id='worker')
        tid = service.store.create_task(sid, 'work', assignee_id='worker')
        service.store.transition_task(tid, 1, 'ready', 'ready')
        lease = service.store.claim_task(tid, 'worker', internal=True)
        service.store.transition_task(tid, lease['revision'], 'running', 'running')
        def intervening_start(*args, **kwargs):
            service.store.create_provider_session('worker', 'probe', task_id=tid, prompt='new execution')
            raise ValueError('provider_execution_revision_conflict')
        monkeypatch.setattr(service, '_start_provider_task', intervening_start)
        with pytest.raises(ValueError, match='provider_execution_revision_conflict'):
            service.start_provider_task('worker', 'old execution', task_id=tid)
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (tid,)).fetchone()[0] == 'running'
    finally:
        service.close()


def test_cancelled_stream_does_not_persist_late_output(tmp_path):
    from butian.runtime.provider_runtime import ProviderRuntime
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / 'runtime.db')
    sid = store.create_provider_session('worker', 'probe', prompt='work')
    class Provider:
        def stream(self, remote, prompt):
            store.finish_provider_session(sid, 'cancelled', 'explicit cancellation')
            yield ProviderEvent('late-output', 'output', text='must not persist')
            yield ProviderEvent('late-completion', 'status', status='completed')
    try:
        result = ProviderRuntime(store).consume(sid, Provider())
        assert result['status'] == 'cancelled'
        assert store.db.execute('SELECT COUNT(*) FROM provider_outputs WHERE provider_session_id=?', (sid,)).fetchone()[0] == 0
    finally:
        store.close()


def test_context_revision_change_cannot_start_new_execution_with_old_context(tmp_path, monkeypatch):
    import json
    import sys
    from butian.runtime.service import ButianService
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'print("result")']}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker', provider_id='local-command')
        sid = service.store.create_session('solo', dispatcher_member_id='worker')
        tid = service.store.create_task(sid, 'work', assignee_id='worker')
        service.store.transition_task(tid, 1, 'ready', 'ready')
        lease = service.store.claim_task(tid, 'worker', internal=True)
        service.store.transition_task(tid, lease['revision'], 'running', 'running')
        original = service.compose_context
        def intervening_context(*args, **kwargs):
            composed = original(*args, **kwargs)
            revision = service.store.db.execute('SELECT revision FROM tasks WHERE task_id=?', (tid,)).fetchone()[0]
            service.store.transition_task(tid, revision, 'waiting_user', 'question')
            service.store.transition_task(tid, revision + 1, 'running', 'answer')
            return composed
        monkeypatch.setattr(service, 'compose_context', intervening_context)
        with pytest.raises(ValueError, match='provider_execution_revision_conflict'):
            service.start_provider_task('worker', 'old context', task_id=tid)
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_sessions WHERE task_id=?', (tid,)).fetchone()[0] == 0
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (tid,)).fetchone()[0] == 'running'
    finally:
        service.close()


def test_revision_change_during_stream_rejects_late_output(tmp_path):
    from butian.runtime.provider_runtime import ProviderRuntime
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / 'runtime.db')
    session = store.create_session('solo')
    task = store.create_task(session, 'stream', assignee_id='worker')
    store.transition_task(task, 1, 'ready', 'stream-ready')
    lease = store.claim_task(task, 'worker', internal=True)
    store.transition_task(task, lease['revision'], 'running', 'stream-running')
    sid = store.create_provider_session('worker', 'probe', task_id=task, prompt='work')
    class Provider:
        def stream(self, remote, prompt):
            revision = store.db.execute('SELECT revision FROM tasks WHERE task_id=?', (task,)).fetchone()[0]
            store.transition_task(task, revision, 'waiting_user', 'stream-question')
            yield ProviderEvent('late-output', 'output', text='must not persist')
            yield ProviderEvent('late-done', 'status', status='completed')
    try:
        result = ProviderRuntime(store).consume(sid, Provider())
        assert result['status'] == 'unknown'
        assert store.db.execute('SELECT COUNT(*) FROM provider_outputs WHERE provider_session_id=?', (sid,)).fetchone()[0] == 0
        assert store.db.execute('SELECT status FROM tasks WHERE task_id=?', (task,)).fetchone()[0] == 'waiting_user'
    finally:
        store.close()


def test_cancel_ack_does_not_overwrite_concurrent_completion(tmp_path):
    from butian.runtime.service import ButianService
    from butian.adapters.base import ProviderResult
    service = ButianService(tmp_path)
    sid = service.store.create_provider_session('worker', 'cancel-probe', prompt='work')
    class Provider:
        provider_id = 'cancel-probe'
        def cancel(self, remote):
            service.store.finish_provider_session(sid, 'completed')
            return ProviderResult('cancelled')
    service.providers.register(Provider())
    try:
        result = service.cancel_provider_task(sid)
        assert result['status'] == 'completed'
        assert service.store.provider_session(sid)['status'] == 'completed'
    finally:
        service.close()


def test_process_death_releases_provider_consumption_lock(tmp_path):
    import subprocess
    import sys
    from butian.runtime.provider_runtime import ProviderRuntime
    from butian.adapters.base import ProviderEvent
    store = Store(tmp_path / 'runtime.db')
    sid = store.create_provider_session('worker', 'probe', prompt='persisted')
    code = '''import os, sys
from butian.runtime.store import Store
from butian.runtime.provider_runtime import ProviderRuntime
class Dies:
    def stream(self, *args):
        os._exit(23)
        yield
store = Store(sys.argv[1])
ProviderRuntime(store).consume(sys.argv[2], Dies())
'''
    class Resume:
        def stream(self, remote, prompt):
            assert prompt == 'persisted'
            yield ProviderEvent('resumed-output', 'output', text='recovered')
            yield ProviderEvent('resumed-done', 'status', status='completed')
    try:
        child = subprocess.run([sys.executable, '-c', code, str(store.path), sid], timeout=10)
        assert child.returncode == 23
        result = ProviderRuntime(store).consume(sid, Resume())
        assert result['status'] == 'completed'
        assert result['text'] == 'recovered'
    finally:
        store.close()
