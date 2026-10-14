from butian.runtime.service import ButianService


def test_maintenance_dispatches_pending_dream_once_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        for index in range(3):
            memory.put(str(index), 'lesson', f'Independent measured material {index}', status='candidate')
        memory.close()
        candidate = service.dream_run('worker', min_interval=0)['candidate_id']
        first = service.dispatch_dream_tasks(limit=1)
        assert len(first) == 1
        assert first[0]['candidate_id'] == candidate
        task_id = first[0]['task_id']
        package = service.export_execution(task_id, actor_member_id='worker')
        assert 'Independent measured material' in package['context']
        assert 'butian_dream_' in package['context']
        assert service.dispatch_dream_tasks(limit=1) == []
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        assert service.dispatch_dream_tasks(limit=1) == []
        assert service.store.db.execute('SELECT COUNT(*) FROM dream_reasoning_tasks').fetchone()[0] == 1
    finally:
        service.close()


def seed_candidate(service):
    service.add_member('worker', 'Worker')
    memory = service.memory('worker')
    try:
        for index in range(3):
            memory.put(str(index), 'lesson', f'Measured observation {index}', status='candidate')
    finally:
        memory.close()
    return service.dream_run('worker', min_interval=0)['candidate_id']


def test_maintenance_creates_host_task(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        candidate = seed_candidate(service)
        result = service.maintenance_tick()
        rows = service.store.db.execute('SELECT candidate_id FROM dream_reasoning_tasks').fetchall()
        assert rows and rows[0]['candidate_id'] == candidate
        assert service.maintenance_tick()['dream_reasoning_tasks'] == []
    finally:
        service.close()


def test_dispatch_rolls_back_tasks_and_marker_on_event_failure(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        seed_candidate(service)
        before = service.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        original = service.store.append_event
        def reject(*args, **kwargs):
            if len(args) > 1 and args[1] == 'dream_reasoning_dispatched':
                raise RuntimeError('injected_dispatch_failure')
            return original(*args, **kwargs)
        monkeypatch.setattr(service.store, 'append_event', reject)
        with pytest.raises(RuntimeError, match='injected_dispatch_failure'):
            service.dispatch_dream_tasks()
        assert service.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
        monkeypatch.setattr(service.store, 'append_event', original)
        assert len(service.dispatch_dream_tasks()) == 1
    finally:
        service.close()


def test_dispatch_rejects_stale_material(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        seed_candidate(service)
        memory = service.memory('worker')
        try:
            memory.db.execute("UPDATE memories SET content='changed' WHERE memory_id='0'")
        finally:
            memory.close()
        assert service.dispatch_dream_tasks() == []
        assert service.store.db.execute('SELECT COUNT(*) FROM dream_reasoning_tasks').fetchone()[0] == 0
    finally:
        service.close()


def test_two_connections_dispatch_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    seed_candidate(service)
    service.close()
    barrier = Barrier(2)
    def dispatch(_):
        local = ButianService(tmp_path)
        try:
            barrier.wait(timeout=10)
            return local.dispatch_dream_tasks()
        finally:
            local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, range(2)))
    assert sum(len(result) for result in results) == 1


def test_dispatch_reaches_candidate_after_first_hundred(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        candidate = seed_candidate(service)
        memory = service.memory('worker')
        try:
            for index in range(101):
                memory.db.execute("INSERT INTO dream_candidates SELECT ?,run_id,memory_ids,content,status,?,source_snapshot FROM dream_candidates WHERE candidate_id=?", (f'older-{index:03d}', -index - 1, candidate))
            memory.db.execute("UPDATE memories SET content='changed' WHERE memory_id='0'")
            fresh = service.dream_run('worker', min_interval=0, min_fragments=1)
            assert fresh['status'] == 'completed'
            latest = memory.db.execute('SELECT candidate_id FROM dream_candidates ORDER BY created_at DESC LIMIT 1').fetchone()[0]
        finally:
            memory.close()
        result = service.dispatch_dream_tasks()
        assert len(result) == 1
        assert result[0]['candidate_id'] == latest
    finally:
        service.close()


def test_candidate_inputs_do_not_starve_warm_memory_maintenance(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        try:
            for index in range(101):
                memory.put(f'candidate-{index}', 'lesson', 'Unverified observation', status='candidate')
            memory.put('verified-old', 'lesson', 'Verified old method', status='warm', source_event_ids=['seed-evidence'])
            memory.db.execute("UPDATE memories SET created_at=0,updated_at=CASE WHEN status='candidate' THEN 0 ELSE 1 END")
        finally:
            memory.close()
        result = service._maintain_member_memories('worker')
        assert result['assessed'] == 1
        assert result['cooled'] == 1
    finally:
        service.close()

def test_busy_first_member_does_not_starve_second_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        for owner in ('aaa', 'zzz'):
            service.add_member(owner, owner)
            memory = service.memory(owner)
            try:
                for index in range(3):
                    memory.put(str(index), 'lesson', f'Measured {owner} observation {index}', status='candidate')
            finally:
                memory.close()
            service.dream_run(owner, min_interval=0)
        assert service.dispatch_dream_tasks()[0]['member_id'] == 'aaa'
        memory = service.memory('aaa')
        try:
            memory.put('extra', 'lesson', 'Another independent measurement', status='candidate')
        finally:
            memory.close()
        assert service.dream_run('aaa', min_interval=0, min_fragments=1)['status'] == 'completed'
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        assert service.dispatch_dream_tasks()[0]['member_id'] == 'zzz'
    finally:
        service.close()

def test_stale_backlog_scan_is_bounded_and_resumes_after_restart(tmp_path, monkeypatch):
    from butian.runtime.dream import DreamRunner
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        candidate = seed_candidate(service)
        memory = service.memory('worker')
        try:
            for index in range(250):
                memory.db.execute('INSERT INTO dream_candidates SELECT ?,run_id,memory_ids,content,status,?,source_snapshot FROM dream_candidates WHERE candidate_id=?', (f'stale-{index:03d}', index - 1000, candidate))
            memory.db.execute("UPDATE memories SET content='new measurement' WHERE memory_id='0'")
        finally:
            memory.close()
        fresh = service.dream_run('worker', min_interval=0, min_fragments=1)['candidate_id']
        calls = []
        original = DreamRunner._validate_candidate_sources
        def track(self, cid):
            calls.append(cid)
            return original(self, cid)
        monkeypatch.setattr(DreamRunner, '_validate_candidate_sources', track)
        result = service.dispatch_dream_tasks()
        assert len(calls) <= 200
        assert result == []
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        calls.clear()
        result = service.dispatch_dream_tasks()
        assert len(calls) <= 200
        assert result[0]['candidate_id'] == fresh
    finally:
        service.close()


def test_stale_first_member_scan_does_not_starve_other_member(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        for owner in ('aaa', 'zzz'):
            service.add_member(owner, owner)
            memory = service.memory(owner)
            for index in range(3):
                memory.put(str(index), 'lesson', f'Independent measurement {index}', status='candidate')
            memory.close()
        stale = service.dream_run('aaa', min_interval=0)['candidate_id']
        fresh = service.dream_run('zzz', min_interval=0)['candidate_id']
        memory = service.memory('aaa')
        try:
            for index in range(450):
                memory.db.execute('INSERT INTO dream_candidates SELECT ?,run_id,memory_ids,content,status,?,source_snapshot FROM dream_candidates WHERE candidate_id=?', (f'stale-{index}', index - 1000, stale))
            memory.db.execute("UPDATE memories SET content='changed' WHERE memory_id='0'")
        finally:
            memory.close()
        assert service.dispatch_dream_tasks() == []
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        result = service.dispatch_dream_tasks()
        assert result and result[0]['member_id'] == 'zzz'
        assert result[0]['candidate_id'] == fresh
    finally:
        service.close()
