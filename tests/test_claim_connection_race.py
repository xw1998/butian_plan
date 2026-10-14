from concurrent.futures import ThreadPoolExecutor
import threading
from butian.runtime.store import Store


def test_two_connections_claim_once_and_recover_expired_lease(tmp_path):
    path = tmp_path / 'team.db'
    store = Store(path)
    store.add_member('dispatcher', 'Dispatcher')
    store.add_member('a', 'A')
    store.add_member('b', 'B')
    session = store.create_session('team', 'dispatcher', None, {})
    task = store.create_task(session, 'claim once')
    store.transition_task(task, 1, 'ready', 'ready')
    store.close()
    barrier = threading.Barrier(2)
    def claim(member):
        local = Store(path)
        try:
            barrier.wait(timeout=10)
            try:
                return member, local.claim_task(task, member)
            except ValueError as error:
                return member, str(error)
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['a', 'b']))
    successes = [(member, result) for member, result in results if isinstance(result, dict)]
    assert len(successes) == 1
    assert next(result for _, result in results if isinstance(result, str)) in {'task_lease_held', 'revision_conflict'}
    store = Store(path)
    try:
        winner, claimed = successes[0]
        assert store.db.execute('SELECT assignee_id FROM tasks WHERE task_id=?', (task,)).fetchone()[0] == winner
        assert store.reclaim_expired_leases(now=claimed['lease_until'] + 1) == [task]
        loser = 'b' if winner == 'a' else 'a'
        reclaimed = store.claim_task(task, loser)
        assert reclaimed['revision'] > claimed['revision']
        assert store.db.execute('SELECT assignee_id FROM tasks WHERE task_id=?', (task,)).fetchone()[0] == loser
    finally:
        store.close()


def test_expired_claim_failure_preserves_original_lease(tmp_path):
    import pytest
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        session = service.create_session('solo', 'worker')
        task = service.store.create_task(session['session_id'], 'atomic claim', 'worker')
        service.store.transition_task(task, 1, 'ready', 'atomic-ready')
        service.store.claim_task(task, 'worker')
        service.store.db.execute('UPDATE tasks SET lease_until=1 WHERE task_id=?', (task,))
        before = dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone())
        service.store.db.execute("CREATE TRIGGER reject_claim BEFORE UPDATE ON tasks WHEN NEW.status='claimed' BEGIN SELECT RAISE(ABORT, 'injected_claim_failure'); END")
        with pytest.raises(Exception, match='injected_claim_failure'):
            service.store.claim_task(task, 'worker')
        after = dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task,)).fetchone())
        assert after == before
    finally:
        service.close()
