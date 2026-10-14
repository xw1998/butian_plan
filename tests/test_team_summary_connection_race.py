import json
import threading
from concurrent.futures import ThreadPoolExecutor
from butian.runtime.service import ButianService


def test_two_connections_prepare_one_host_summary(tmp_path):
    service=ButianService(tmp_path)
    try:
        service.add_member('dispatcher','Dispatcher')
        service.add_member('worker','Worker')
        goal=service.submit_host_team_goal('Verify measured length',actor_member_id='dispatcher',member_ids=['worker'],subtasks=['Measure length'],problem={'acceptance_criteria':['Preserve measured evidence']})
        root=goal['task_id'];child=goal['children'][0]['task_id']
        package=service.export_execution(child,actor_member_id='worker')
        proof=tmp_path/'measurement.txt';proof.write_text('Measured length: 2.3 meters',encoding='utf-8')
        evidence=service.record_evidence(str(proof),owner_member_id='worker',actor_member_id='worker')
        delivered=service.submit_execution(child,expected_revision=package['task_revision'],problem_revision=package['problem_revision'],submission_id='measurement',output=proof.read_text(encoding='utf-8'),evidence_ids=[evidence],actor_member_id='worker')
        service.review_task(child,delivered['revision'],'approve',reviewer_member_id='dispatcher',actor_member_id='dispatcher')
    finally:service.close()
    barrier=threading.Barrier(2)
    def summarize(_):
        local=ButianService(tmp_path)
        try:
            barrier.wait(timeout=10)
            try:return local.run_team_goal(root)
            except ValueError as exc:return str(exc)
        finally:local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(summarize,range(2)))
    assert any(isinstance(item,dict) and item['status']=='awaiting_external_execution' for item in results)
    service=ButianService(tmp_path)
    try:
        payload=json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?',(root,)).fetchone()[0])
        assert len(payload['results'])==1 and payload['results'][0]['task_id']==child
        assert service.store.db.execute("SELECT COUNT(*) FROM events WHERE aggregate_id=? AND event_type='team_summary_requested'",(root,)).fetchone()[0]==1
    finally:service.close()


def test_parent_revision_changes_before_summary_transaction(tmp_path, monkeypatch):
    service = ButianService(tmp_path)
    other = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', 'Dispatcher')
        service.add_member('worker', 'Worker')
        goal = service.submit_host_team_goal('Verify measured length', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['Measure length'], problem={'acceptance_criteria': ['Preserve measured evidence']})
        root = goal['task_id']
        child = goal['children'][0]['task_id']
        package = service.export_execution(child, actor_member_id='worker')
        proof = tmp_path / 'revision-measurement.txt'
        proof.write_text('Measured length: 2.3 meters', encoding='utf-8')
        evidence = service.record_evidence(str(proof), owner_member_id='worker', actor_member_id='worker')
        delivered = service.submit_execution(child, expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='revision-measurement', output=proof.read_text(encoding='utf-8'), evidence_ids=[evidence], actor_member_id='worker')
        service.review_task(child, delivered['revision'], 'approve', reviewer_member_id='dispatcher', actor_member_id='dispatcher')
        original = service.store.transition_task
        revised = []
        def interleave(task_id, revision, target, *args, **kwargs):
            result = original(task_id, revision, target, *args, **kwargs)
            if task_id == root and target == 'running' and not revised:
                revised.append(other.update_problem(root, goal['problem']['revision'], {'acceptance_criteria': ['Repeat measurement under revised conditions']}, actor_member_id='dispatcher', reason='Conditions changed before final summary'))
            return result
        monkeypatch.setattr(service.store, 'transition_task', interleave)
        result = service.run_team_goal(root)
        assert result['status'] == 'ready'
        assert len(revised) == 1
        payload = json.loads(other.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (root,)).fetchone()[0])
        assert payload['problem_model']['revision'] == revised[0]['revision']
        assert not payload.get('results')
        assert other.store.db.execute("SELECT COUNT(*) FROM events WHERE aggregate_id=? AND event_type='team_summary_requested'", (root,)).fetchone()[0] == 0
        assert other.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
        assert not service.store.db.in_transaction
    finally:
        other.close()
        service.close()


def test_team_resume_rechecks_root_session_after_write_lock(tmp_path):
    import pytest
    service = ButianService(tmp_path)
    other = ButianService(tmp_path)
    original_db = service.store.db
    try:
        service.add_member('dispatcher', 'Dispatcher')
        service.add_member('worker', 'Worker')
        goal = service.submit_host_team_goal('Measure', actor_member_id='dispatcher', member_ids=['worker'], subtasks=['Report measurement'])
        root = goal['task_id']
        service.update_problem(root, goal['problem']['revision'], {'acceptance_criteria': ['Preserve evidence']}, actor_member_id='dispatcher', reason='Clarify acceptance')
        class InterleavedConnection:
            inserted = False
            def execute(self, sql, *args, **kwargs):
                if sql == 'BEGIN IMMEDIATE' and not self.inserted:
                    self.inserted = True
                    other.store.create_provider_session('dispatcher', 'manual', task_id=root)
                return original_db.execute(sql, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(original_db, name)
            def __enter__(self):
                original_db.__enter__()
                return self
            def __exit__(self, *args):
                return original_db.__exit__(*args)
        service.store.db = InterleavedConnection()
        with pytest.raises(ValueError, match='previous_execution_not_stopped'):
            service.resume_problem_task(root, actor_member_id='dispatcher')
        assert original_db.execute('SELECT COUNT(*) FROM tasks WHERE parent_id=?', (root,)).fetchone()[0] == 0
        assert not original_db.in_transaction
    finally:
        service.store.db = original_db
        other.close()
        service.close()
