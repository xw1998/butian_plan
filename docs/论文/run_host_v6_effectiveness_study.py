"""Host-loop v6: 经验库 vs 纯读源，成本轴与预算轴双轴对照。

协议见 `实验协议-真实宿主多轮-v6.md`（跑之前已冻结）。前五档只证明「某个字段承重」，
本档第一次给出「用这套工具 vs 不用它」的对照：

  arm lib     有经验库（butian_memory_recall + butian_knowledge_source_read）
  arm noref   没有经验库，每题读源全文自行定位（真正的「不用」基线）

轴 A（成本，三题轮全跑）：都不限预算，比正确率 + 读源次数 + prompt token。
轴 B（预算，只跑 R3）：两臂同样的读源额度 L∈{6,12}，超额必须答 unknown，比正确率。

语料是 120 实体 × 1..4 版本的手册（24 个任务实体 + 96 个诱饵），经验库预置全部 120 条
v1 规则为 warm，两臂都不知道哪些实体会被提问。

密钥只从环境变量 DEEPSEEK_API_KEY 读取，不写入任何产物。

  --stage bootstrap   手工册、源快照、预置 120 条 warm 记忆
  --stage run         两个模型 × 两臂 × 三轴全跑（已结算自动跳过）
  --stage report      聚合 Z1..Z5，写 summary.json
"""
from pathlib import Path
import argparse
import json
import os
import random
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_host_v4_multiround_study as v4
import run_host_v5_multiround_study as v5
from run_host_multiround_study import (McpSession, dump, load, service_call, sha256_text,
                                       _score, _answer_from_rule, question_text)
from run_multiround_study import make_rule, solve

PROTOCOL = 'host-loop-v6'
SEED = 20261103
ROUNDS = 3
FAMILIES = ['calibration', 'schema', 'tool']
ENTITIES_PER_FAMILY = 40
TASKS_PER_FAMILY = 8
MODELS = ['deepseek-v4-pro', 'deepseek-flash']
ARMS = ['lib', 'noref']
MEMBER = 'host'
TOKEN = 'host-loop-token'
RECALL_LIMIT = 20
BUDGET_ROUND = 3
BUDGET_LEVELS = [6, 12]
SOURCE_LIMIT = 50000  # butian_knowledge_source_read 的上限

# ---------------------------------------------------------------- 语料

def entity_plan():
    plan = []
    for family in FAMILIES:
        for index in range(1, ENTITIES_PER_FAMILY + 1):
            plan.append({'entity': f'{family}{index:03d}', 'family': family,
                         'task': index <= TASKS_PER_FAMILY})
    return plan

def task_index():
    """任务实体的全局下标（决定漂移日程），按族分组、族内按编号。"""
    mapping = {}
    for item in entity_plan():
        if item['task']:
            mapping[item['entity']] = len(mapping)
    return mapping

def truth_version(k, round_no):
    """冻结的漂移日程：R1 全 v1；R2 有 8 个到 v2；R3 再加 4 个到 v2。"""
    if round_no == 1:
        return 1
    if k % 3 == 0:
        return 2
    if round_no >= 3 and k in (1, 4, 7, 10):
        return 2
    return 1

def _params(rule):
    return {k: val for k, val in rule.items() if k not in ('entity', 'version')}

def build_corpus():
    rng = random.Random(SEED)
    plan = entity_plan()
    index = task_index()
    rules = {}
    for item in plan:
        entity, family, is_task = item['entity'], item['family'], item['task']
        count = 4 if is_task else rng.choice([1, 2, 3, 4])
        base = None
        for version in range(1, count + 1):
            rule = make_rule(family, entity, version, rng)
            if version == 1:
                base = rule
            else:
                # 漂移必须真的改变答案，否则「沿用旧版本」与「正确」同值，检测空转。
                guard = 0
                while _params(rule) == _params(base) and guard < 200:
                    rule = make_rule(family, entity, version, rng)
                    guard += 1
            rules[(entity, version)] = rule
    tasks = []
    for item in plan:
        if not item['task']:
            continue
        entity, family, k = item['entity'], item['family'], index[item['entity']]
        for round_no in range(1, ROUNDS + 1):
            version = truth_version(k, round_no)
            rule = rules[(entity, version)]
            task = {'round': round_no, 'entity': entity, 'family': family,
                    'k': k, 'version': version,
                    'stale': version != 1}
            if family == 'calibration':
                task['raw'] = rng.randrange(100, 10000)
            elif family == 'schema':
                task['payload'] = {f'{c}{i}': rng.randrange(1, 10000)
                                   for c in ('f', 'h') for i in range(1, 5)}
            else:
                task['raw'] = rng.randrange(1, 100)
            task['truth'] = solve(rule, task)
            tasks.append(task)
    return plan, tasks, rules

