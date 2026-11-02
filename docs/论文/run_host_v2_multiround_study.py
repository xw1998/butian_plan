"""Host-loop v2（压力档）: 真实模型 + 真实 MCP 工具 + 近邻版本诱饵 + 上游坏经验。

与 v1（`run_host_multiround_study.py`）共用同一套基础设施与同一判分函数，只加两个压力轴：

  * decoy-drift  同一实体相邻版本的字段名/端点名**不随版本变化**，数值只差 3%
                 （offset / divisor / factor 逐版本 +3），版本只能靠 RULE 行的 version 字段区分；
  * poison-stable 在宿主第一次召回之前，记忆已被写成 warm 的**错值**（数值偏移 7 或 37），
                 带两条独立成功观察，模拟"上一批会话把提取错误写进经验库"。

宿主仍然只能做三件事：`butian_memory_recall`、`butian_knowledge_source_read`、提交答案；
经验写回一律由本 harness 在客观判分之后执行。

阶段（每次调用开一个 MCP 会话，状态落盘）：

  --stage bootstrap          成员、源快照、任务流、坏经验预置
  --stage recall  --round N  对每个实体跑 butian_memory_recall
  -> 宿主写 stages/rN-recall-decision.json
  --stage source  --round N  对请求读源的实体跑 butian_knowledge_source_read
  -> 宿主写 stages/rN-answers.json
  --stage settle  --round N  判分、写证据与实践观察、记录记忆状态
  --stage report             聚合 N1..N5，写 summary.json

`--stub` 只用于管道自检，结果标 `host_kind = stub`，不得当作宿主结果引用。
"""
from pathlib import Path
import argparse
import json
import random
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_host_multiround_study import (McpSession, dump, load, sha256_text, service_call,
                                       _score, _answer_from_rule, question_text, ROOT)

PROTOCOL = 'host-loop-v2'
SEED = 20261102
ROUNDS = 4
PER_FAMILY = 6
FAMILIES = ['calibration', 'schema', 'tool']
MEMBER = 'host'
STUB = False
RECALL_LIMIT = 100
# 与 v1 同一个理由：recall 只做子串模糊匹配 + 状态/权重/时间排序，没有结构化过滤。
GROUP_OF_INDEX = {0: 'clean-stable', 1: 'clean-stable',
                  2: 'clean-drift', 3: 'clean-drift',
                  4: 'poison-stable', 5: 'decoy-drift'}
GROUPS = ('clean-stable', 'clean-drift', 'decoy-drift', 'poison-stable')
POISON_OFFSET = {'calibration': ('offset', 37), 'schema': ('divisor', 7), 'tool': ('factor', 7)}

# ---------------------------------------------------------------- 任务流

def entity_plan():
    plan = []
    for family in FAMILIES:
        for index in range(PER_FAMILY):
            group = GROUP_OF_INDEX[index]
            plan.append({'entity': f'{family}{index:03}', 'family': family, 'group': group,
                         'stable': group in ('clean-stable', 'poison-stable')})
    return plan

def _decoy_rule(family, entity, base, version):
    """近邻版本：名字不随版本变，只有数值差 3%，版本只能靠 RULE.version 区分。"""
    rule = {'family': family, 'entity': entity, 'version': version}
    if family == 'calibration':
        rule.update(scale=base['scale'], offset=base['offset'] + 3 * (version - 1))
    elif family == 'schema':
        rule.update(flow_field='f1', level_field='h1', divisor=base['divisor'] + 3 * (version - 1))
    else:
        rule.update(endpoint=f'/api/legacy/{entity}', parameter='limit',
                    factor=base['factor'] + 3 * (version - 1))
    return rule

def _decoy_base(family, entity, rng):
    if family == 'calibration':
        return {'scale': rng.choice([500, 1000, 2000]), 'offset': rng.randrange(10, 30)}
    if family == 'schema':
        return {'divisor': rng.choice([100, 200])}
    return {'factor': rng.choice([100, 200])}

def _poisoned(rule):
    """把真规则的一个数值改错：肉眼看不出，判分是精确匹配所以必错。"""
    field, delta = POISON_OFFSET[rule['family']]
    bad = dict(rule)
    bad[field] = rule[field] + delta
    return bad

