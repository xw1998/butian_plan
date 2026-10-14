import json
import sys
from butian.runtime.service import ButianService
from butian.runtime.problem import ProblemModels


def test_provider_team_revision_rebuilds_current_children(tmp_path):
    command = [sys.executable, '-c', 'import sys; print(sys.stdin.read())']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', 'Dispatcher', provider_id='local-command', model_id='local-command')
        service.add_member('worker', 'Worker', provider_id='local-command', model_id='local-command')
        goal = service.submit_goal('Measure length', mode='team', member_id='dispatcher', member_ids=['worker'], subtasks=['Measure and report'], problem={'acceptance_criteria': ['Preserve measurement'], 'constraints': ['125 cm']})
        root = goal['task_id']
        old = goal['children'][0]['task_id']
        assert service.run_team_goal(root)['status'] == 'awaiting_child_reviews'
        ProblemModels(service.store).update(root, 1, {'constraints': ['230 cm']}, actor_id='dispatcher', reason='Correct measurement')
        service.resume_problem_task(root, actor_member_id='dispatcher')
        current = service.store.db.execute("SELECT task_id,status,payload FROM tasks WHERE parent_id=? AND COALESCE(json_extract(payload,'$.superseded_by_problem_revision'),0)=0", (root,)).fetchall()
        assert len(current) == 1 and current[0]['task_id'] != old
        payload = json.loads(current[0]['payload'])
        assert payload['parent_problem_revision'] == 2
        assert payload.get('execution_mode') != 'host'
        assert service.run_team_goal(root)['status'] == 'awaiting_child_reviews'
        output = json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (current[0]['task_id'],)).fetchone()[0])['provider_output']
        assert '230 cm' in output
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_sessions WHERE task_id=?', (current[0]['task_id'],)).fetchone()[0] == 1
    finally:
        service.close()