def build_archive(rules) -> str:
    lines = ['# 契约手册（归档快照，单位 kPa，内部使用，共 120 个实体）',
             '# 每条 RULE 行是一个实体在一个文档版本下的参数；版本号必须与本轮任务一致。',
             '# 换算约定（对全部实体与版本一致，公式本身不随版本变化）：',
             '#   calibration: 工程值 = (raw - offset) / scale',
             '#   schema:      flow = payload[flow_field] / divisor, level = payload[level_field] / divisor',
             '#   tool:        端点 = endpoint, 参数名 = parameter, 参数值 = raw * factor']
    for key in sorted(rules):
        lines.append('RULE ' + json.dumps(rules[key], sort_keys=True, ensure_ascii=False))
    return '\n'.join(lines) + '\n'

# ---------------------------------------------------------------- 提示词

FORMULAS = """三族任务的换算约定（公式本身不随版本变化，参数随实体与版本变化）：
- calibration: 工程值 = (raw - offset) / scale
- schema:      flow = payload[flow_field] / divisor, level = payload[level_field] / divisor
- tool:        端点 = endpoint, 参数名 = parameter, 参数值 = raw * factor"""

FORMAT = """只输出一个 JSON 对象，不要 markdown 代码块，不要额外文字。字段：
{
  "decision": "experience" | "source" | "unknown",
  "rule": {...} | null,          // 你采用的那条 RULE 对象，原样抄出，不要改写字段
  "answer": {...} | null,        // 你自己算出的答案
  "reason": "一句话说明依据"
}

answer 的格式：calibration 族 {"value": 数}；schema 族 {"flow": 数, "level": 数}；
tool 族 {"endpoint": 字符串, "parameter": 字符串, "value": 数}。
判分是精确匹配。没有可用依据时必须 decision="unknown"，不得猜。"""

LIB_SYSTEM = """你是一个工程助手，通过经验库和源文档答题。你会收到一道题，以及一次经验召回的结果。

""" + FORMULAS + """

你必须严格按以下消费规则决定依据，不得自行放宽或收紧：
1. 召回给出的经验，若状态为 warm 或 hot，且其版本与本轮任务的文档版本一致 -> 据此作答；
2. 若召回项不含任何版本与状态信息 -> 按内容直接作答；
3. 若版本不一致，或状态为 candidate 或 disputed -> 声明需要读源（decision="source"），不要先作答；
4. 三者都不成立 -> decision="unknown"。

""" + FORMAT

NOREF_SYSTEM = """你是一个工程助手，只能依据源文档答题。你没有经验库，没有任何历史记录。

""" + FORMULAS + """

本题会附上源文档全文（若本轮读源额度已用尽则不附）。你必须从源文档中找出该实体在
**本轮文档版本**下的那条 RULE，原样抄进 rule 字段，并给出你自己算出的 answer。
若没有源文档可用，必须 decision="unknown"，不得凭记忆或猜测作答。

""" + FORMAT

# ---------------------------------------------------------------- 阶段