def build_tasks():
    rng = random.Random(SEED)
    tasks, rules, poison = [], {}, {}
    for item in entity_plan():
        entity, family, group = item['entity'], item['family'], item['group']
        versions = [1, 1, 1, 1] if item['stable'] else [1, 2, 3, 4]
        by_version = {}
        decoy_base = _decoy_base(family, entity, rng) if group == 'decoy-drift' else None
        for round_no, version in enumerate(versions, start=1):
            if version not in by_version:
                if decoy_base is not None:
                    rule = _decoy_rule(family, entity, decoy_base, version)
                else:
                    from run_multiround_study import make_rule
                    rule = make_rule(family, entity, version, rng)
                    while by_version and any(
                            {k: v for k, v in rule.items() if k != 'version'}
                            == {k: v for k, v in prev.items() if k != 'version'}
                            for prev in by_version.values()):
                        rule = make_rule(family, entity, version, rng)
                by_version[version] = rule
            rule = by_version[version]
            rules[f'{entity}@{round_no}'] = rule
            task = {'round': round_no, 'entity': entity, 'family': family, 'group': group,
                    'version': version, 'stable': item['stable']}
            if family == 'calibration':
                task['raw'] = rng.randrange(100, 10000)
            elif family == 'schema':
                # 版本升到 v4，载荷键也要覆盖到 v4（make_rule 把版本写进字段名）
                task['payload'] = {f'{k}{v}': rng.randrange(1, 10000) for k in ('f', 'h') for v in (1, 2, 3, 4)}
            else:
                task['raw'] = rng.randrange(1, 100)
            task['truth'] = _solve(rule, task)
            tasks.append(task)
        if group == 'poison-stable':
            poison[entity] = {'true': by_version[1], 'memory': _poisoned(by_version[1]),
                              'memory_id': f'{entity}-v1'}
    return tasks, rules, poison

def _solve(rule, task):
    from run_multiround_study import solve
    return solve(rule, task)

def build_archive(rules) -> str:
    from run_multiround_study import make_rule
    lines = ['# 契约手册（归档快照，单位 kPa，内部使用）',
             '# 每条 RULE 行是一个实体在一个文档版本下的参数；版本号必须与本轮任务一致。',
             '# 换算约定（对全部实体与版本一致，公式本身不随版本变化）：',
             '#   calibration: 工程值 = (raw - offset) / scale',
             '#   schema:      flow = payload[flow_field] / divisor, level = payload[level_field] / divisor',
             '#   tool:        端点 = endpoint, 参数名 = parameter, 参数值 = raw * factor']
    seen = set()
    for entity_round in sorted(rules):
        rule = rules[entity_round]
        key = (rule['entity'], rule['version'])
        if key in seen:
            continue
        seen.add(key)
        lines.append('RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False))
    rng = random.Random(SEED + 7)
    for index in range(6):
        lines.append('RULE ' + json.dumps(make_rule('calibration', f'retired{index:03}', 1, rng),
                                          sort_keys=True, ensure_ascii=False))
    return '\n'.join(lines) + '\n'

# ---------------------------------------------------------------- 阶段

def stage_bootstrap(out: Path):
    home = out / 'production'
    tasks, rules, poison = build_tasks()
    archive = build_archive(rules)
    (out / 'fixtures').mkdir(parents=True, exist_ok=True)
    archive_path = out / 'fixtures' / 'contract-manual-v1-v4.txt'
    archive_path.write_text(archive, encoding='utf-8')
    digest = sha256_text(archive)

    def setup(svc):
        row = svc.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (MEMBER,)).fetchone()
        if not row:
            svc.add_member(MEMBER, 'Real host driving Butian over MCP (stress arm)')
        svc.store.set_member_token(MEMBER, 'host-loop-token')
        return svc.learning.add_source('manual', str(archive_path), '契约手册（版本化归档，v1..v4）', digest,
                                       owner_member_id=MEMBER)

    source_id = service_call(home, setup)
    verified = service_call(home, lambda svc: svc.learning.verify_source_content(source_id, archive, owner_member_id=MEMBER))

    def seed(svc):
        """预置坏经验：warm + 两次独立成功观察，内容是错值。"""
        memory = svc.memory(MEMBER)
        statuses = {}
        try:
            for entity, item in poison.items():
                content = 'RULE ' + json.dumps(item['memory'], sort_keys=True, ensure_ascii=False)
                for tag in ('seed-a', 'seed-b'):
                    path = out / 'poison-evidence' / f'{entity}-{tag}.txt'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"entity={entity}; tag={tag}; source=upstream_extraction (simulated error)\n"
                                    f"content={content}\n", encoding='utf-8')
                    evidence = svc.record_evidence(path, owner_member_id=MEMBER, actor_member_id=MEMBER)
                    state = svc.record_practice_outcome(
                        MEMBER, memory_id=item['memory_id'], kind='lesson', content=content, outcome='success',
                        independent_key=f'{tag}-{entity}', evidence_ref=evidence)
                    statuses[entity] = state['status']
        finally:
            memory.close()
        return statuses

    seeded = service_call(home, seed)
    dump(out / 'fixtures' / 'tasks.json', tasks)
    dump(out / 'fixtures' / 'rules.json', {k: v for k, v in rules.items()})
    dump(out / 'fixtures' / 'poison.json', poison)
    dump(out / 'state.json', {'protocol': PROTOCOL, 'source_id': source_id, 'source_sha256': digest,
                              'archive': str(archive_path), 'created_at': time.time(), 'round': 0,
                              'poison_entities': sorted(poison)})
    print(f'[bootstrap] source_id={source_id} verified={verified} tasks={len(tasks)} poison={seeded}')