def test_team_resume_rejects_unstopped_child_session(tmp_path):
    import pytest
    command = [sys.executable, '-c', 'print("measurement")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        service.add_member('dispatcher', 'Dispatcher', provider_id='local-command', model_id='local-command')
        service.add_member('worker', 'Worker', provider_id='local-command', model_id='local-command')
        goal = service.submit_goal('Measure length', mode='team', member_id='dispatcher', member_ids=['worker'], subtasks=['Measure'], problem={'acceptance_criteria': ['Preserve measurement']})
        root = goal['task_id']; child = goal['children'][0]['task_id']
        service.run_team_goal(root)
        session = goal['children'][0]['execution']['provider_session_id']
        # Persist an unresolved stop state, without launching an OS cancellation probe.
        service.store.db.execute("UPDATE provider_sessions SET status='cancelling' WHERE provider_session_id=?", (session,))
        ProblemModels(service.store).update(root, 1, {'constraints': ['230 cm']}, actor_id='dispatcher', reason='Correction')
        with pytest.raises(ValueError, match='previous_execution_not_stopped'):
            service.resume_problem_task(root, actor_member_id='dispatcher')
        assert service.store.db.execute('SELECT COUNT(*) FROM tasks WHERE parent_id=?', (root,)).fetchone()[0] == 1
    finally:
        service.close()


def test_team_clarification_preserves_explicit_plan(tmp_path):
    command = [sys.executable, '-c', 'import sys; print(sys.stdin.read())']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        for member in ['dispatcher', 'worker']:
            service.add_member(member, member, provider_id='local-command', model_id='local-command')
        goal = service.submit_goal('Measure length', mode='team', member_id='dispatcher', member_ids=['worker'], subtasks=['Measure and report'])
        assert goal['status'] == 'needs_clarification'
        assert service.store.db.execute('SELECT COUNT(*) FROM provider_sessions').fetchone()[0] == 0
        root = goal['task_id']
        ProblemModels(service.store).update(root, 1, {'acceptance_criteria': ['Report measurement']}, actor_id='dispatcher', reason='Confirm acceptance')
        service.resume_problem_task(root, actor_member_id='dispatcher')
        assert service.run_team_goal(root)['status'] == 'awaiting_child_reviews'
        child = service.store.db.execute('SELECT payload FROM tasks WHERE parent_id=?', (root,)).fetchone()
        assert 'Report measurement' in json.loads(child[0])['provider_output']
    finally:
        service.close()


def test_recovery_visits_obsolete_child_and_preserves_unconfirmed_stop(tmp_path, monkeypatch):
    command = [sys.executable, '-c', 'print("measurement")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        for member in ['dispatcher', 'worker']:
            service.add_member(member, member, provider_id='local-command', model_id='local-command')
        goal = service.submit_goal('Measure', mode='team', member_id='dispatcher', member_ids=['worker'], subtasks=['Report'], problem={'acceptance_criteria': ['Report measurement']})
        root = goal['task_id']
        service.run_team_goal(root)
        session = goal['children'][0]['execution']['provider_session_id']
        service.store.db.execute("UPDATE provider_sessions SET status='cancelling' WHERE provider_session_id=?", (session,))
        ProblemModels(service.store).update(root, 1, {'constraints': ['230 cm']}, actor_id='dispatcher', reason='Correction')
        visited = []
        def unconfirmed(provider_session_id):
            visited.append(provider_session_id)
            return {'status': 'unknown', 'reason': 'stop_not_confirmed'}
        monkeypatch.setattr(service, 'cancel_provider_task', unconfirmed)
        assert service.recover_problem_execution(root) is False
        assert visited == [session]
        assert service.store.provider_session(session)['status'] == 'cancelling'
        def confirmed(provider_session_id):
            service.store.finish_provider_session(provider_session_id, 'cancelled', 'confirmed')
            return {'status': 'cancelled'}
        monkeypatch.setattr(service, 'cancel_provider_task', confirmed)
        assert service.recover_problem_execution(root) is True
        service.resume_problem_task(root, actor_member_id='dispatcher')
        assert service.run_team_goal(root)['status'] == 'awaiting_child_reviews'
    finally:
        service.close()


def test_obsolete_unknown_child_remains_blocked_after_restart(tmp_path):
    command = [sys.executable, '-c', 'print("measurement")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        for member in ['dispatcher', 'worker']:
            service.add_member(member, member, provider_id='local-command', model_id='local-command')
        goal = service.submit_goal('Measure', mode='team', member_id='dispatcher', member_ids=['worker'], subtasks=['Report'], problem={'acceptance_criteria': ['Report measurement']})
        root = goal['task_id']
        service.run_team_goal(root)
        session = goal['children'][0]['execution']['provider_session_id']
        service.store.db.execute("UPDATE provider_sessions SET status='unknown',reason='stop_not_confirmed' WHERE provider_session_id=?", (session,))
        ProblemModels(service.store).update(root, 1, {'constraints': ['230 cm']}, actor_id='dispatcher', reason='Correction')
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        import pytest
        assert service.recover_problem_execution(root) is False
        with pytest.raises(ValueError, match='previous_execution_not_stopped'):
            service.resume_problem_task(root, actor_member_id='dispatcher')
    finally:
        service.close()


def test_parent_revision_blocks_obsolete_provider_consumption(tmp_path):
    import pytest
    command = [sys.executable, '-c', 'print("measurement")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    try:
        for member in ['dispatcher', 'worker']:
            service.add_member(member, member, provider_id='local-command', model_id='local-command')
        goal = service.submit_goal('Measure', mode='team', member_id='dispatcher', member_ids=['worker'], subtasks=['Report'], problem={'acceptance_criteria': ['Report measurement']})
        root = goal['task_id']
        service.run_team_goal(root)
        session = goal['children'][0]['execution']['provider_session_id']
        ProblemModels(service.store).update(root, 1, {'constraints': ['230 cm']}, actor_id='dispatcher', reason='Correction')
        with pytest.raises(ValueError, match='provider_execution_revision_conflict'):
            service.consume_provider_task(session, actor_member_id='worker')
    finally:
        service.close()


def test_parent_revision_between_evidence_and_review_blocks_publication(tmp_path, monkeypatch):
    import pytest
    command = [sys.executable, '-c', 'print("measurement")']
    (tmp_path / 'providers.json').write_text(json.dumps({'providers': [{'type': 'local-command', 'command': command}]}), encoding='utf-8')
    service = ButianService(tmp_path)
    other = None
    try:
        for member in ['dispatcher', 'worker']:
            service.add_member(member, member, provider_id='local-command', model_id='local-command')
        goal = service.submit_goal('Measure', mode='team', member_id='dispatcher', member_ids=['worker'], subtasks=['Report'], problem={'acceptance_criteria': ['Report measurement']})
        root = goal['task_id']; child = goal['children'][0]['task_id']
        other = ButianService(tmp_path)
        original = service.record_evidence
        def evidence_then_revision(*args, **kwargs):
            evidence = original(*args, **kwargs)
            ProblemModels(other.store).update(root, 1, {'constraints': ['230 cm']}, actor_id='dispatcher', reason='Concurrent correction')
            return evidence
        monkeypatch.setattr(service, 'record_evidence', evidence_then_revision)
        with pytest.raises(ValueError, match='team_child_problem_stale'):
            service.run_team_goal(root)
        state = service.store.db.execute('SELECT status,payload FROM tasks WHERE task_id=?', (child,)).fetchone()
        assert state['status'] != 'review'
        assert 'provider_output' not in json.loads(state['payload'])
        assert service.store.db.execute('SELECT COUNT(*) FROM handoffs WHERE task_id=?', (child,)).fetchone()[0] == 0
    finally:
        if other is not None:
            other.close()
        service.close()