def stage_bootstrap(out: Path):
    home = out / 'production'
    plan, tasks, rules = build_corpus()
    archive = build_archive(rules)
    (out / 'fixtures').mkdir(parents=True, exist_ok=True)
    archive_path = out / 'fixtures' / 'contract-manual-120.txt'
    archive_path.write_text(archive, encoding='utf-8')
    digest = sha256_text(archive)

    def setup(svc):
        row = svc.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (MEMBER,)).fetchone()
        if not row:
            svc.add_member(MEMBER, 'Real host driving Butian over MCP (v6 effectiveness)')
        svc.store.set_member_token(MEMBER, TOKEN)
        return svc.learning.add_source('manual', str(archive_path), '契约手册（120 实体，v1-v4）',
                                       digest, owner_member_id=MEMBER)

    source_id = service_call(home, setup)
    verified = service_call(home, lambda svc: svc.learning.verify_source_content(
        source_id, archive, owner_member_id=MEMBER))

    evidence_dir = out / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest = []

    def seed(svc):
        memory = svc.memory(MEMBER)
        statuses = {}
        try:
            for item in plan:
                entity = item['entity']
                memory_id = f'{entity}-v1'
                rule = rules[(entity, 1)]
                content = 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)
                for tag in ('seed-a', 'seed-b'):
                    path = evidence_dir / f'{entity}-{tag}.txt'
                    body = (f'entity={entity}; tag={tag}; source=upstream_extraction\n'
                            f'content={content}\n')
                    path.write_text(body, encoding='utf-8')
                    evidence = svc.record_evidence(path, owner_member_id=MEMBER,
                                                   actor_member_id=MEMBER)
                    state = svc.record_practice_outcome(
                        MEMBER, memory_id=memory_id, kind='lesson', content=content,
                        outcome='success', independent_key=f'{tag}-{entity}',
                        evidence_ref=evidence)
                    statuses[entity] = state['status']
                    manifest.append({'entity': entity, 'tag': tag, 'memory_id': memory_id,
                                     'sha256': sha256_text(body), 'status': state['status']})
        finally:
            memory.close()
        return statuses

    seeded = service_call(home, seed)
    dump(out / 'fixtures' / 'tasks.json', tasks)
    dump(out / 'fixtures' / 'plan.json', plan)
    dump(out / 'fixtures' / 'rules.json',
         {f'{k[0]}@{k[1]}': v for k, v in rules.items()})
    dump(out / 'fixtures' / 'seed-manifest.json', manifest)
    dump(out / 'state.json', {'protocol': PROTOCOL, 'source_id': source_id,
                              'source_sha256': digest, 'archive': str(archive_path),
                              'archive_bytes': len(archive.encode('utf-8')),
                              'archive_chars': len(archive),
                              'archive_lines': archive.count('\n'),
                              'created_at': time.time(), 'round': 0,
                              'seeded_status': seeded})
    print(f'[bootstrap] source_id={source_id} verified={verified} '
          f'entities={len(plan)} tasks={len(tasks)} archive={len(archive.encode())}B '
          f'lines={archive.count(chr(10))} statuses={sorted(set(seeded.values()))}')

# ---------------------------------------------------------------- 作答

def _truth_rows(tasks, round_no):
    return [t for t in tasks if t['round'] == round_no]

def _ask_and_score(messages, model, key, tag, task, calls, reads, budget_left):
    obj, record = v5.ask(messages, model, key, calls, tag)
    rule = (obj or {}).get('rule')
    answer = (obj or {}).get('answer')
    if isinstance(rule, dict):
        try:
            script_ans = _answer_from_rule(rule, task)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            script_ans = {'unknown': True}
    else:
        script_ans = {'unknown': True}
    return {
        'decision': (obj or {}).get('decision'),
        'rule': rule, 'answer': answer,
        'script_verdict': _score(task, script_ans),
        'model_verdict': _score(task, answer),
        'reads': reads, 'budget_left': budget_left,
    }

def _read_source(session, state):
    """读源并核对完整性。截断的手册会让基线看起来更差，必须硬失败。"""
    payload = session.call('butian_knowledge_source_read', source_id=state['source_id'],
                           member_id=MEMBER, limit=SOURCE_LIMIT)
    text = payload.get('content', '')
    expected = state.get('archive_chars')
    if expected is not None and len(text) != expected:
        raise SystemExit(f'源文档被截断：返回 {len(text)} 字符，期望 {expected}')
    return text

