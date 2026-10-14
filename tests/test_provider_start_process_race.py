import multiprocessing
import time
from pathlib import Path
from butian.runtime.store import Store
from butian.runtime.provider_runtime import ProviderRuntime
from butian.adapters.registry import ProviderRegistry
from butian.adapters.local import LocalCommandProvider


def start_in_process(path, task, barrier, queue):
    store = Store(Path(path))
    class RecordedProvider(LocalCommandProvider):
        def create_child_session(self, *args, **kwargs):
            with open(Path(path).parent / 'remote-starts.txt', 'a', encoding='utf-8') as log:
                log.write('remote session created\n')
            time.sleep(.3)
            return super().create_child_session(*args, **kwargs)
    provider = RecordedProvider(['unused'])
    registry = ProviderRegistry()
    registry.register(provider)
    try:
        barrier.wait(timeout=10)
        try:
            result = ProviderRuntime(store, registry).start('member', 'execute once', task_id=task, provider_id='local-command')
            queue.put(('accepted', result['provider_session_id']))
        except ValueError as error:
            queue.put(('rejected', str(error)))
    finally:
        store.close()


def test_independent_processes_create_only_one_remote_session(tmp_path):
    store = Store(tmp_path / 'team.db')
    store.add_member('member', 'Executor')
    session = store.create_session('solo', 'member', None, {})
    task = store.create_task(session, 'single execution', assignee_id='member')
    for revision, status in enumerate(['ready', 'claimed', 'running'], 1):
        store.transition_task(task, revision, status, 'start-race-' + status)
    store.close()
    context = multiprocessing.get_context('spawn')
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=start_in_process, args=(str(tmp_path / 'team.db'), task, barrier, queue)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        outcomes = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert not process.is_alive()
            assert process.exitcode == 0
        assert sorted(item[0] for item in outcomes) == ['accepted', 'rejected']
        assert next(item[1] for item in outcomes if item[0] == 'rejected') == 'provider_start_in_progress'
        assert (tmp_path / 'remote-starts.txt').read_text(encoding='utf-8').splitlines() == ['remote session created']
        store = Store(tmp_path / 'team.db')
        try:
            assert store.db.execute('SELECT COUNT(*) FROM provider_sessions WHERE task_id=?', (task,)).fetchone()[0] == 1
        finally:
            store.close()
    finally:
        for process in processes:
            if process.is_alive():
                process.join(timeout=10)
        queue.close()
        queue.join_thread()
