"""Host-loop v3（修复后才做得出来的三问）: 误伤、内容级修订、规模。

与 v1 / v2 共用同一套基础设施与同一判分函数。三组 × 三族 = 9 个实体 × 4 轮 = 36 次作答：

  * false-disputed  内容**正确**，两条独立成功 → warm，再补一条失败 → disputed
                    问：误伤之后会不会答错？复用收益还剩多少？能不能自愈？
  * repair          内容是**错值**，两条独立成功 → warm
                    问：R1 误用被判错之后，`butian_memory_revise` 能不能把它救回可复用
  * scale           正常 v1 记忆 + 库内另有 300 条同族无关记忆（每族 100 条，名字 {family}5xx）
                    问：几百条记忆时召回还准不准、宿主还顶不顶得住

宿主只能做三件事：`butian_memory_recall`、`butian_knowledge_source_read`、提交答案；
经验写回与 R1 之后的内容修订一律由本 harness 在客观判分之后执行。

宿主策略（协议第 2 节已冻结）：召回里有 warm/hot 且版本一致 → 用经验；candidate/disputed/缺版本 → 读源。

阶段（每次调用开一个 MCP 会话，状态落盘）：

  --stage bootstrap          成员、源快照、300 条无关记忆、误伤与坏经验预置
  --stage recall  --round N  对每个实体跑 butian_memory_recall
  -> 宿主写 stages/rN-recall-decision.json
  --stage source  --round N  对请求读源的实体跑 butian_knowledge_source_read
  -> 宿主写 stages/rN-answers.json
  --stage settle  --round N  判分、写证据与实践观察、R1 对 repair 组执行内容修订
  --stage report             聚合 M1..M4，写 summary.json

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

PROTOCOL = 'host-loop-v3'
SEED = 20261102
ROUNDS = 4
FAMILIES = ['calibration', 'schema', 'tool']
GROUPS = ('false-disputed', 'repair', 'scale')
MEMBER = 'host'
STUB = False
RECALL_LIMIT = 100
DECOYS_PER_FAMILY = 100
DECOY_BASE = 500
GROUP_OF_SUFFIX = {1: 'false-disputed', 2: 'repair', 3: 'scale'}
POISON_OFFSET = {'calibration': ('offset', 37), 'schema': ('divisor', 7), 'tool': ('factor', 7)}

# ---------------------------------------------------------------- 任务流

def entity_plan():
    plan = []
    for family in FAMILIES:
        for suffix, group in GROUP_OF_SUFFIX.items():
            plan.append({'entity': f'{family}{suffix}01', 'family': family, 'group': group})
    return plan

def _poisoned(rule):
    """把真规则的一个数值改错：肉眼看不出，判分是精确匹配所以必错。"""
    field, delta = POISON_OFFSET[rule['family']]
    bad = dict(rule)
    bad[field] = rule[field] + delta
    return bad

def build_tasks():
    from run_multiround_study import make_rule
    rng = random.Random(SEED)
    tasks, rules = [], {}
    for item in entity_plan():
        entity, family = item['entity'], item['family']
        rule = make_rule(family, entity, 1, rng)
        for round_no in range(1, ROUNDS + 1):
            rules[f'{entity}@{round_no}'] = rule
            task = {'round': round_no, 'entity': entity, 'family': family, 'group': item['group'],
                    'version': 1, 'stable': True}
            if family == 'calibration':
                task['raw'] = rng.randrange(100, 10000)
            elif family == 'schema':
                task['payload'] = {f'{k}{v}': rng.randrange(1, 10000) for k in ('f', 'h') for v in (1, 2, 3, 4)}
            else:
                task['raw'] = rng.randrange(1, 100)
            task['truth'] = _solve(rule, task)
            tasks.append(task)
    return tasks, rules

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
        lines.append('RULE ' + json.dumps(make_rule('calibration', f'retired{index:03d}', 1, rng),
                                          sort_keys=True, ensure_ascii=False))
    return '\n'.join(lines) + '\n'

def _decoy_memories():
    """300 条同族无关记忆：名字是 {family}5xx，与 101/201/301 不共享长词。"""
    from run_multiround_study import make_rule
    rng = random.Random(SEED + 11)
    rows = []
    for family in FAMILIES:
        for index in range(DECOYS_PER_FAMILY):
            entity = f'{family}{DECOY_BASE + index:03d}'
            rule = make_rule(family, entity, 1, rng)
            rows.append({'memory_id': f'{entity}-v1', 'entity': entity, 'family': family,
                         'content': 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)})
    return rows

# ---------------------------------------------------------------- 阶段

def stage_bootstrap(out: Path):
    home = out / 'production'
    tasks, rules = build_tasks()
    archive = build_archive(rules)
    (out / 'fixtures').mkdir(parents=True, exist_ok=True)
    archive_path = out / 'fixtures' / 'contract-manual-v1.txt'
    archive_path.write_text(archive, encoding='utf-8')
    digest = sha256_text(archive)
    decoys = _decoy_memories()

    def setup(svc):
        row = svc.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (MEMBER,)).fetchone()
        if not row:
            svc.add_member(MEMBER, 'Real host driving Butian over MCP (v3 arm)')
        svc.store.set_member_token(MEMBER, 'host-loop-token')
        return svc.learning.add_source('manual', str(archive_path), '契约手册（版本化归档，v1）', digest,
                                       owner_member_id=MEMBER)

    source_id = service_call(home, setup)
    verified = service_call(home, lambda svc: svc.learning.verify_source_content(source_id, archive, owner_member_id=MEMBER))

    def evidence_file(name: str, body: str) -> Path:
        path = out / 'seed-evidence' / f'{name}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        return path

    def seed(svc):
        """预置：300 条无关记忆 + 误伤组（内容正确却 disputed）+ 修复组（内容是错值却 warm）。"""
        memory = svc.memory(MEMBER)
        statuses = {}
        try:
            for row in decoys:
                memory.put(row['memory_id'], 'lesson', row['content'], confidence=0.6, weight=1.5,
                           status='warm', source_event_ids=[f"seed-decoy-{row['memory_id']}"])
            for item in entity_plan():
                entity, group = item['entity'], item['group']
                memory_id = f'{entity}-v1'
                true_rule = rules[f'{entity}@1']
                content = 'RULE ' + json.dumps(true_rule, sort_keys=True, ensure_ascii=False)
                if group == 'repair':
                    content = 'RULE ' + json.dumps(_poisoned(true_rule), sort_keys=True, ensure_ascii=False)
                for tag in ('seed-a', 'seed-b'):
                    path = evidence_file(f'{entity}-{tag}',
                                         f'entity={entity}; tag={tag}; group={group}; '
                                         f'source=upstream_extraction\ncontent={content}\n')
                    evidence = svc.record_evidence(path, owner_member_id=MEMBER, actor_member_id=MEMBER)
                    state = svc.record_practice_outcome(
                        MEMBER, memory_id=memory_id, kind='lesson', content=content, outcome='success',
                        independent_key=f'{tag}-{entity}', evidence_ref=evidence)
                    statuses[entity] = state['status']
                if group == 'false-disputed':
                    # 误伤：内容正确，但被一条失败观察打成 disputed。
                    state = svc.record_practice_outcome(
                        MEMBER, memory_id=memory_id, kind='lesson', content=content, outcome='failure',
                        independent_key=f'false-alarm-{entity}')
                    statuses[entity] = state['status']
        finally:
            memory.close()
        return statuses

    seeded = service_call(home, seed)
    dump(out / 'fixtures' / 'tasks.json', tasks)
    dump(out / 'fixtures' / 'rules.json', {k: v for k, v in rules.items()})
    dump(out / 'fixtures' / 'decoys.json', {'count': len(decoys), 'sample': decoys[:3],
                                            'ids': [row['memory_id'] for row in decoys]})
    dump(out / 'state.json', {'protocol': PROTOCOL, 'source_id': source_id, 'source_sha256': digest,
                              'archive': str(archive_path), 'created_at': time.time(), 'round': 0,
                              'decoy_count': len(decoys),
                              'seeded_status': seeded})
    print(f'[bootstrap] source_id={source_id} verified={verified} tasks={len(tasks)} '
          f'decoys={len(decoys)} seeded={seeded}')

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
            own = f"{task['entity']}-v1"
            items.append({'entity': task['entity'], 'family': task['family'], 'group': task['group'],
                          'version': task['version'],
                          'question': question_text(task), 'recall_id': payload.get('recall_id'),
                          'no_match': payload.get('no_match'), 'memories': memories,
                          'n_memories': len(memories),
                          'irrelevant': sum(1 for m in memories if m['memory_id'] != own)})
    finally:
        session.close()
    dump(out / 'stages' / f'r{round_no}-recall-request.json',
         {'round': round_no, 'items': items,
          'instruction': '宿主策略（协议第 2 节，已冻结）：召回里有 warm/hot 且版本一致 → 用经验作答；'
                         'candidate/disputed/缺版本 → 读源；两者都不成立 → unknown。'})
    print(f"[recall r{round_no}] {len(items)} items, "
          f"payload sizes={[item['n_memories'] for item in items]}, "
          f"irrelevant={sum(item['irrelevant'] for item in items)} "
          f"-> stages/r{round_no}-recall-request.json")

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

def _status_of(recall_items, round_no, entity):
    for item in recall_items.get((round_no, entity), []):
        if item['memory_id'] == f'{entity}-v1':
            return item['status']
    return None

def stage_settle(out: Path, round_no: int):
    home = out / 'production'
    tasks = _tasks(out, round_no)
    decisions = load(out / 'stages' / f'r{round_no}-recall-decision.json')
    answers = load(out / 'stages' / f'r{round_no}-answers.json')
    rules = load(out / 'fixtures' / 'rules.json')
    recall_raw = load(out / 'stages' / f'r{round_no}-recall-request.json')
    recall_items = {(round_no, item['entity']): item['memories'] for item in recall_raw['items']}
    results = []
    for task in tasks:
        entity, group = task['entity'], task['group']
        answer = answers.get(entity, {'unknown': True, 'basis': 'missing'})
        verdict = _score(task, answer)
        basis = answer.get('basis') or ('experience' if not decisions.get(entity, {}).get('need_source') else 'source')
        results.append({'round': round_no, 'entity': entity, 'family': task['family'], 'group': group,
                        'version': task['version'],
                        'need_source': bool(decisions.get(entity, {}).get('need_source')),
                        'basis': basis, 'verdict': verdict,
                        'status_before': _status_of(recall_items, round_no, entity),
                        'recall_ids': [m['memory_id'] for m in recall_items.get((round_no, entity), [])],
                        'answer': answer, 'truth': task['truth']})

    def writeback(svc):
        memory = svc.memory(MEMBER)
        try:
            for row in results:
                if row['verdict'] == 'unknown':
                    continue
                rule = rules[f"{row['entity']}@{round_no}"]
                content = 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)
                memory_id = f"{row['entity']}-v1"
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
                    row['successes'] = state['successes']
                    row['failures'] = state['failures']
                except ValueError as error:
                    row['memory_status'] = f'error:{error}'
        finally:
            memory.close()
        return True

    service_call(home, writeback)

    # R1 之后：对修复组执行内容级修订（证据必须是这条记忆没用过的新证据）。
    revision = {}
    if round_no == 1:
        def repair(svc):
            out_map = {}
            for row in results:
                if row['group'] != 'repair':
                    continue
                rule = rules[f"{row['entity']}@1"]
                content = 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)
                path = out / 'revision-evidence' / f"{row['entity']}-r1-fix.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"entity={row['entity']}; round=1; kind=corrected_measurement; "
                                f"content={content}", encoding='utf-8')
                evidence = svc.record_evidence(path, owner_member_id=MEMBER, actor_member_id=MEMBER)
                out_map[row['entity']] = svc.revise_memory(
                    MEMBER, f"{row['entity']}-v1", content=content,
                    reason='R1 误用坏值被判错，按新测量证据改写内容',
                    evidence_refs=[evidence])
            return out_map
        revision = service_call(home, repair)

    statuses = service_call(home, lambda svc: _statuses(svc, results))
    for row in results:
        row['status_after'] = statuses.get(row['entity'])
    dump(out / 'stages' / f'r{round_no}-result.json',
         {'round': round_no, 'results': results, 'revision': revision})
    counts = {k: sum(1 for r in results if r['verdict'] == k) for k in ('correct', 'wrong', 'unknown')}
    print(f"[settle r{round_no}] {counts} reads={sum(1 for r in results if r['need_source'])} "
          f"revision={sorted(revision)}")

def _statuses(svc, results):
    memory = svc.memory(MEMBER)
    try:
        out = {}
        for row in results:
            found = memory.get(f"{row['entity']}-v1")
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
                'status_before': {r['entity']: r['status_before'] for r in subset},
                'status_after': {r['entity']: r.get('status_after') for r in subset},
            }

    recall_raw = {}
    for round_no in range(1, ROUNDS + 1):
        path = out / 'stages' / f'r{round_no}-recall-request.json'
        if path.is_file():
            for item in load(path)['items']:
                recall_raw[(round_no, item['entity'])] = item

    # M1 误伤：全对 + 全读源 + 从不用 disputed 作依据 + R4 仍是 disputed
    false_dispute = [r for r in rows if r['group'] == 'false-disputed']
    m1_all_correct = bool(false_dispute) and all(r['verdict'] == 'correct' for r in false_dispute)
    m1_all_read = bool(false_dispute) and all(r['need_source'] for r in false_dispute)
    m1_no_disputed_basis = all(not (r['basis'] == 'experience') for r in false_dispute)
    m1_still_disputed = bool(false_dispute) and all(
        r['status_after'] == 'disputed' for r in false_dispute if r['round'] == ROUNDS)
    m1 = m1_all_correct and m1_all_read and m1_no_disputed_basis and m1_still_disputed

    # M2 修订：R1 误用被判错 + R2..R4 全对 + R4 不读源且用经验 + 修订确实发生
    repair_rows = [r for r in rows if r['group'] == 'repair']
    m2_r1_poisoned_use = any(r['round'] == 1 and r['basis'] == 'experience' and r['verdict'] == 'wrong'
                             for r in repair_rows)
    m2_after_correct = bool(repair_rows) and all(
        r['verdict'] == 'correct' for r in repair_rows if r['round'] >= 2)
    m2_r4_reuse = bool(repair_rows) and all(
        (not r['need_source']) and r['basis'] == 'experience'
        for r in repair_rows if r['round'] == ROUNDS)
    revision_rows = load(out / 'stages' / 'r1-result.json').get('revision', {}) if (out / 'stages' / 'r1-result.json').is_file() else {}
    m2_revision_done = len(revision_rows) == len([r for r in repair_rows if r['round'] == 1])
    m2 = m2_r1_poisoned_use and m2_after_correct and m2_r4_reuse and m2_revision_done

    # M3 规模：无关条数 0、单次条数中位 <= 5、scale 组全对且 R2..R4 复用
    scale_recalls = [recall_raw[key] for key in recall_raw if recall_raw[key]['group'] == 'scale']
    irrelevant = sum(item['irrelevant'] for item in scale_recalls)
    sizes = sorted(item['n_memories'] for item in scale_recalls)
    median_size = sizes[len(sizes) // 2] if sizes else None
    scale_rows = [r for r in rows if r['group'] == 'scale']
    m3_correct = bool(scale_rows) and all(r['verdict'] == 'correct' for r in scale_rows)
    m3_reuse = all(not r['need_source'] for r in scale_rows)
    m3 = irrelevant == 0 and median_size is not None and median_size <= 5 and m3_correct and m3_reuse

    # M4 可审计（v2 的 N5 口径）
    recall_calls = set()
    trace_path = out / 'mcp-trace.jsonl'
    if trace_path.is_file():
        for line in trace_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get('tool') == 'butian_memory_recall':
                recall_calls.add(str(entry.get('arguments', {}).get('query', '')))
    m4_reasons = []
    for row in rows:
        key = (row['round'], row['entity'])
        item = recall_raw.get(key)
        if item is None:
            m4_reasons.append(f'no recall record for {key}')
            continue
        if row['entity'] not in recall_calls:
            m4_reasons.append(f'no recall tool call for {row["entity"]}')
        if row['basis'] == 'experience':
            claimed = (row['answer'] or {}).get('from_memory')
            if not claimed:
                m4_reasons.append(f'experience answer without from_memory for {key}')
            else:
                recalled = {m['memory_id']: m['status'] for m in item['memories']}
                if claimed not in recalled:
                    m4_reasons.append(f'claimed memory {claimed} absent from recall payload for {key}')
                elif recalled[claimed] == 'disputed' and not row['need_source']:
                    m4_reasons.append(f'disputed memory {claimed} used as sole basis for {key}')
    m4 = not m4_reasons

    if all((m1, m2, m3, m4)):
        verdict = 'v3_false_dispute_and_repair_confirmed'
    elif not m1:
        verdict = 'false_dispute_produces_wrong_answers'
    elif not m2:
        verdict = 'revision_insufficient_to_restore_reuse'
    elif not m3:
        verdict = 'scale_degrades_recall_or_answers'
    else:
        verdict = 'behaviour_not_auditable'

    summary = {
        'execution_kind': 'real_host_multiround_loop_v3',
        'protocol': PROTOCOL,
        'verdict': verdict,
        'host_kind': 'stub' if STUB else 'real_model_current_conversation',
        'llm_inference_calls': None if STUB else 'unmetered: host is the conversational model, calls not instrumented',
        'model_weights_changed': False,
        'rounds': ROUNDS,
        'entities': len({r['entity'] for r in rows}),
        'total_answers': len(rows),
        'decoy_count': load(out / 'fixtures' / 'decoys.json')['count'],
        'by_round': by_round,
        'criteria': {
            'M1_false_dispute_no_wrong_answer_but_zero_reuse': m1,
            'M2_revision_restores_reuse': m2,
            'M3_scale_keeps_recall_precise': m3,
            'M4_behaviour_auditable': m4,
        },
        'm1_detail': {'all_correct': m1_all_correct, 'all_read_source': m1_all_read,
                      'no_experience_basis': m1_no_disputed_basis, 'still_disputed_at_end': m1_still_disputed},
        'm2_detail': {'r1_poisoned_use_and_wrong': m2_r1_poisoned_use, 'r2_r4_all_correct': m2_after_correct,
                      'r4_reuse_without_read': m2_r4_reuse, 'revision_executed': m2_revision_done,
                      'revisions': {k: {'previous_status': v.get('previous_status'), 'status': v.get('status')}
                                    for k, v in revision_rows.items()}},
        'm3_detail': {'irrelevant_total': irrelevant, 'recall_sizes': sizes,
                      'median_size': median_size, 'all_correct': m3_correct, 'reuse_all_rounds': m3_reuse},
        'm4_violations': m4_reasons,
        'recall_limit': RECALL_LIMIT,
        'limitations': [
            '宿主同时是实验设计者，非盲；本轮更重，因为宿主知道三组构造',
            '单宿主、单次运行，不能跨模型外推，不报告 p 值',
            '坏经验、误伤、300 条无关记忆都是 harness 直接注入的构造，不是真实历史事故',
            '任务是契约化合成任务，真值来自 solve()',
            '不测量 token 成本与延迟，不声称模型参数变化',
            'repair 组的恢复节律由「successes>=2 且无失败 → warm」这条规则决定，不是普适常数',
            'scale 组只到 300 条；千条以上的召回仍用子串匹配 + IDF 闸门，不外推',
        ],
        'rows': rows,
    }
    dump(out / 'summary.json', summary)
    print(json.dumps({'verdict': verdict, 'criteria': summary['criteria'],
                      'm1_detail': summary['m1_detail'], 'm2_detail': summary['m2_detail'],
                      'm3_detail': summary['m3_detail']}, ensure_ascii=False, indent=2))

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