def run_arm(home: Path, model: str, key: str, arm: str, round_no: int,
            budget=None, tag_prefix=''):
    state = load(home / 'state.json')
    tasks = load(home / 'fixtures' / 'tasks.json')
    rows = []
    calls = []
    reads = 0
    budget_left = budget
    session = McpSession(home / 'production', TOKEN, home / 'mcp-trace.jsonl')
    calls_dir = home / 'calls' / (tag_prefix or arm)
    calls_dir.mkdir(parents=True, exist_ok=True)
    try:
        for task in _truth_rows(tasks, round_no):
            entity = task['entity']
            question = question_text(task)
            tag = f'{tag_prefix or arm}-r{round_no}-{entity}'
            reads_before = reads

            if arm == 'lib':
                payload = session.call('butian_memory_recall', query=entity, limit=RECALL_LIMIT)
                memories = payload.get('memories') or payload.get('items') or []
                shown = [{'memory_id': m.get('memory_id'), 'status': m.get('status'),
                          'content': (m.get('content') or '')[:400],
                          'needs_revalidation': m.get('needs_revalidation')}
                         for m in memories]
                quota = '本轮不限读源次数。' if budget is None else f'本轮读源额度剩余 {budget_left} 次。'
                user = (question + '\n\n' + quota
                        + f'\n\n本次经验召回返回（共 {len(shown)} 条，列表顺序即下标顺序）：\n'
                        + json.dumps(shown, ensure_ascii=False, indent=2))
                messages = [{'role': 'system', 'content': LIB_SYSTEM},
                            {'role': 'user', 'content': user}]
                obj, record = v5.ask(messages, model, key, calls, tag)
                decision = (obj or {}).get('decision')
                if decision == 'source':
                    allowed = budget is None or (budget_left or 0) > 0
                    if allowed:
                        src_text = _read_source(session, state)
                        reads += 1
                        if budget is not None:
                            budget_left -= 1
                        follow = messages + [
                            {'role': 'assistant', 'content': record.get('retry_reply') or record['reply']},
                            {'role': 'user', 'content': v5.SOURCE_TURN + src_text}]
                        obj2, record2 = v5.ask(follow, model, key, calls, tag + '-source')
                    else:
                        follow = messages + [
                            {'role': 'assistant', 'content': record.get('retry_reply') or record['reply']},
                            {'role': 'user', 'content': '本轮读源额度已用尽，不能读源。'
                                                        '没有可用依据时必须 decision="unknown"，不得猜。'}]
                        obj2, record2 = v5.ask(follow, model, key, calls, tag + '-denied')
                    if obj2 is not None:
                        obj = obj2
            else:
                allowed = budget is None or (budget_left or 0) > 0
                if allowed:
                    src_text = _read_source(session, state)
                    reads += 1
                    if budget is not None:
                        budget_left -= 1
                    quota = ('本轮不限读源次数。' if budget is None
                             else f'本轮读源额度剩余 {budget_left} 次。')
                    user = question + '\n\n' + quota + '\n\n以下是源文档全文快照：\n\n' + src_text
                else:
                    user = (question + '\n\n本轮读源额度已用尽，无法读取源文档。'
                            '没有可用依据时必须 decision="unknown"，不得猜。')
                messages = [{'role': 'system', 'content': NOREF_SYSTEM},
                            {'role': 'user', 'content': user}]
                obj, record = v5.ask(messages, model, key, calls, tag)

            rule = (obj or {}).get('rule')
            answer = (obj or {}).get('answer')
            if isinstance(rule, dict):
                try:
                    script_ans = _answer_from_rule(rule, task)
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    script_ans = {'unknown': True}
            else:
                script_ans = {'unknown': True}
            rows.append({'round': round_no, 'entity': entity, 'family': task['family'],
                         'k': task['k'], 'arm': arm, 'version': task['version'],
                         'stale': task['stale'],
                         'decision': (obj or {}).get('decision'),
                         'basis': (obj or {}).get('decision'),
                         'reads_this': reads - reads_before,
                         'budget_left': budget_left,
                         'script_verdict': _score(task, script_ans),
                         'model_verdict': _score(task, answer),
                         'answer': answer, 'truth': task['truth']})
    finally:
        session.close()

    for record in calls:
        name = record['tag'].replace('/', '_') + '.json'
        (calls_dir / name).write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}
    for record in calls:
        for key_name in ('usage', 'retry_usage'):
            item = record.get(key_name)
            if item:
                usage['prompt_tokens'] += item.get('prompt_tokens', 0)
                usage['completion_tokens'] += item.get('completion_tokens', 0)
                usage['calls'] += 1
    return {'rows': rows, 'reads': reads, 'usage': usage,
            'served_models': sorted({r.get('served_model') for r in calls if r.get('served_model')}),
            'parse_errors': sum(1 for r in calls if str(r.get('parse', '')).startswith('failed'))}

def run_model(out: Path, model: str, key: str):
    home = out / 'models' / model
    home.mkdir(parents=True, exist_ok=True)
    if not (home / 'state.json').is_file():
        stage_bootstrap(home)
    stages = home / 'stages'
    stages.mkdir(parents=True, exist_ok=True)

    # 轴 A：成本轴，三轮不限预算
    for round_no in range(1, ROUNDS + 1):
        for arm in ARMS:
            path = stages / f'A-r{round_no}-{arm}.json'
            if path.is_file():
                print(f'[{model} A r{round_no} {arm}] settled, skip')
                continue
            result = run_arm(home, model, key, arm, round_no)
            dump(path, result)
            print(f'[{model} A r{round_no} {arm}] rows={len(result["rows"])} '
                  f'reads={result["reads"]} prompt={result["usage"]["prompt_tokens"]}')

    # 轴 B：预算轴，只跑 R3，两档额度
    for level in BUDGET_LEVELS:
        for arm in ARMS:
            path = stages / f'B-L{level}-{arm}.json'
            if path.is_file():
                print(f'[{model} B L={level} {arm}] settled, skip')
                continue
            result = run_arm(home, model, key, arm, BUDGET_ROUND, budget=level,
                             tag_prefix=f'B-L{level}-{arm}')
            dump(path, result)
            print(f'[{model} B L={level} {arm}] reads={result["reads"]} '
                  f'prompt={result["usage"]["prompt_tokens"]}')