def _tasks(out: Path, round_no: int):
    return [t for t in load(out / 'fixtures' / 'tasks.json') if t['round'] == round_no]

def stage_recall(out: Path, round_no: int):
    items = []
    session = McpSession(out / 'production', 'host-loop-token', out / 'mcp-trace.jsonl')
    try:
        for task in _tasks(out, round_no):
            payload = session.call('butian_memory_recall', query=task['entity'], limit=RECALL_LIMIT,
                                   decision_id=f"r{round_no}-{task['entity']}")
            memories = [{'memory_id': m['memory_id'], 'status': m['status'], 'content': m['content'][:400],
                         'needs_revalidation': m.get('needs_revalidation'),
                         'environment_match': m.get('environment_match')}
                        for m in payload.get('memories', [])]
            items.append({'entity': task['entity'], 'family': task['family'], 'group': task['group'],
                          'version': task['version'], 'stable': task['stable'],
                          'question': question_text(task), 'recall_id': payload.get('recall_id'),
                          'no_match': payload.get('no_match'), 'memories': memories})
    finally:
        session.close()
    dump(out / 'stages' / f'r{round_no}-recall-request.json',
         {'round': round_no, 'items': items,
          'instruction': '对每个实体决定：是否已有与当前版本一致的可用经验（use_experience），'
                         '是否需要读源（need_source）。两者都 false 视为无依据作答，会被判 unknown。'})
    print(f'[recall r{round_no}] {len(items)} items -> stages/r{round_no}-recall-request.json')

def stage_source(out: Path, round_no: int):
    state = load(out / 'state.json')
    decisions = load(out / 'stages' / f'r{round_no}-recall-decision.json')
    tasks = {t['entity']: t for t in _tasks(out, round_no)}
    items, reads = [], 0
    session = McpSession(out / 'production', 'host-loop-token', out / 'mcp-trace.jsonl')
    try:
        for entity, decision in decisions.items():
            if not decision.get('need_source'):
                continue
            reads += 1
            payload = session.call('butian_knowledge_source_read', source_id=state['source_id'],
                                   member_id=MEMBER, limit=50000)
            items.append({'entity': entity, 'version': tasks[entity]['version'],
                          'sha256': payload.get('sha256'), 'content': payload.get('content', '')})
    finally:
        session.close()
    dump(out / 'stages' / f'r{round_no}-source-request.json',
         {'round': round_no, 'reads': reads, 'items': items})
    print(f'[source r{round_no}] reads={reads} -> stages/r{round_no}-source-request.json')

def _stale_rule(rules, entity, round_no):
    for previous in range(round_no - 1, 0, -1):
        rule = rules.get(f'{entity}@{previous}')
        if rule is not None:
            return rule
    return None

