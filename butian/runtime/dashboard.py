"""Authenticated, content-free team metrics; never infer causal benefits."""
from collections import Counter
from pathlib import Path
import sqlite3
import time


def snapshot(service, actor):
    if not service.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (actor,)).fetchone():
        raise ValueError("member_not_found")
    members = service.store.db.execute("SELECT member_id,name,role FROM members ORDER BY member_id").fetchall()
    totals = Counter()
    kinds, states, practice = Counter(), Counter(), Counter()
    coverage = Counter()
    member_stats = []
    root = (service.home / 'members').resolve()
    for member in members:
        row = dict(member)
        row.update(coverage='unavailable', totals={}, memory_kinds={}, memory_states={})
        member_stats.append(row)
        path = root / member['member_id'] / 'memory.db'
        if root not in path.resolve().parents:
            coverage['unavailable'] += 1
            continue
        if not path.is_file():
            coverage['not_initialized'] += 1
            row['coverage'] = 'not_initialized'
            continue
        db = None
        try:
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
            db.execute('BEGIN')
            local = Counter()
            local_kinds, local_states, local_practice = Counter(), Counter(), Counter()
            for kind, status, count in db.execute('SELECT kind,status,COUNT(*) FROM memories GROUP BY kind,status'):
                local['memories'] += count
                local_kinds[kind] += count
                local_states[status] += count
                if status in {'candidate', 'pending_validation'}:
                    local['pending_validation'] += count
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, metric in [('memory_recall_decisions', 'recall_decisions'), ('recall_feedback_commits', 'feedback_decisions'), ('skill_states', 'skills'), ('memory_versions', 'versions'), ('memory_conflicts', 'conflicts')]:
                if table in tables:
                    local[metric] = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if 'memory_recall_log' in tables:
                local['recall_hits'], local['used_hits'] = db.execute('SELECT COUNT(*),COALESCE(SUM(used=1),0) FROM memory_recall_log').fetchone()
            if 'usage_feedback_evidence' in tables:
                for outcome, count in db.execute('SELECT outcome,COUNT(*) FROM usage_feedback_evidence GROUP BY outcome'):
                    if outcome in {'useful', 'harmful'}:
                        local[outcome + '_evidence'] = count
            if 'practice_observations' in tables:
                local_practice.update(dict(db.execute('SELECT outcome,COUNT(*) FROM practice_observations GROUP BY outcome')))
            if 'memory_validation_batches' in tables:
                local['validation_batches'] = db.execute('SELECT COUNT(*) FROM memory_validation_batches').fetchone()[0]
            for table, metric in [('dream_candidates','dream_candidates'), ('hypotheses','dream_hypotheses'), ('verification_queue','verification_queue'), ('memory_distillations','distillations'), ('memory_abstractions','abstractions'), ('memory_maintenance','maintenance'), ('memory_compositions','compositions'), ('skill_events','skill_events'), ('practice_observations','practice_observations')]:
                if table in tables:
                    local[metric] = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            if 'dream_runs' in tables:
                run_states = dict(db.execute('SELECT status,COUNT(*) FROM dream_runs GROUP BY status'))
                local['dream_attempts'] = sum(run_states.values())
                local['dream_runs'] = run_states.get('completed', 0)
                local['dream_skipped'] = run_states.get('skipped', 0)
                local['dream_other'] = local['dream_attempts'] - local['dream_runs'] - local['dream_skipped']
                run_columns = {r[1] for r in db.execute('PRAGMA table_info(dream_runs)')}
                local['dream_auto'] = db.execute("SELECT COUNT(*) FROM dream_runs WHERE status='completed' AND source='auto'").fetchone()[0] if 'source' in run_columns else 0
            if 'memory_validation_batches' in tables:
                local['validation_samples'], local['validation_passes'], local['validation_failures'] = db.execute('SELECT COALESCE(SUM(sample_count),0),COALESCE(SUM(pass_count),0),COALESCE(SUM(fail_count),0) FROM memory_validation_batches').fetchone()
            row.update(coverage='readable', totals=dict(local), memory_kinds=dict(local_kinds), memory_states=dict(local_states))
            totals.update(local)
            kinds.update(local_kinds)
            states.update(local_states)
            practice.update(local_practice)
            coverage['readable'] += 1
        except (sqlite3.Error, OSError, ValueError):
            coverage['unavailable'] += 1
        finally:
            if db is not None:
                db.close()
    tasks = Counter(dict(service.store.db.execute(
        'SELECT t.status,COUNT(*) FROM tasks t JOIN sessions s ON s.session_id=t.session_id '
        'WHERE s.dispatcher_member_id=? OR t.assignee_id=? GROUP BY t.status', (actor, actor))))
    return {
        'generated_at': time.time(), 'scope': 'team_counts_only',
        'task_scope': 'authenticated_member', 'members': len(members),
        'coverage': dict(coverage), 'partial': bool(coverage['unavailable']),
        'member_stats': member_stats, 'totals': dict(totals), 'memory_kinds': dict(kinds), 'memory_states': dict(states),
        'practice_outcomes': dict(practice), 'task_states': dict(tasks),
        'avoided_errors': {'count': None, 'supported': False,
                           'reason': '尚未记录独立避错证据；有用反馈和召回命中不等于避免错误。'},
        'definitions': {
            'pending_validation': '状态为 candidate 或 pending_validation 的记忆，按记忆条目计数。',
            'useful_evidence': '按成员、记忆和证据摘要去重的有用反馈，非独立任务次数。',
            'recall_hits': '召回日志条数，同一记忆可多次被召回。',
            'dream_runs': '仅统计本成员库中 completed 的自省；skipped 是门控检查未执行，另记 dream_skipped，dream_attempts 为全部记录。',
            'dream_auto': '其中由后台自动自省产生的 completed 数；自动自省默认关闭（BUTIAN_AUTO_DREAM=1 才开启），因此默认为 0。',
            'members': '当前注册成员身份数，不等于在线人数。',
            'coverage': '未初始化的记忆库没有记录；不可读取的库不计入总量，总量可能不完整。',
        },
    }