# ---------------------------------------------------------------- 汇总

def _acc(rows):
    n = len(rows)
    correct = sum(1 for r in rows if r['script_verdict'] == 'correct')
    unknown = sum(1 for r in rows if r['script_verdict'] == 'unknown')
    return {'n': n, 'correct': correct, 'unknown': unknown,
            'acc': round(correct / n, 4) if n else 0.0}

def model_summary(home: Path) -> dict:
    stages = home / 'stages'
    axis_a = {}
    for arm in ARMS:
        rows = []
        reads = 0
        usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}
        for round_no in range(1, ROUNDS + 1):
            data = load(stages / f'A-r{round_no}-{arm}.json')
            rows.extend(data['rows'])
            reads += data['reads']
            for key_name in usage:
                usage[key_name] += data['usage'][key_name]
        axis_a[arm] = {**_acc(rows), 'reads': reads, 'usage': usage,
                       'by_round': {f'R{r}': _acc([x for x in rows if x['round'] == r])
                                    for r in range(1, ROUNDS + 1)}}
    axis_b = {}
    for level in BUDGET_LEVELS:
        axis_b[f'L{level}'] = {}
        for arm in ARMS:
            data = load(stages / f'B-L{level}-{arm}.json')
            axis_b[f'L{level}'][arm] = {**_acc(data['rows']), 'reads': data['reads'],
                                        'budget_used': data['reads']}

    lib, noref = axis_a['lib'], axis_a['noref']
    z1 = lib['acc'] >= noref['acc'] - 0.05
    z2 = noref['reads'] > 0 and lib['reads'] <= 0.5 * noref['reads']
    z3 = noref['acc'] >= 0.95
    z4_gaps = {level: round(axis_b[level]['lib']['acc'] - axis_b[level]['noref']['acc'], 4)
               for level in axis_b}
    z4 = any(gap >= 0.15 for gap in z4_gaps.values())

    if not z3:
        verdict = 'v6_baseline_handicapped'
    elif not z1:
        verdict = 'v6_accuracy_regression'
    elif z2 and z4:
        verdict = 'v6_library_effective_both_axes'
    elif z2:
        verdict = 'v6_library_effective_cost_only'
    else:
        verdict = 'v6_no_advantage'

    return {'axis_a': axis_a, 'axis_b': axis_b,
            'criteria': {'Z1_accuracy_not_worse': z1, 'Z2_reads_at_most_half': z2,
                         'Z3_baseline_above_95': z3, 'Z4_budget_gap_ge_15pp': z4},
            'budget_gaps_pp': {k: round(v * 100, 1) for k, v in z4_gaps.items()},
            'reads_saving_pct': round((1 - lib['reads'] / noref['reads']) * 100, 1)
            if noref['reads'] else None,
            'verdict': verdict}

def stage_report(out: Path):
    models = {}
    for model in MODELS:
        home = out / 'models' / model
        if not (home / 'stages' / f'A-r{ROUNDS}-noref.json').is_file():
            continue
        models[model] = model_summary(home)
    summary = {'protocol': PROTOCOL, 'execution_kind': 'external_model_effectiveness_v6',
               'task_seed': SEED, 'models': models}
    if len(models) == len(MODELS):
        verdicts = {m: models[m]['verdict'] for m in models}
        if len(set(verdicts.values())) == 1:
            summary['verdict'] = 'v6_cross_model_replicated'
        else:
            summary['verdict'] = 'v6_split'
        summary['per_model_verdict'] = verdicts
    else:
        summary['verdict'] = 'incomplete'
    dump(out / 'summary.json', summary)
    print(f'[report] verdict={summary["verdict"]}')
    for model, data in models.items():
        print(f'  {model}: {data["verdict"]}  Z={data["criteria"]}  '
              f'gap={data["budget_gaps_pp"]}  saving={data["reads_saving_pct"]}%')

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--stage', required=True,
                        choices=['bootstrap', 'run', 'report'])
    parser.add_argument('--model', default=None)
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.stage == 'bootstrap':
        stage_bootstrap(out)
        return 0
    if args.stage == 'report':
        stage_report(out)
        return 0
    key = os.environ.get('DEEPSEEK_API_KEY')
    if not key:
        raise SystemExit('DEEPSEEK_API_KEY 未设置：本档需要外部模型，密钥只从环境变量读取')
    targets = [args.model] if args.model else MODELS
    for model in targets:
        run_model(out, model, key)
    stage_report(out)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