def _status_of(recall_items, entity, round_no, version):
    memory_id = f'{entity}-v{version}'
    for item in recall_items.get((round_no, entity), []):
        if item['memory_id'] == memory_id:
            return item['status']
    return None

def stage_settle(out: Path, round_no: int):
    home = out / 'production'
    tasks = _tasks(out, round_no)
    decisions = load(out / 'stages' / f'r{round_no}-recall-decision.json')
    answers = load(out / 'stages' / f'r{round_no}-answers.json')
    rules = load(out / 'fixtures' / 'rules.json')
    poison = load(out / 'fixtures' / 'poison.json')
    recall_raw = load(out / 'stages' / f'r{round_no}-recall-request.json')
    recall_items = {(round_no, item['entity']): item['memories'] for item in recall_raw['items']}
    results = []
    for task in tasks:
        entity, group = task['entity'], task['group']
        answer = answers.get(entity, {'unknown': True, 'basis': 'missing'})
        verdict = _score(task, answer)
        basis = answer.get('basis') or ('experience' if not decisions.get(entity, {}).get('need_source') else 'source')
        stale_error = False
        if verdict == 'wrong' and basis == 'experience' and group in ('clean-drift', 'decoy-drift'):
            stale = _stale_rule(rules, entity, round_no)
            if stale is not None and _score(task, _answer_from_rule(stale, task)) == 'correct':
                stale_error = True
        poisoned_reuse = False
        if group == 'poison-stable' and basis == 'experience':
            poisoned_reuse = _score(task, _answer_from_rule(poison[entity]['memory'], task)) == 'wrong'
        results.append({'round': round_no, 'entity': entity, 'family': task['family'], 'group': group,
                        'version': task['version'], 'stable': task['stable'],
                        'need_source': bool(decisions.get(entity, {}).get('need_source')),
                        'basis': basis, 'verdict': verdict, 'stale_version_error': stale_error,
                        'poisoned_reuse': poisoned_reuse,
                        'status_before': _status_of(recall_items, entity, round_no, task['version']),
                        'answer': answer, 'truth': task['truth']})

    def writeback(svc):
        memory = svc.memory(MEMBER)
        try:
            for row in results:
                if row['verdict'] == 'unknown':
                    continue
                rule = rules[f"{row['entity']}@{row['round']}"]
                content = 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)
                memory_id = f"{row['entity']}-v{rule['version']}"
                path = out / 'practice-evidence' / f"{memory_id}-r{round_no}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"entity={row['entity']}; round={round_no}; verdict={row['verdict']}; "
                                f"rule={json.dumps(rule, sort_keys=True, ensure_ascii=False)}", encoding='utf-8')
                evidence = svc.record_evidence(path, owner_member_id=MEMBER, actor_member_id=MEMBER)
                try:
                    outcome = 'success' if row['verdict'] == 'correct' else 'failure'
                    state = svc.record_practice_outcome(
                        MEMBER, memory_id=memory_id, kind='lesson', content=content, outcome=outcome,
                        independent_key=f'r{round_no}-{row["entity"]}', evidence_ref=evidence)
                    row['memory_status'] = state['status']
                except ValueError as error:
                    row['memory_status'] = f'error:{error}'
        finally:
            memory.close()
        return True

    service_call(home, writeback)
    statuses = service_call(home, lambda svc: _statuses(svc, results))
    for row in results:
        row['status_after'] = statuses.get(row['entity'])
    dump(out / 'stages' / f'r{round_no}-result.json', {'round': round_no, 'results': results})
    counts = {k: sum(1 for r in results if r['verdict'] == k) for k in ('correct', 'wrong', 'unknown')}
    print(f"[settle r{round_no}] {counts} reads={sum(1 for r in results if r['need_source'])} "
          f"stale={sum(r['stale_version_error'] for r in results)} "
          f"poisoned_reuse={sum(r['poisoned_reuse'] for r in results)}")

def _statuses(svc, results):
    memory = svc.memory(MEMBER)
    try:
        out = {}
        for row in results:
            found = memory.get(f"{row['entity']}-v{row['version']}")
            out[row['entity']] = None if found is None else found['status']
        return out
    finally:
        memory.close()

# ---------------------------------------------------------------- 汇总

