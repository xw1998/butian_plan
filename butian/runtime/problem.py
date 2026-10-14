from __future__ import annotations
import json
import time
import uuid

LIST_FIELDS = ('constraints', 'known_facts', 'unknowns', 'acceptance_criteria', 'next_actions',
               'undesired_outcomes', 'inputs', 'outputs', 'resources', 'assumptions',
               'disputes', 'confirmation_required', 'partial_progress_criteria')
FIELDS = LIST_FIELDS + ('deadline', 'problem_type')


def normalize(goal, problem=None):
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('goal_required')
    if problem is not None and not isinstance(problem, dict):
        raise ValueError('invalid_problem_model')
    data = dict(problem or {})
    if set(data) - set(FIELDS):
        raise ValueError('unknown_problem_field')
    result = {'goal': goal.strip()}
    for field in LIST_FIELDS:
        value = data.get(field, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ValueError('invalid_problem_field:' + field)
        result[field] = [x.strip() for x in value]
    deadline = data.get('deadline', '')
    if not isinstance(deadline, str):
        raise ValueError('invalid_problem_field:deadline')
    problem_type = data.get('problem_type', 'unknown')
    if problem_type not in {'known', 'similar', 'new', 'unknown'}:
        raise ValueError('invalid_problem_field:problem_type')
    result.update(deadline=deadline.strip(), problem_type=problem_type)
    questions = list(dict.fromkeys(result['confirmation_required']))
    if not result['acceptance_criteria']:
        questions.append('什么结果可以证明目标已完成？')
    result['clarification_questions'] = questions
    result['definition_status'] = 'needs_clarification' if questions else 'defined'
    return result


class ProblemModels:
    def __init__(self, store):
        self.store = store
        store.db.execute('CREATE TABLE IF NOT EXISTS problem_models (task_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, actor_id TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(task_id,revision))')

    def get(self, task_id):
        row = self.store.db.execute('SELECT body FROM problem_models WHERE task_id=? ORDER BY revision DESC LIMIT 1', (task_id,)).fetchone()
        if not row:
            raise ValueError('problem_model_not_found')
        return json.loads(row['body'])

    def save(self, task_id, model, *, actor_id, reason, expected_revision=0):
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError('invalid_problem_revision')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('correction_reason_required')
        store = self.store
        with store._write_lock:
            store.db.execute('SAVEPOINT problem_write')
            try:
                task = store.db.execute('SELECT payload,status FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                if not task:
                    raise ValueError('task_not_found')
                if task['status'] in {'done', 'cancelled'}:
                    raise ValueError('task_closed')
                row = store.db.execute('SELECT COALESCE(MAX(revision),0) FROM problem_models WHERE task_id=?', (task_id,)).fetchone()
                if row[0] != expected_revision:
                    raise ValueError('revision_conflict')
                if not isinstance(model, dict):
                    raise ValueError('invalid_problem_model')
                normalized = normalize(model.get('goal'), {field: model.get(field) for field in FIELDS if field in model})
                body = dict(normalized, revision=expected_revision + 1, source={'actor_member_id': actor_id, 'reason': reason.strip()})
                encoded = json.dumps(body, ensure_ascii=False)
                store.db.execute('INSERT INTO problem_models VALUES (?,?,?,?,?,?)', (task_id, body['revision'], encoded, actor_id, reason.strip(), time.time()))
                payload = json.loads(task['payload'])
                parent = store.db.execute('SELECT parent_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()[0]
                if parent:
                    payload['parent_problem_revision'] = self.get(parent)['revision']
                if expected_revision > 0 and payload.get('mode') == 'team' and payload.get('results'):
                    payload['invalidated_team_summary'] = {'problem_revision': expected_revision, 'results': payload.pop('results'), 'reason': reason.strip()}
                    payload.pop('instruction', None)
                payload.update(problem_model=body, goal=body['goal'])
                store.db.execute('UPDATE tasks SET payload=?,title=?,revision=revision+? WHERE task_id=?', (json.dumps(payload, ensure_ascii=False), body['goal'], int(expected_revision > 0), task_id))
                store.append_event(task_id, 'problem_model_revised', body, 'problem:' + uuid.uuid4().hex)
                store.db.execute('RELEASE problem_write')
                return body
            except Exception:
                store.db.execute('ROLLBACK TO problem_write')
                store.db.execute('RELEASE problem_write')
                raise

    def update(self, task_id, expected_revision, changes, *, actor_id, reason):
        if not isinstance(changes, dict) or set(changes) - (set(FIELDS) | {'goal'}):
            raise ValueError('invalid_problem_changes')
        with self.store._write_lock:
            current = self.get(task_id)
            data = {key: changes.get(key, current.get(key, '' if key == 'deadline' else 'unknown' if key == 'problem_type' else [])) for key in FIELDS}
            model = normalize(changes.get('goal', current['goal']), data)
            return self.save(task_id, model, actor_id=actor_id, reason=reason, expected_revision=expected_revision)
