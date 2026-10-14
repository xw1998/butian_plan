import json

from butian.mcp_server import TOOLS, call_tool
from butian.runtime.service import ButianService

def registered_proof(service, member, label):
    path = service.home / (label + ".txt")
    path.write_text("execution result: " + label, encoding="utf-8")
    return service.record_evidence(path, owner_member_id=member, actor_member_id=member)


def test_practice_outcome_candidate_then_warm(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        first = service.record_practice_outcome("m", memory_id="lesson-1", kind="lesson", content="keep evidence", outcome="success", independent_key="run-a", evidence_ref=registered_proof(service, "m", "evidence-a"))
        assert first["status"] == "candidate"
        second = service.record_practice_outcome("m", memory_id="lesson-1", kind="lesson", content="keep evidence", outcome="success", independent_key="run-b", evidence_ref=registered_proof(service, "m", "evidence-b"))
        assert second["status"] == "warm"
        assert second["successes"] == 2
    finally:
        service.close()


def test_practice_outcome_rejects_unsupported_evidence_and_duplicate(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        try:
            service.record_practice_outcome("m", memory_id="lesson-2", kind="lesson", content="x", outcome="success", independent_key="run-a")
        except ValueError as exc:
            assert str(exc) == "practice_success_evidence_required"
        else:
            raise AssertionError("missing evidence was accepted")
        service.record_practice_outcome("m", memory_id="lesson-2", kind="lesson", content="x", outcome="success", independent_key="run-a", evidence_ref=registered_proof(service, "m", "e"))
        try:
            service.record_practice_outcome("m", memory_id="lesson-2", kind="lesson", content="x", outcome="success", independent_key="run-a", evidence_ref=registered_proof(service, "m", "e2"))
        except ValueError as exc:
            assert str(exc) == "duplicate_independent_observation"
        else:
            raise AssertionError("duplicate observation was accepted")
    finally:
        service.close()


def test_mcp_practice_tool_is_registered_and_callable(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m", "Member")
        service.store.set_member_token("m", "secret")
        assert any(tool["name"] == "butian_memory_practice" for tool in TOOLS)
        json.dumps(TOOLS)
        result = call_tool(service, "butian_memory_practice", {"member_id": "m", "memory_id": "lesson-3", "kind": "lesson", "content": "x", "outcome": "failure", "independent_key": "run-a", "actor_member_id": "m", "token": "secret"})
        assert result["status"] == "candidate"
    finally:
        service.close()


def test_review_creates_audited_candidate_once(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("worker", "执行者")
        service.add_member("reviewer", "审核者")
        session = service.create_session("team", "reviewer")
        task_id = service.store.create_task(session["session_id"], "reviewed task", "worker")
        for revision, state in ((1, "ready"), (2, "claimed"), (3, "running"), (4, "review")):
            service.store.transition_task(task_id, revision, state, "review-test-" + state)
        evidence_path = tmp_path / "review-evidence.txt"
        evidence_path.write_text("verified", encoding="utf-8")
        evidence_id = service.record_evidence(evidence_path, owner_member_id="worker", actor_member_id="worker")
        service.submit_handoff({"task_id": task_id, "task_revision": 5, "member_id": "worker", "status": "done", "summary": "完成实现", "evidence_ids": [evidence_id], "next_action": "交付", "confidence": 0.9})
        result = service.review_task(task_id, 5, "approve", reviewer_member_id="reviewer")
        learning = result["learning"]
        assert learning["candidate"] is True
        memory = service.memory("worker")
        try:
            assert memory.get(learning["memory_id"])["status"] == "candidate"
        finally:
            memory.close()
        assert service.store.validate_source_events([learning["event_id"]], member_id="worker")
        handoff = service.store.db.execute("SELECT payload FROM handoffs WHERE task_id=? AND revision=?", (task_id, 5)).fetchone()
        task = {"task_id": task_id, "payload": "{}"}
        duplicate = service._record_review_learning(task, 5, "approve", "", handoff)
        assert duplicate["status"] == "committed"
        assert service.store.db.execute("SELECT COUNT(*) FROM member_memory_outbox WHERE memory_id=?", (learning["memory_id"],)).fetchone()[0] == 1
    finally:
        service.close()


def test_real_task_candidate_reuse_counterevidence_and_restart(tmp_path):
    import sys
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': [sys.executable, '-c', 'import sys; text=sys.stdin.read(); print("calibration evidence verified")'] }]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker', provider_id='local-command')
        goal = service.submit_goal('calibration', member_id='worker', problem={'acceptance_criteria': ['执行结果须留存证据并通过本用例业务断言']})
        result = service.consume_provider_task(goal['execution']['provider_session_id'])
        assert result['status'] == 'completed'
        task = service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()
        learning = service.review_task(task['task_id'], task['revision'], 'approve', reviewer_member_id='worker')['learning']
        mid = learning['memory_id']
        memory = service.memory('worker')
        try:
            item = memory.get(mid)
            assert item['status'] == 'candidate'
            content = item['content']
        finally:
            memory.close()
        composed, report = service.compose_context('worker', 'calibration')
        assert mid in report.recalled_memory_ids
        assert 'candidate' in composed
        for key in ('independent-a', 'independent-b'):
            service.record_practice_outcome('worker', memory_id=mid, kind='task_review_candidate', content=content, outcome='success', independent_key=key, evidence_ref=registered_proof(service, 'worker', key))
        memory = service.memory('worker')
        try:
            assert memory.get(mid)['status'] == 'warm'
            memory.record_usage_feedback(mid, outcome='harmful', evidence_ref='counterexample-real-fixture')
            assert memory.get(mid)['status'] == 'disputed'
            memory.put(mid, 'task_review_candidate', content + ' revised preconditions', status='candidate', change_reason='counterevidence_revision', source_event_ids=[learning['event_id']])
        finally:
            memory.close()
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        chain = service.memory_versions('worker', mid)
        assert chain['chain']['valid']
        assert len(chain['versions']) >= 2
        composed, report = service.compose_context('worker', 'calibration')
        assert mid in report.recalled_memory_ids
        assert 'revised preconditions' in composed
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?', (goal['task_id'],)).fetchone()[0] == 'done'
    finally:
        service.close()

def test_practice_counterevidence_and_same_proof_cannot_promote(tmp_path):
    import pytest
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'Member')
        args = dict(memory_id='method', kind='lesson', content='check conditions')
        service.record_practice_outcome('m', **args, outcome='success', independent_key='a', evidence_ref=registered_proof(service, "m", "proof-a"))
        with pytest.raises(ValueError, match='independent_evidence_required'):
            service.record_practice_outcome('m', **args, outcome='success', independent_key='b', evidence_ref=registered_proof(service, "m", "proof-a"))
        service.record_practice_outcome('m', **args, outcome='success', independent_key='c', evidence_ref=registered_proof(service, "m", "proof-c"))
        result = service.record_practice_outcome('m', **args, outcome='failure', independent_key='d', evidence_ref=registered_proof(service, "m", "counterexample"))
        assert result['status'] == 'disputed'
    finally:
        service.close()


def test_practice_rejects_unregistered_and_duplicate_content(tmp_path):
    import pytest
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'Member')
        args = dict(memory_id='proof-method', kind='lesson', content='check input', outcome='success')
        with pytest.raises(ValueError, match='evidence_not_found'):
            service.record_practice_outcome('m', **args, independent_key='fake', evidence_ref='invented-proof')
        ids = []
        for index in range(2):
            path = tmp_path / f'same-{index}.txt'
            path.write_text('same execution proof', encoding='utf-8')
            ids.append(service.record_evidence(path, owner_member_id='m', actor_member_id='m'))
        service.record_practice_outcome('m', **args, independent_key='first', evidence_ref=ids[0])
        with pytest.raises(ValueError, match='independent_evidence_required'):
            service.record_practice_outcome('m', **args, independent_key='copy', evidence_ref=ids[1])
    finally:
        service.close()


def test_practice_failure_rolls_back_observation_and_version(tmp_path, monkeypatch):
    import pytest
    from butian.runtime.memory import MemoryStore
    memory = MemoryStore(tmp_path / 'rollback.db')
    try:
        original = memory.put
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 2:
                raise RuntimeError('write interrupted')
            return result
        monkeypatch.setattr(memory, 'put', fail_second)
        with pytest.raises(RuntimeError, match='write interrupted'):
            memory.record_practice_outcome(memory_id='method', kind='lesson', content='test', outcome='success', independent_key='run', evidence_ref='registered-proof', evidence_digest='digest')
        assert memory.get('method') is None
        for table in ('practice_observations', 'practice_evidence', 'memory_versions'):
            assert memory.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
    finally:
        memory.close()


def test_concurrent_practice_same_digest_is_counted_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from butian.runtime.memory import MemoryStore
    path = tmp_path / 'concurrent.db'
    MemoryStore(path).close()
    def observe(index):
        memory = MemoryStore(path)
        try:
            try:
                memory.record_practice_outcome(memory_id='method', kind='lesson', content='test', outcome='success', independent_key=str(index), evidence_ref=str(index), evidence_digest='same-content')
                return 'accepted'
            except ValueError as exc:
                return str(exc)
        finally:
            memory.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(observe, range(2)))
    assert sorted(results) == ['accepted', 'independent_evidence_required']
    memory = MemoryStore(path)
    try:
        assert memory.db.execute('SELECT COUNT(*) FROM practice_observations').fetchone()[0] == 1
        assert memory.get('method')['status'] == 'candidate'
    finally:
        memory.close()


