"""Revision-bound human and MCP host delivery, without a model dependency."""
from __future__ import annotations
import hashlib
import json
import math
import time
import uuid
from .problem import ProblemModels


def _task(service, task_id, actor):
    service.authorize_task(task_id, actor)
    row = service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if not row or row['assignee_id'] != actor:
        raise ValueError('execution_owner_required')
    payload = json.loads(row['payload'])
    if row['parent_id']:
        parent_revision = ProblemModels(service.store).get(row['parent_id'])['revision']
        if payload.get('parent_problem_revision') != parent_revision:
            raise ValueError('team_child_problem_stale')
    if payload.get('superseded_by_problem_revision'):
        raise ValueError('team_child_problem_stale')
    member = service.store.db.execute('SELECT provider_id FROM members WHERE member_id=?', (actor,)).fetchone()
    if payload.get('execution_mode') != 'host' and member['provider_id'] != 'manual':
        raise ValueError('external_execution_required')
    if payload.get('mode') == 'team' and not row['parent_id']:
        children = service.store.db.execute("SELECT status FROM tasks WHERE parent_id=? AND COALESCE(json_extract(payload,'$.superseded_by_problem_revision'),0)=0", (task_id,)).fetchall()
        if not children or any(child['status'] != 'done' for child in children):
            raise ValueError('team_children_not_reviewed')
        if not payload.get('results'):
            raise ValueError('team_summary_not_prepared')
    return dict(row), payload


def export_execution(service, task_id, *, actor_member_id, method_ids=None):
    with service.store._write_lock:
        savepoint = 'host_export_' + uuid.uuid4().hex
        service.store.db.execute('SAVEPOINT ' + savepoint)
        try:
            service.store.db.execute('UPDATE tasks SET revision=revision WHERE 0')
            result = _export_execution(service, task_id, actor_member_id=actor_member_id, method_ids=method_ids)
            service.store.db.execute('RELEASE SAVEPOINT ' + savepoint)
            return result
        except BaseException:
            service.store.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
            service.store.db.execute('RELEASE SAVEPOINT ' + savepoint)
            raise