def stage_report(out: Path):
    rounds = []
    for round_no in range(1, ROUNDS + 1):
        path = out / 'stages' / f'r{round_no}-result.json'
        if path.is_file():
            rounds.append(load(path)['results'])
    if not rounds:
        raise SystemExit('没有结算结果')
    rows = [row for block in rounds for row in block]
    by_round = {}
    for round_no, block in enumerate(rounds, start=1):
        for group in GROUPS:
            subset = [r for r in block if r['group'] == group]
            by_round[f'r{round_no}-{group}'] = {
                'n': len(subset),
                'correct': sum(r['verdict'] == 'correct' for r in subset),
                'wrong': sum(r['verdict'] == 'wrong' for r in subset),
                'unknown': sum(r['verdict'] == 'unknown' for r in subset),
                'reads': sum(1 for r in subset if r['need_source']),
                'experience_basis': sum(1 for r in subset if r['basis'] == 'experience'),
                'stale_version_errors': sum(1 for r in subset if r['stale_version_error']),
                'poisoned_reuse': sum(1 for r in subset if r['poisoned_reuse']),
            }

    clean_stable = [by_round[f'r{n}-clean-stable'] for n in range(1, ROUNDS + 1)]
    drift = [r for r in rows if r['group'] in ('clean-drift', 'decoy-drift')]
    drift_by_round = {n: [r for r in drift if r['round'] == n] for n in range(2, ROUNDS + 1)}
    n1 = all(block['reads'] == 0 for block in clean_stable[1:]) and all(block['correct'] == block['n'] for block in clean_stable)
    n2 = (all(sum(1 for r in block if r['need_source']) == len(block) for block in drift_by_round.values())
          and sum(1 for r in drift if r['stale_version_error']) == 0)

    poison_rows = [r for r in rows if r['group'] == 'poison-stable']
    poison_ids = sorted({r['entity'] for r in poison_rows})
    recovery = {}
    for entity in poison_ids:
        seq = [r for r in poison_rows if r['entity'] == entity]
        wrongs = [r['round'] for r in seq if r['verdict'] != 'correct']
        first_wrong = min(wrongs) if wrongs else None
        if first_wrong is None:
            recovery[entity] = {'first_wrong_round': None, 'recovered_round': None,
                                'read_rounds': [r['round'] for r in seq if r['need_source']],
                                'note': 'no_failure'}
        else:
            after = [r for r in seq if r['round'] > first_wrong]
            ok = all(r['need_source'] and r['verdict'] == 'correct' for r in after)
            recovery[entity] = {'first_wrong_round': first_wrong,
                                'recovered_round': (min(r['round'] for r in after
                                                        if r['need_source'] and r['verdict'] == 'correct')
                                                    if ok and after else None),
                                'read_rounds': [r['round'] for r in seq if r['need_source']],
                                'all_after_ok': ok}
    n3 = all(v.get('note') == 'no_failure' or v.get('all_after_ok') for v in recovery.values())
    exercised = [e for e, v in recovery.items() if v['first_wrong_round'] is not None]
    degrade_ok = True
    for entity in exercised:
        seq = [r for r in poison_rows if r['entity'] == entity]
        first = next(r for r in seq if r['round'] == recovery[entity]['first_wrong_round'])
        after = [r for r in seq if r['round'] == first['round'] + 1]
        if first['status_after'] != 'disputed':
            degrade_ok = False
        if after and not (not first['need_source'] and after[0]['need_source']):
            degrade_ok = False
    n4 = bool(exercised) and degrade_ok

    recall_map = {}
    for round_no in range(1, ROUNDS + 1):
        path = out / 'stages' / f'r{round_no}-recall-request.json'
        if path.is_file():
            for item in load(path)['items']:
                recall_map[(round_no, item['entity'])] = {m['memory_id']: m['status'] for m in item['memories']}
    recall_calls = set()
    trace_path = out / 'mcp-trace.jsonl'
    if trace_path.is_file():
        for line in trace_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get('tool') == 'butian_memory_recall':
                recall_calls.add(str(entry.get('arguments', {}).get('query', '')))
    n5_reasons = []
    for row in rows:
        key = (row['round'], row['entity'])
        if key not in recall_map:
            n5_reasons.append(f'no recall record for {key}')
        if row['entity'] not in recall_calls:
            n5_reasons.append(f'no recall tool call for {row["entity"]}')
        if row['basis'] == 'experience':
            claimed = (row['answer'] or {}).get('from_memory')
            if not claimed:
                n5_reasons.append(f'experience answer without from_memory for {key}')
            else:
                recalled = recall_map.get(key, {})
                if claimed not in recalled:
                    n5_reasons.append(f'claimed memory {claimed} absent from recall payload for {key}')
                elif recalled[claimed] == 'disputed' and not row['need_source']:
                    n5_reasons.append(f'disputed memory {claimed} used as sole basis for {key}')
    n5 = not n5_reasons

    summary = {
        'execution_kind': 'real_host_multiround_loop_stress',
        'protocol': PROTOCOL,
        'host_kind': 'stub' if STUB else 'real_model_current_conversation',
        'llm_inference_calls': None if STUB else 'unmetered: host is the conversational model, calls not instrumented',
        'model_weights_changed': False,
        'rounds': ROUNDS,
        'entities_per_family': PER_FAMILY,
        'total_answers': len(rows),
        'by_round': by_round,
        'criteria': {
            'N1_clean_reuse_holds': n1,
            'N2_version_gate_under_decoy': n2,
            'N3_poison_recovery_fast': n3,
            'N4_degradation_visible_and_used': n4,
            'N5_behaviour_auditable': n5,
        },
        'all_criteria_met': all((n1, n2, n3, n4, n5)),
        'poison_recovery': recovery,
        'n5_violations': n5_reasons,
        'recall_limit': RECALL_LIMIT,
        'channel_notes': [
            'butian_memory_recall 只做子串模糊匹配（查询按二字组命中，再按状态/权重/更新时间排序），'
            '没有按实体或标签的结构化过滤；宿主用 limit=%d 取回全部记忆后自行按内容里的 entity/version 过滤。' % RECALL_LIMIT,
            '记忆内容只在创建时写入：record_practice_outcome 不会修改已有记忆的 content，'
            '因此被写坏的内容无法通过后续成功实践纠正（只能降级 status），这一点在 poison-stable 上被直接观测到。',
        ],
        'limitations': [
            '宿主同时是实验设计者，非盲；v2 更重，因为宿主知道有坏经验组',
            '单宿主、单次运行，不能跨模型外推，不报告 p 值',
            '坏经验是 harness 直接注入的（带两份伪造证据），不是真实历史事故',
            '任务仍是契约化合成任务，真值来自 solve()',
            '不测量 token 成本与延迟，不声称模型参数变化',
        ],
        'rows': rows,
    }
    dump(out / 'summary.json', summary)
    print(json.dumps({'by_round': by_round, 'criteria': summary['criteria'],
                      'all_criteria_met': summary['all_criteria_met'],
                      'poison_recovery': recovery}, ensure_ascii=False, indent=2))