def test_review_applies_declared_method_use_automatically(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        service.add_member('reviewer', 'Reviewer')
        memory = service.memory('worker')
        memory.put('stable-method', 'lesson', 'check input', status='candidate')
        memory.close()
        session = service.create_session('team', 'reviewer')
        for index, outcome in enumerate(('success', 'success', 'failure')):
            task_id = service.store.create_task(session['session_id'], 'check input', 'worker')
            for rev, state in ((1,'ready'),(2,'claimed'),(3,'running'),(4,'review')):
                service.store.transition_task(task_id, rev, state, f'{task_id}:{state}')
            proof = registered_proof(service, 'worker', f'actual-task-{index}')
            service.submit_handoff({'task_id':task_id,'task_revision':5,'member_id':'worker','status':'done','summary':'checked input','evidence_ids':[proof],'next_action':'review','confidence':0.9,'method_uses':[{'memory_id':'stable-method','outcome':outcome,'evidence_id':proof}]})
            service.review_task(task_id,5,'approve',reviewer_member_id='reviewer')
        memory = service.memory('worker')
        try:
            assert memory.get('stable-method')['status'] == 'disputed'
            assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='stable-method'").fetchone()[0] == 3
            assert len(memory.list_versions('stable-method')) >= 4
            skill = memory.db.execute("SELECT * FROM skill_states WHERE skill_domain='practice:lesson'").fetchone()
            assert skill is not None
            assert skill['level'] == 2 / 8
        finally:
            memory.close()
    finally:
        service.close()


def test_review_method_outbox_recovers_without_double_count(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'Member')
        memory = service.memory('m')
        memory.put('method', 'lesson', 'check input')
        memory.close()
        proof = registered_proof(service, 'm', 'recovery-run')
        oid = service.store.enqueue_member_memory('review:method:run', 'm', 'method', 'review_method_practice', json.dumps({'outcome':'success','evidence_id':proof,'memory_content_hash':__import__('hashlib').sha256(b'check input').hexdigest()}), {}, [])
        result = service.drain_member_memory_outbox(fail_after_write=True)
        assert result[0]['status'] == 'processing'
        service.store.db.execute('UPDATE member_memory_outbox SET lease_until=0 WHERE outbox_id=?', (oid,))
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        assert service.startup_recovery['member_memory_outbox'][0]['status'] == 'committed'
        memory = service.memory('m')
        try:
            assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='method'").fetchone()[0] == 1
            assert memory.get('method')['status'] == 'candidate'
        finally:
            memory.close()
        assert service.store.list_member_memory_outbox()[0]['status'] == 'committed'
    finally:
        service.close()


def test_restart_recovers_review_before_learning_enqueue(tmp_path, monkeypatch):
    import pytest
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'Member')
        session = service.create_session('solo', 'm')
        task = service.store.create_task(session['session_id'], 'checked input', 'm')
        for rev, state in ((1,'ready'),(2,'claimed'),(3,'running'),(4,'review')):
            service.store.transition_task(task, rev, state, f'{task}:{state}')
        proof = registered_proof(service, 'm', 'crash-review-proof')
        service.submit_handoff({'task_id':task,'task_revision':5,'member_id':'m','status':'done','summary':'checked input','evidence_ids':[proof],'next_action':'review','confidence':0.9})
        def interrupted(*args, **kwargs):
            raise RuntimeError('crash before learning enqueue')
        monkeypatch.setattr(service, '_record_review_learning', interrupted)
        with pytest.raises(RuntimeError, match='crash before learning'):
            service.review_task(task,5,'approve',reviewer_member_id='m')
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        memory = service.memory('m')
        try:
            assert memory.get(f'mem_review_{task}_5_approve') is not None
        finally:
            memory.close()
    finally:
        service.close()