def _export_execution(service, task_id, *, actor_member_id, method_ids=None):
    if method_ids is not None and (not isinstance(method_ids, list) or len(method_ids) > 8 or any(not isinstance(mid, str) or not mid for mid in method_ids) or len(set(method_ids)) != len(method_ids)):
        raise ValueError('invalid_execution_method_ids')
    with service.store._write_lock:
        # Reject oversized explicit references before advancing any task state.
        _task(service, task_id, actor_member_id)
        if method_ids:
            from .context import DEFAULT_BUDGETS
            memory = service.memory(actor_member_id)
            try:
                references = []
                for mid in method_ids:
                    item = memory.get(mid)
                    if not item or item['status'] in {'archived', 'disputed', 'deprecated', 'invalidated'}:
                        raise ValueError('execution_method_unavailable')
                    if len(item['content'].encode('utf-8')) > 16384:
                        raise ValueError('execution_method_too_large')
                    references.append({'memory_id': mid, 'content': item['content'], 'content_hash': hashlib.sha256(item['content'].encode('utf-8')).hexdigest(), 'status': item['status']})
                if len(json.dumps({'explicit_methods_reference_only': references}, ensure_ascii=False)) > DEFAULT_BUDGETS['L3']:
                    raise ValueError('execution_method_context_budget_exceeded')
            finally:
                memory.close()
        task, payload = _task(service, task_id, actor_member_id)
        model = ProblemModels(service.store).get(task_id)
        if model['definition_status'] != 'defined':
            raise ValueError('problem_needs_clarification')
        if task['status'] in {'todo', 'ready', 'unknown', 'failed', 'blocked', 'provider_lost'}:
            active = service.store.db.execute("SELECT 1 FROM provider_sessions WHERE task_id=? AND status IN ('running','cancelling','unknown','provider_lost')", (task_id,)).fetchone()
            if active:
                raise ValueError('previous_execution_not_stopped')
            if task['status'] != 'ready':
                service.store.transition_task(task_id, task['revision'], 'ready', f"problem-resume:{task_id}:{task['revision']}")
            task = dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            service.store.transition_task(task_id, task['revision'], 'claimed', f"external-claim:{task_id}:{task['revision']}")
            task['revision'] += 1
            task['status'] = 'claimed'
        if task['status'] == 'claimed':
            service.store.transition_task(task_id, task['revision'], 'running', f"external-run:{task_id}:{task['revision']}")
        task = dict(service.store.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
        if task['status'] != 'running':
            raise ValueError('external_task_not_running')
        payload = json.loads(task['payload'])
        instruction = payload.get('instruction') or payload.get('goal') or task['title']
        if task['parent_id']:
            parent_model = ProblemModels(service.store).get(task['parent_id'])
            if payload.get('parent_problem_revision') != parent_model['revision']:
                raise ValueError('team_child_problem_stale')
            instruction += '\n' + json.dumps({'team_problem': parent_model}, ensure_ascii=False)
        if payload.get('results'):
            instruction += '\n' + json.dumps({'goal': task['title'], 'results': payload['results']}, ensure_ascii=False)
        context, report = service.compose_context(actor_member_id, instruction, task_id=task_id, include_tools=True)
        service.store.db.execute('CREATE TABLE IF NOT EXISTS execution_method_snapshots (task_id TEXT NOT NULL, task_revision INTEGER NOT NULL, actor_member_id TEXT NOT NULL, problem_revision INTEGER NOT NULL, hashes TEXT NOT NULL, PRIMARY KEY(task_id,task_revision))')
        explicit_methods = []
        memory = service.memory(actor_member_id)
        try:
            for mid in method_ids or []:
                item = memory.get(mid)
                if not item or item['status'] in {'archived', 'disputed', 'deprecated', 'invalidated'}:
                    raise ValueError('execution_method_unavailable')
                if len(item['content'].encode('utf-8')) > 16384:
                    raise ValueError('execution_method_too_large')
                digest = hashlib.sha256(item['content'].encode('utf-8')).hexdigest()
                report.recalled_memory_hashes[mid] = digest
                explicit_methods.append({'memory_id': mid, 'content': item['content'], 'content_hash': digest, 'status': item['status']})
        finally:
            memory.close()
        if explicit_methods:
            from .context import DEFAULT_BUDGETS
            methods_context = json.dumps({'explicit_methods_reference_only': explicit_methods}, ensure_ascii=False)
            methods_budget = DEFAULT_BUDGETS['L3']
            if len(methods_context) > methods_budget:
                raise ValueError('execution_method_context_budget_exceeded')
            report.layers['EXPLICIT_METHODS'] = len(methods_context)
            report.budget_chars['EXPLICIT_METHODS'] = methods_budget
            context += '\n' + methods_context
        hashes = json.dumps(report.recalled_memory_hashes, sort_keys=True)
        previous = service.store.db.execute('SELECT actor_member_id,problem_revision,hashes FROM execution_method_snapshots WHERE task_id=? AND task_revision=?', (task_id, task['revision'])).fetchone()
        if previous and (previous['actor_member_id'] != actor_member_id or previous['problem_revision'] != model['revision']):
            raise ValueError('execution_method_snapshot_binding_changed')
        if previous and previous['hashes'] != hashes:
            raise ValueError('execution_method_snapshot_changed_requires_restart')
        service.store.db.execute('INSERT OR IGNORE INTO execution_method_snapshots VALUES (?,?,?,?,?)', (task_id, task['revision'], actor_member_id, model['revision'], hashes))
        service.store.append_event(task_id, 'external_execution_exported', {'task_revision': task['revision'], 'problem_revision': model['revision'], 'actor_member_id': actor_member_id}, 'external-export:' + uuid.uuid4().hex)
        return {'task_id': task_id, 'task_revision': task['revision'], 'problem_revision': model['revision'], 'context': context, 'context_report': report.as_dict(), 'method_snapshot': report.recalled_memory_hashes, 'status': 'awaiting_external_execution'}


def submit_execution(service, task_id, *, expected_revision, problem_revision, submission_id, output, evidence_ids, actor_member_id, work_done=None, work_not_done=None, assumptions=None, questions_for_user=None, confidence=0.7, method_uses=None):
    if not isinstance(submission_id, str) or not submission_id.strip() or len(submission_id) > 200:
        raise ValueError('invalid_submission_id')
    if not isinstance(output, str) or not output.strip() or len(output.encode('utf-8')) > 1048576:
        raise ValueError('invalid_execution_output')
    if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(e, str) or not e for e in evidence_ids):
        raise ValueError('execution_evidence_required')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError('invalid_execution_confidence')
    details = {key: [] if value is None else value for key, value in {'work_done': work_done, 'work_not_done': work_not_done, 'assumptions': assumptions, 'questions_for_user': questions_for_user}.items()}
    for values in details.values():
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError('invalid_execution_details')
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (expected_revision, problem_revision)):
        raise ValueError('invalid_execution_revision')
    if method_uses is not None and not isinstance(method_uses, list):
        raise ValueError('method_uses_must_be_list')
    request = dict(details, method_uses=method_uses or [], output=output, evidence_ids=evidence_ids, expected_revision=expected_revision, problem_revision=problem_revision, confidence=confidence)
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    store = service.store
    with store._write_lock:
        savepoint = 'host_submit_' + uuid.uuid4().hex
        store.db.execute('SAVEPOINT ' + savepoint)
        try:
            store.db.execute('UPDATE tasks SET revision=revision WHERE 0')
            task, payload = _task(service, task_id, actor_member_id)
            store.db.execute('CREATE TABLE IF NOT EXISTS external_submissions (task_id TEXT NOT NULL, submission_id TEXT NOT NULL, request_hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(task_id,submission_id))')
            previous = store.db.execute('SELECT request_hash,result FROM external_submissions WHERE task_id=? AND submission_id=?', (task_id, submission_id)).fetchone()
            if previous:
                if previous['request_hash'] != digest:
                    raise ValueError('submission_id_conflict')
                result = json.loads(previous['result'])
                store.db.execute('RELEASE SAVEPOINT ' + savepoint)
                return result
            # Re-read inside the write transaction: a second runtime may have corrected the task.
            task, payload = _task(service, task_id, actor_member_id)
            model = ProblemModels(store).get(task_id)
            if task['revision'] != expected_revision or model['revision'] != problem_revision:
                raise ValueError('revision_conflict')
            if task['status'] != 'running' or model['definition_status'] != 'defined':
                raise ValueError('external_task_not_running')
            snapshot_table = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_method_snapshots'").fetchone()
            snapshot = store.db.execute('SELECT actor_member_id,problem_revision,hashes FROM execution_method_snapshots WHERE task_id=? AND task_revision=?', (task_id, expected_revision)).fetchone() if snapshot_table else None
            if method_uses and not snapshot:
                raise ValueError('execution_method_snapshot_missing')
            if snapshot:
                if snapshot['actor_member_id'] != actor_member_id or snapshot['problem_revision'] != problem_revision:
                    raise ValueError('execution_method_snapshot_binding_changed')
                saved_hashes = json.loads(snapshot['hashes'])
                memory = service.memory(actor_member_id)
                try:
                    for use in method_uses or []:
                        if not isinstance(use, dict) or use.get('memory_id') not in saved_hashes:
                            raise ValueError('execution_method_not_in_snapshot')
                        current = memory.get(use['memory_id'])
                        if not current or current['status'] in {'archived', 'disputed', 'deprecated', 'invalidated'}:
                            raise ValueError('execution_method_unavailable')
                        if hashlib.sha256(current['content'].encode('utf-8')).hexdigest() != saved_hashes[use['memory_id']]:
                            raise ValueError('execution_method_content_changed')
                finally:
                    memory.close()
            from .extraction import validate_extraction
            validate_extraction(service, payload, actor_member_id, output)
            for evidence_id in evidence_ids:
                if store.verify_evidence(evidence_id, actor_member_id=actor_member_id)['status'] != 'valid':
                    raise ValueError('execution_evidence_invalid')
            session = store.db.execute("SELECT * FROM provider_sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
            if session:
                if session['provider_id'] != 'manual' or session['status'] != 'running' or session['problem_revision'] != problem_revision:
                    raise ValueError('external_provider_session_conflict')
                now = time.time()
                store.db.execute('INSERT INTO provider_outputs VALUES (?,?,?,?,?)', (session['provider_session_id'], 'external:' + submission_id, output, 'human', now))
                store.db.execute("UPDATE provider_sessions SET status='completed',reason='external_result_submitted',last_provider_event_id=?,updated_at=? WHERE provider_session_id=?", ('external:' + submission_id, now, session['provider_session_id']))
            revision = expected_revision + 1
            payload.update({'provider_output': output, 'provider_status': 'completed', 'external_submission_id': submission_id, 'evidence_ids': evidence_ids, 'execution_problem_revision': problem_revision})
            updated = store.db.execute("UPDATE tasks SET status='review',revision=revision+1,payload=?,lease_id=NULL,lease_until=NULL WHERE task_id=? AND revision=? AND status='running'", (json.dumps(payload, ensure_ascii=False), task_id, expected_revision)).rowcount
            if updated != 1:
                raise ValueError('revision_conflict')
            service.submit_handoff(dict(details, method_uses=method_uses or [], task_id=task_id, task_revision=revision, member_id=actor_member_id, status='done' if not details['work_not_done'] and not details['questions_for_user'] else 'partial', summary=output[:2000], evidence_ids=evidence_ids, next_action='审核成果及验收条件', confidence=confidence))
            result = {'task_id': task_id, 'status': 'review', 'revision': revision, 'problem_revision': problem_revision, 'submission_id': submission_id, 'evidence_ids': evidence_ids}
            store.db.execute('INSERT INTO external_submissions VALUES (?,?,?,?)', (task_id, submission_id, digest, json.dumps(result, ensure_ascii=False)))
            store.append_event(task_id, 'external_execution_submitted', result, f'external-delivery:{task_id}:{submission_id}')
            store.db.execute('RELEASE SAVEPOINT ' + savepoint)
            return result
        except BaseException:
            store.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
            store.db.execute('RELEASE SAVEPOINT ' + savepoint)
            raise