# ---------------------------------------------------------------- stub

def stub_decisions(out: Path, round_no: int):
    tasks = _tasks(out, round_no)
    rules = load(out / 'fixtures' / 'rules.json')
    decisions, answers = {}, {}
    for task in tasks:
        decisions[task['entity']] = {'use_experience': False, 'need_source': True}
        answers[task['entity']] = {**_answer_from_rule(rules[f"{task['entity']}@{task['round']}"], task),
                                   'basis': 'source'}
    dump(out / 'stages' / f'r{round_no}-recall-decision.json', decisions)
    dump(out / 'stages' / f'r{round_no}-answers.json', answers)

def main(argv=None):
    global STUB
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True)
    parser.add_argument('--stage', required=True,
                        choices=['bootstrap', 'recall', 'source', 'settle', 'report', 'stub'])
    parser.add_argument('--round', type=int, default=0)
    parser.add_argument('--stub', action='store_true')
    args = parser.parse_args(argv)
    STUB = args.stub
    out = Path(args.out)
    if not out.is_absolute():
        out = (HERE / out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.stage == 'bootstrap':
        stage_bootstrap(out)
    elif args.stage == 'recall':
        stage_recall(out, args.round)
    elif args.stage == 'source':
        stage_source(out, args.round)
    elif args.stage == 'settle':
        stage_settle(out, args.round)
    elif args.stage == 'stub':
        stub_decisions(out, args.round)
    else:
        stage_report(out)

if __name__ == '__main__':
    main()