def test_restart_recovers_partially_enqueued_review_methods(tmp_path, monkeypatch):
    import pytest
    service = ButianService(tmp_path)
    service.add_member('m', 'Member')
    memory = service.memory('m')
    memory.put('method', 'lesson', 'check input')
    memory.close()
    session = service.create_session('solo', 'm')
    task = service.store.create_task(session['session_id'], 'checked input', 'm')
    for rev, state in ((1,'ready'),(2,'claimed'),(3,'running'),(4,'review')):
        service.store.transition_task(task, rev, state, f'{task}:{state}')
    proof = registered_proof(service, 'm', 'partial-review-proof')
    service.submit_handoff({'task_id':task,'task_revision':5,'member_id':'m','status':'done','summary':'checked input','evidence_ids':[proof],'next_action':'review','confidence':0.9,'method_uses':[{'memory_id':'method','outcome':'success','evidence_id':proof}]})
    original = service.store.enqueue_member_memory
    def interrupted(*args, **kwargs):
        if args[3] == 'review_method_practice':
            raise RuntimeError('partial_review_enqueue')
        return original(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(service.store, 'enqueue_member_memory', interrupted)
        with pytest.raises(RuntimeError, match='partial_review_enqueue'):
            service.review_task(task,5,'approve',reviewer_member_id='m')
    assert len(service.store.list_member_memory_outbox()) == 1
    service.close()
    service = ButianService(tmp_path)
    try:
        memory = service.memory('m')
        try:
            assert memory.db.execute("SELECT COUNT(*) FROM practice_observations WHERE memory_id='method'").fetchone()[0] == 1
        finally:
            memory.close()
        assert len(service.store.list_member_memory_outbox()) == 2
    finally:
        service.close()
