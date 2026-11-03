"""Host-loop v4: 连续漂移 + 版本元数据的消融对照。

协议见 `实验协议-真实宿主多轮-v4.md`（跑之前已冻结）。2 组 × 2 臂 × 3 族 = 12 实体
× 4 轮 = 48 次作答：

  组 drift    源文档第 n 轮是 v(n)，记忆全程停在 v1
  组 stable   四轮都是 v1（反假绿对照）

  臂 gated    召回原样返回 memory_id / status / content（content 内含 version）
  臂 naive    召回只给 content，且抹掉其中的 version 字段（模拟"只存文本"的记忆模块）

两臂共用同一个 butian 库、同一批 warm 记忆，差别只在**召回呈现层**。宿主消费规则
两臂完全相同（协议第 2 节）。本档**不写回、不登记实践观察**，把漂移单独隔出来。

阶段（每次调用开一个 MCP 会话，状态落盘）：

  --stage bootstrap          成员、源快照、12 条 v1 经验预置为 warm
  --stage recall  --round N  跑 butian_memory_recall，按臂裁剪成宿主可见的呈现层
  -> 宿主写 stages/rN-recall-decision.json
  --stage source  --round N  对请求读源的实体跑 butian_knowledge_source_read
  -> 宿主写 stages/rN-answers.json
  --stage settle  --round N  判分、算 stale_match、核对记忆终态（只读）
  --stage report             聚合 K1..K4，写 summary.json

召回的**原始**返回落在 stages/rN-recall-audit.json，与宿主可见的 recall-request.json
分开：后者才是宿主做决策时读的文件，避免 naive 臂被元数据污染。

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

PROTOCOL = 'host-loop-v4'
SEED = 20261103
ROUNDS = 4
FAMILIES = ['calibration', 'schema', 'tool']
GROUPS = ('drift', 'stable')
ARMS = ('gated', 'naive')
MEMBER = 'host'
STUB = False
RECALL_LIMIT = 100
SUFFIX_MAP = {1: ('drift', 'gated'), 2: ('drift', 'naive'),
              3: ('stable', 'gated'), 4: ('stable', 'naive')}

# ---------------------------------------------------------------- 任务流

def entity_plan():
    plan = []
    for family in FAMILIES:
        for suffix, (group, arm) in SUFFIX_MAP.items():
            plan.append({'entity': f'{family}{suffix}01', 'family': family,
                         'group': group, 'arm': arm})
    return plan

def _params(rule):
    """规则的数值内核（不含 entity/version 这些标识字段），用于确认漂移是真的变了。"""
    return {k: v for k, v in rule.items() if k not in ('entity', 'version')}

def build_tasks():
    from run_multiround_study import make_rule, solve
    rng = random.Random(SEED)
    tasks, rules = [], {}
    for item in entity_plan():
        entity, family, group = item['entity'], item['family'], item['group']
        base = make_rule(family, entity, 1, rng)
        versions = {1: base}
        if group == 'drift':
            for version in range(2, ROUNDS + 1):
                rule = make_rule(family, entity, version, rng)
                # 漂移必须真的改变答案：参数与 v1 相同就重抽，直到不同。
                while _params(rule) == _params(base):
                    rule = make_rule(family, entity, version, rng)
                versions[version] = rule
        for round_no in range(1, ROUNDS + 1):
            version = round_no if group == 'drift' else 1
            rule = versions[version]
            rules[f'{entity}@{round_no}'] = rule
            task = {'round': round_no, 'entity': entity, 'family': family,
                    'group': group, 'arm': item['arm'], 'version': version,
                    'stable': group == 'stable'}
            if family == 'calibration':
                task['raw'] = rng.randrange(100, 10000)
            elif family == 'schema':
                task['payload'] = {f'{k}{v}': rng.randrange(1, 10000)
                                   for k in ('f', 'h') for v in range(1, ROUNDS + 1)}
            else:
                task['raw'] = rng.randrange(1, 100)
            task['truth'] = solve(rule, task)
            tasks.append(task)
    return tasks, rules

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

# ---------------------------------------------------------------- 呈现层

def _strip_version(content: str) -> str:
    """naive 臂：把 content 里的 version 字段抹掉，其余原样。"""
    if not content.startswith('RULE '):
        return content
    try:
        rule = json.loads(content[5:])
    except ValueError:
        return content
    rule.pop('version', None)
    return 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)

def present(memories, arm: str):
    """同一个召回返回，两种呈现。naive 只给内容，且内容里没有版本。"""
    if arm == 'gated':
        return [{'memory_id': m['memory_id'], 'status': m['status'],
                 'content': m['content'][:400],
                 'needs_revalidation': m.get('needs_revalidation'),
                 'environment_match': m.get('environment_match')} for m in memories]
    return [{'content': _strip_version(m['content'][:400])} for m in memories]

# ---------------------------------------------------------------- 阶段

def stage_bootstrap(out: Path):
    home = out / 'production'
    tasks, rules = build_tasks()
    archive = build_archive(rules)
    (out / 'fixtures').mkdir(parents=True, exist_ok=True)
    archive_path = out / 'fixtures' / 'contract-manual-v1-v4.txt'
    archive_path.write_text(archive, encoding='utf-8')
    digest = sha256_text(archive)

    def setup(svc):
        row = svc.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (MEMBER,)).fetchone()
        if not row:
            svc.add_member(MEMBER, 'Real host driving Butian over MCP (v4 ablation)')
        svc.store.set_member_token(MEMBER, 'host-loop-token')
        return svc.learning.add_source('manual', str(archive_path), '契约手册（版本化归档，v1-v4）', digest,
                                       owner_member_id=MEMBER)

    source_id = service_call(home, setup)
    verified = service_call(home, lambda svc: svc.learning.verify_source_content(
        source_id, archive, owner_member_id=MEMBER))

    def evidence_file(name: str, body: str) -> Path:
        path = out / 'seed-evidence' / f'{name}.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        return path

    def seed(svc):
        """12 个实体各以 v1 规则、两条独立成功预置为 warm。两臂完全同构。"""
        memory = svc.memory(MEMBER)
        statuses = {}
        try:
            for item in entity_plan():
                entity = item['entity']
                memory_id = f'{entity}-v1'
                rule = rules[f'{entity}@1']
                content = 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)
                for tag in ('seed-a', 'seed-b'):
                    path = evidence_file(f'{entity}-{tag}',
                                         f'entity={entity}; tag={tag}; group={item["group"]}; arm={item["arm"]}; '
                                         f'source=upstream_extraction\ncontent={content}\n')
                    evidence = svc.record_evidence(path, owner_member_id=MEMBER, actor_member_id=MEMBER)
                    state = svc.record_practice_outcome(
                        MEMBER, memory_id=memory_id, kind='lesson', content=content, outcome='success',
                        independent_key=f'{tag}-{entity}', evidence_ref=evidence)
                    statuses[entity] = state['status']
        finally:
            memory.close()
        return statuses

    seeded = service_call(home, seed)
    dump(out / 'fixtures' / 'tasks.json', tasks)
    dump(out / 'fixtures' / 'rules.json', rules)
    dump(out / 'state.json', {'protocol': PROTOCOL, 'source_id': source_id, 'source_sha256': digest,
                              'archive': str(archive_path), 'created_at': time.time(), 'round': 0,
                              'seeded_status': seeded})
    print(f'[bootstrap] source_id={source_id} verified={verified} tasks={len(tasks)} '
          f'entities={len(entity_plan())} seeded={sorted(set(seeded.values()))}')

def _tasks(out: Path, round_no: int):
    return [t for t in load(out / 'fixtures' / 'tasks.json') if t['round'] == round_no]

def stage_recall(out: Path, round_no: int):
    items, audit = [], []
    session = McpSession(out / 'production', 'host-loop-token', out / 'mcp-trace.jsonl')
    try:
        for task in _tasks(out, round_no):
            payload = session.call('butian_memory_recall', query=task['entity'], limit=RECALL_LIMIT,
                                   decision_id=f"r{round_no}-{task['entity']}")
            memories = payload.get('memories', [])
            shown = present(memories, task['arm'])
            items.append({'entity': task['entity'], 'family': task['family'],
                          'group': task['group'], 'arm': task['arm'], 'version': task['version'],
                          'question': question_text(task), 'recall_id': payload.get('recall_id'),
                          'no_match': payload.get('no_match'), 'memories': shown,
                          'n_memories': len(shown)})
            audit.append({'entity': task['entity'], 'arm': task['arm'], 'group': task['group'],
                          'raw': [{'memory_id': m['memory_id'], 'status': m['status'],
                                 'content': m['content'][:400]} for m in memories]})
    finally:
        session.close()
    dump(out / 'stages' / f'r{round_no}-recall-request.json',
         {'round': round_no, 'items': items,
          'instruction': '宿主策略（协议第 2 节，已冻结）：warm/hot 且版本一致 → 用经验作答；'
                         '召回项不含任何版本与状态信息 → 按内容直接作答；'
                         '版本不一致或 candidate/disputed → 读源；都不成立 → unknown。'})
    dump(out / 'stages' / f'r{round_no}-recall-audit.json', {'round': round_no, 'items': audit})
    print(f"[recall r{round_no}] {len(items)} items "
          f"gated={sum(1 for i in items if i['arm'] == 'gated')} "
          f"naive={sum(1 for i in items if i['arm'] == 'naive')} "
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

def _memory_state(svc, entities):
    """只读核对：记忆的内容版本与状态。本档不写回，所以四轮应当一模一样。"""
    memory = svc.memory(MEMBER)
    try:
        out = {}
        for entity in entities:
            found = memory.get(f'{entity}-v1')
            if found is None:
                out[entity] = None
                continue
            content = found.get('content') or ''
            version = None
            if content.startswith('RULE '):
                try:
                    version = json.loads(content[5:]).get('version')
                except ValueError:
                    version = None
            out[entity] = {'status': found['status'], 'content_version': version}
        return out
    finally:
        memory.close()

def stage_settle(out: Path, round_no: int):
    tasks = _tasks(out, round_no)
    decisions = load(out / 'stages' / f'r{round_no}-recall-decision.json')
    answers = load(out / 'stages' / f'r{round_no}-answers.json')
    rules = load(out / 'fixtures' / 'rules.json')
    results = []
    for task in tasks:
        entity = task['entity']
        answer = answers.get(entity, {'unknown': True, 'basis': 'missing'})
        verdict = _score(task, answer)
        need_source = bool(decisions.get(entity, {}).get('need_source'))
        basis = answer.get('basis') or ('source' if need_source else 'experience')
        # 沿用旧版本 = 答案精确等于用 v1 规则算出来的答案。
        stale_task = dict(task, truth=_truth_of(rules[f'{entity}@1'], task))
        stale_match = _score(stale_task, answer) == 'correct'
        results.append({'round': round_no, 'entity': entity, 'family': task['family'],
                        'group': task['group'], 'arm': task['arm'], 'version': task['version'],
                        'need_source': need_source, 'basis': basis, 'verdict': verdict,
                        'stale_match': stale_match,
                        'stale_differs_from_truth': stale_task['truth'] != task['truth'],
                        'answer': answer, 'truth': task['truth']})
    states = service_call(out / 'production',
                          lambda svc: _memory_state(svc, [t['entity'] for t in tasks]))
    for row in results:
        state = states.get(row['entity']) or {}
        row['memory_status'] = state.get('status')
        row['memory_version'] = state.get('content_version')
    dump(out / 'stages' / f'r{round_no}-result.json', {'round': round_no, 'results': results})
    counts = {k: sum(1 for r in results if r['verdict'] == k) for k in ('correct', 'wrong', 'unknown')}
    print(f"[settle r{round_no}] {counts} reads={sum(1 for r in results if r['need_source'])} "
          f"stale_match={sum(1 for r in results if r['stale_match'])}")

def _truth_of(rule, task):
    from run_multiround_study import solve
    return solve(rule, task)

# ---------------------------------------------------------------- 汇总

def _cell(rows):
    return {'n': len(rows),
            'correct': sum(r['verdict'] == 'correct' for r in rows),
            'wrong': sum(r['verdict'] == 'wrong' for r in rows),
            'unknown': sum(r['verdict'] == 'unknown' for r in rows),
            'reads': sum(1 for r in rows if r['need_source']),
            'experience_basis': sum(1 for r in rows if r['basis'] == 'experience'),
            'stale_match': sum(1 for r in rows if r['stale_match'])}

def stage_report(out: Path):
    rounds = []
    for round_no in range(1, ROUNDS + 1):
        path = out / 'stages' / f'r{round_no}-result.json'
        if path.is_file():
            rounds.append(load(path)['results'])
    if len(rounds) != ROUNDS:
        raise SystemExit(f'只找到 {len(rounds)}/{ROUNDS} 轮结算结果')
    rows = [row for block in rounds for row in block]

    def pick(group=None, arm=None, rmin=1, rmax=ROUNDS):
        return [r for r in rows
                if (group is None or r['group'] == group)
                and (arm is None or r['arm'] == arm)
                and rmin <= r['round'] <= rmax]

    by_round = {}
    for round_no, block in enumerate(rounds, start=1):
        for group in GROUPS:
            for arm in ARMS:
                by_round[f'r{round_no}-{group}-{arm}'] = _cell(
                    [r for r in block if r['group'] == group and r['arm'] == arm])
    by_cell = {f'{g}-{a}': _cell(pick(g, a)) for g in GROUPS for a in ARMS}

    gated = pick(arm='gated')
    gated_drift_late = pick('drift', 'gated', rmin=2)
    gated_stable = pick('stable', 'gated')
    naive_drift_late = pick('drift', 'naive', rmin=2)
    naive_drift = pick('drift', 'naive')
    naive_stable = pick('stable', 'naive')

    k1 = (len(gated) == 24 and all(r['verdict'] == 'correct' for r in gated)
          and len(gated_drift_late) == 9 and all(r['need_source'] for r in gated_drift_late)
          and all(not r['need_source'] for r in gated_stable))
    k2 = (len(naive_drift_late) == 9 and all(r['verdict'] == 'wrong' for r in naive_drift_late)
          and all(not r['need_source'] for r in naive_drift)
          and all(r['stale_match'] for r in naive_drift_late))
    k3 = (len(gated_stable) == 12 and len(naive_stable) == 12
          and all(r['verdict'] == 'correct' for r in gated_stable + naive_stable))
    drift_gated_rows = pick('drift', 'gated')
    k4 = (bool(drift_gated_rows)
          and all(r['memory_version'] == 1 and r['memory_status'] == 'warm' for r in drift_gated_rows))

    if k1 and k2 and k3 and k4:
        verdict = 'v4_version_metadata_load_bearing'
    elif not k3:
        verdict = 'v4_control_failed'
    elif k1 and k4 and not k2:
        verdict = 'v4_ablation_inconclusive'
    else:
        verdict = 'v4_not_confirmed'

    # 漂移是真的漂移：每个 drift 实体 R2..R4 的 v1 答案都不等于当轮真值。
    drift_late = pick('drift', rmin=2)
    drift_effective = bool(drift_late) and all(r['stale_differs_from_truth'] for r in drift_late)

    # 可审计：每次作答都有召回工具调用；用经验作答的 gated 题必须指名召回里真实存在的记忆。
    recall_calls = set()
    trace_path = out / 'mcp-trace.jsonl'
    if trace_path.is_file():
        for line in trace_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get('tool') == 'butian_memory_recall':
                recall_calls.add(str(entry.get('arguments', {}).get('query', '')))
    audit = {}
    for round_no in range(1, ROUNDS + 1):
        path = out / 'stages' / f'r{round_no}-recall-audit.json'
        if path.is_file():
            for item in load(path)['items']:
                audit[(round_no, item['entity'])] = item
    violations = []
    for row in rows:
        key = (row['round'], row['entity'])
        if row['entity'] not in recall_calls:
            violations.append(f'no recall tool call for {row["entity"]}')
        if key not in audit:
            violations.append(f'no recall audit record for {key}')
            continue
        if row['arm'] == 'gated' and row['basis'] == 'experience':
            claimed = (row['answer'] or {}).get('from_memory')
            ids = {m['memory_id'] for m in audit[key]['raw']}
            if not claimed:
                violations.append(f'experience answer without from_memory for {key}')
            elif claimed not in ids:
                violations.append(f'claimed memory {claimed} absent from recall payload for {key}')

    summary = {
        'execution_kind': 'real_host_multiround_loop_v4',
        'protocol': PROTOCOL,
        'verdict': verdict,
        'host_kind': 'stub' if STUB else 'real_model_current_conversation',
        'llm_inference_calls': None if STUB else 'unmetered: host is the conversational model, calls not instrumented',
        'model_weights_changed': False,
        'rounds': ROUNDS,
        'entities': len({r['entity'] for r in rows}),
        'total_answers': len(rows),
        'design': {'groups': list(GROUPS), 'arms': list(ARMS), 'families': FAMILIES,
                   'ablation': 'recall presentation only; same store, same memories, same host rule',
                   'writeback': False},
        'by_round': by_round,
        'by_cell': by_cell,
        'accuracy': {
            'gated': sum(r['verdict'] == 'correct' for r in gated),
            'naive': sum(r['verdict'] == 'correct' for r in pick(arm='naive')),
            'gated_drift': sum(r['verdict'] == 'correct' for r in pick('drift', 'gated')),
            'naive_drift': sum(r['verdict'] == 'correct' for r in naive_drift),
            'gated_stable': sum(r['verdict'] == 'correct' for r in gated_stable),
            'naive_stable': sum(r['verdict'] == 'correct' for r in naive_stable),
        },
        'criteria': {
            'K1_gated_arm_load_bearing': k1,
            'K2_naive_drift_fails_by_reusing_v1': k2,
            'K3_stable_control_both_arms_correct': k3,
            'K4_memory_not_laundered_by_drift': k4,
        },
        'k1_detail': {'gated_n': len(gated), 'gated_correct': sum(r['verdict'] == 'correct' for r in gated),
                      'drift_r2_r4_reads': sum(1 for r in gated_drift_late if r['need_source']),
                      'stable_reads': sum(1 for r in gated_stable if r['need_source'])},
        'k2_detail': {'naive_drift_late_n': len(naive_drift_late),
                      'wrong': sum(r['verdict'] == 'wrong' for r in naive_drift_late),
                      'stale_match': sum(r['stale_match'] for r in naive_drift_late),
                      'reads_all_rounds': sum(1 for r in naive_drift if r['need_source']),
                      'drift_effective': drift_effective},
        'k3_detail': {'gated_stable': _cell(gated_stable), 'naive_stable': _cell(naive_stable)},
        'k4_detail': {r['entity']: sorted({(x['memory_version'], x['memory_status'])
                                          for x in drift_gated_rows if x['entity'] == r['entity']})
                      for r in drift_gated_rows},
        'audit_violations': violations,
        'recall_limit': RECALL_LIMIT,
        'limitations': [
            '宿主同时是实验设计者，非盲；消费规则预注册并冻结，但不消除非盲性',
            'naive 臂的「无元数据即信任内容」是建模假设，结论只在该类消费者下成立',
            '四则运算由本地脚本执行，不是模型推理能力的证据',
            '漂移日程是协议内设定（每轮必变），不代表真实世界的版本变化分布',
            '单种子、单次运行，没有跨模型、跨运行的重复，不报告 p 值',
            '记忆全程不写回，本档不回答「漂移后能不能追上新版本」',
            '12 个实体是 12 条独立规则，48 题不能当作 48 个独立样本',
            'naive 呈现层由 harness 构造，不是另装一个开源记忆模块',
        ],
        'rows': rows,
    }
    dump(out / 'summary.json', summary)
    print(json.dumps({'verdict': verdict, 'criteria': summary['criteria'],
                      'accuracy': summary['accuracy'], 'by_cell': by_cell,
                      'k2_detail': summary['k2_detail'], 'audit_violations': violations},
                     ensure_ascii=False, indent=2))

# ---------------------------------------------------------------- stub

def stub_decisions(out: Path, round_no: int):
    """管道自检：一律读源。不得当作宿主结果引用。"""
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