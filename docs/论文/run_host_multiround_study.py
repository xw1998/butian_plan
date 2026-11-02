"""Host-loop benchmark: a real model drives Butian over MCP across three rounds.

Unlike the deterministic benchmarks in this directory, nothing here solves the
task for the host. The host is a real language model that talks to the running
production service over the MCP stdio channel (`python -m butian.cli mcp`),
may call exactly two read tools, and then answers. The harness only:

  * builds the fixture stream and the hash-verified source document,
  * executes the host's tool calls and records them (server-side audit),
  * scores each answer against `solve()` and writes the practice observation,
  * aggregates the frozen criteria in 实验协议-真实宿主多轮.md.

Stages (each invocation opens one MCP session, so state lives on disk):

  --stage bootstrap          members, source snapshot, task stream
  --stage recall --round N   runs butian_memory_recall for every item
  -> host writes stages/rN-recall-decision.json
  --stage source --round N   runs butian_knowledge_source_read where requested
  -> host writes stages/rN-answers.json
  --stage settle --round N   scores, writes evidence + practice observations
  --stage report             aggregates P1..P5 and writes summary.json

`--stub` auto-fills decision files by parsing the source document. It exists to
prove the pipe; results produced with it are labelled `host_kind = stub` and
must never be reported as a real-host result.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from butian.runtime.service import ButianService
from run_multiround_study import FAMILIES, make_rule, solve

PROTOCOL = 'host-loop-v1'
SEED = 20261102
ROUNDS = 3
PER_FAMILY = 4
STABLE_PER_FAMILY = 2
MEMBER = 'host'
STUB = False
# recall 只能做子串模糊匹配（查询按二字组命中再按状态/权重/时间排序），没有按实体或标签的结构化过滤；
# limit=10 时 12 条记忆只能返回 10 条、且可能不含提问实体本身，宿主无法据此判断该实体有无经验。
# 因此宿主统一用 limit=50 取回全部记忆后自行按内容里的 entity/version 过滤，这一参数不改变任何判据。
RECALL_LIMIT = 50


# ---------------------------------------------------------------- 基础设施

def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def load(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class McpSession:
    """One `python -m butian.cli mcp` process, JSON-RPC over stdio."""

    def __init__(self, home: Path, token: str, trace: Path):
        env = dict(os.environ, BUTIAN_HOME=str(home))
        env.pop('BUTIAN_PROVIDERS_FILE', None)
        self.proc = subprocess.Popen(
            [sys.executable, '-m', 'butian.cli', 'mcp'], cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8')
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(line) for line in self.proc.stdout], daemon=True).start()
        self.auth = {'member_id': MEMBER, 'actor_member_id': MEMBER, 'token': token}
        self.trace = trace
        self.seq = 0
        self.request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                    'clientInfo': {'name': 'host-loop-host', 'version': PROTOCOL}})
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        self.proc.stdin.flush()

    def request(self, method, params):
        self.seq += 1
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.seq, 'method': method, 'params': params}) + '\n')
        self.proc.stdin.flush()
        raw = self.lines.get(timeout=60)
        reply = json.loads(raw)
        if 'error' in reply:
            raise RuntimeError(f'{method} failed: {reply["error"]}')
        return reply['result']

    def call(self, name: str, **arguments):
        started = time.time()
        result = self.request('tools/call', {'name': name, 'arguments': dict(self.auth, **arguments)})
        text = result['content'][0]['text'] if result.get('content') else ''
        payload = json.loads(text) if text.strip().startswith(('{', '[')) else {'text': text}
        entry = {'tool': name, 'arguments': {k: v for k, v in arguments.items() if k != 'token'},
                 'is_error': bool(result.get('isError')), 'ms': round((time.time() - started) * 1000, 1),
                 'response_keys': sorted(payload)[:12] if isinstance(payload, dict) else []}
        with self.trace.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        if result.get('isError'):
            raise RuntimeError(f'{name} returned isError: {text[:300]}')
        return payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def service_call(home: Path, fn):
    svc = ButianService(home)
    try:
        return fn(svc)
    finally:
        svc.close()


# ---------------------------------------------------------------- 任务流

def entity_plan():
    plan = []
    for family in FAMILIES:
        for index in range(PER_FAMILY):
            plan.append({'entity': f'{family}{index:03}', 'family': family,
                         'stable': index < STABLE_PER_FAMILY})
    return plan


def build_tasks():
    """Rounds, rules per (entity, round) and the payload of every question.

    A rule belongs to a (entity, version) pair, not to a round: stable entities keep
    version 1 across all three rounds and therefore keep *one* rule, drifting entities
    get a fresh rule per version. Otherwise the archive would carry several RULE lines
    with the same version and the host could not tell which one applies — the question
    would be unanswerable rather than answerable.
    """
    rng = random.Random(SEED)
    tasks, rules = [], {}
    for item in entity_plan():
        entity, family = item['entity'], item['family']
        versions = [1, 1, 1] if item['stable'] else [1, 2, 3]
        by_version = {}
        for round_no, version in enumerate(versions, start=1):
            if version not in by_version:
                rule = make_rule(family, entity, version, rng)
                while not item['stable'] and by_version and any(
                        {k: v for k, v in rule.items() if k != 'version'}
                        == {k: v for k, v in prev.items() if k != 'version'}
                        for prev in by_version.values()):
                    rule = make_rule(family, entity, version, rng)
                by_version[version] = rule
            rule = by_version[version]
            rules[f'{entity}@{round_no}'] = rule
            task = {'round': round_no, 'entity': entity, 'family': family,
                    'version': version, 'stable': item['stable']}
            if family == 'calibration':
                task['raw'] = rng.randrange(100, 10000)
            elif family == 'schema':
                task['payload'] = {f'{k}{v}': rng.randrange(1, 10000) for k in ('f', 'h') for v in (1, 2, 3)}
            else:
                task['raw'] = rng.randrange(1, 100)
            task['truth'] = solve(rule, task)
            tasks.append(task)
    return tasks, rules


def build_archive(rules) -> str:
    """One versioned manual: every (entity, version) rule plus distractors."""
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


def question_text(task) -> str:
    family = task['family']
    if family == 'calibration':
        body = f'原始读数 raw = {task["raw"]}，请按 calibration 族的换算规则给出工程值（kPa）。'
    elif family == 'schema':
        payload = json.dumps(task['payload'], ensure_ascii=False, sort_keys=True)
        body = f'本版本文档字段载荷 payload = {payload}，请给出 flow 与 level 两个工程量（除以本版本的分母）。'
    else:
        body = f'调用参数 raw = {task["raw"]}，请给出本版本的端点路径与参数名，以及该参数的值。'
    return (f'实体 {task["entity"]}（{family} 族），当前文档版本 v{task["version"]}。\n'
            f'{body}\n'
            f'判分是精确匹配；若没有可用依据，必须回答 unknown，不得猜。')


# ---------------------------------------------------------------- 阶段

def stage_bootstrap(out: Path):
    home = out / 'production'
    tasks, rules = build_tasks()
    archive = build_archive(rules)
    (out / 'fixtures').mkdir(parents=True, exist_ok=True)
    archive_path = out / 'fixtures' / 'contract-manual-v1-v3.txt'
    archive_path.write_text(archive, encoding='utf-8')
    digest = sha256_text(archive)

    def setup(svc):
        row = svc.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (MEMBER,)).fetchone()
        if not row:
            svc.add_member(MEMBER, 'Real host driving Butian over MCP')
        svc.store.set_member_token(MEMBER, 'host-loop-token')
        return svc.learning.add_source('manual', str(archive_path), '契约手册（版本化归档）', digest,
                                       owner_member_id=MEMBER)

    source_id = service_call(home, setup)
    verified = service_call(home, lambda svc: svc.learning.verify_source_content(source_id, archive, owner_member_id=MEMBER))
    dump(out / 'fixtures' / 'tasks.json', tasks)
    dump(out / 'fixtures' / 'rules.json', rules)
    dump(out / 'state.json', {'protocol': PROTOCOL, 'source_id': source_id,
                              'source_sha256': digest, 'archive': str(archive_path),
                              'created_at': time.time(), 'round': 0})
    print(f'[bootstrap] source_id={source_id} verified={verified} tasks={len(tasks)}')


def stage_recall(out: Path, round_no: int):
    state = load(out / 'state.json')
    tasks = [t for t in load(out / 'fixtures' / 'tasks.json') if t['round'] == round_no]
    items = []
    trace = out / 'mcp-trace.jsonl'
    session = McpSession(out / 'production', 'host-loop-token', trace)
    try:
        for task in tasks:
            payload = session.call('butian_memory_recall', query=task['entity'], limit=RECALL_LIMIT,
                                   decision_id=f"r{round_no}-{task['entity']}")
            memories = [{'memory_id': m['memory_id'], 'status': m['status'], 'content': m['content'][:400],
                         'needs_revalidation': m.get('needs_revalidation'),
                         'environment_match': m.get('environment_match')}
                        for m in payload.get('memories', [])]
            items.append({'entity': task['entity'], 'family': task['family'], 'version': task['version'],
                          'stable': task['stable'], 'question': question_text(task),
                          'recall_id': payload.get('recall_id'), 'no_match': payload.get('no_match'),
                          'memories': memories})
    finally:
        session.close()
    dump(out / 'stages' / f'r{round_no}-recall-request.json',
         {'round': round_no, 'items': items,
          'instruction': '对每个实体决定：是否已有与当前版本一致的可用经验（use_experience），'
                         '是否需要读源（need_source）。need_source=false 且 use_experience=false 视为无依据作答，'
                         '会被判 unknown。'})
    print(f'[recall r{round_no}] {len(items)} items -> stages/r{round_no}-recall-request.json')


def stage_source(out: Path, round_no: int):
    state = load(out / 'state.json')
    decisions = load(out / 'stages' / f'r{round_no}-recall-decision.json')
    tasks = {t['entity']: t for t in load(out / 'fixtures' / 'tasks.json') if t['round'] == round_no}
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


def _score(task, answer) -> str:
    if not isinstance(answer, dict):
        return 'unknown'
    if answer.get('unknown'):
        return 'unknown'
    truth = task['truth']
    family = task['family']
    try:
        if family == 'calibration':
            value = answer.get('value')
            ok = isinstance(value, (int, float)) and abs(float(value) - float(truth)) <= 1e-6
            return 'correct' if ok else 'wrong'
        if family == 'schema':
            for key in ('flow', 'level'):
                if not isinstance(answer.get(key), (int, float)):
                    return 'wrong'
                if abs(float(answer[key]) - float(truth[key])) > 1e-9:
                    return 'wrong'
            return 'correct'
        if str(answer.get('endpoint', '')) != truth['endpoint']:
            return 'wrong'
        parameter = answer.get('parameter')
        if not isinstance(parameter, str) or parameter not in truth['kwargs']:
            return 'wrong'
        value = answer.get('value')
        if not isinstance(value, (int, float)) or abs(float(value) - float(truth['kwargs'][parameter])) > 1e-9:
            return 'wrong'
        return 'correct'
    except (TypeError, ValueError):
        return 'wrong'


def _stale_rule(rules, entity, round_no):
    """The rule the host would apply if it ignored the version."""
    for previous in range(round_no - 1, 0, -1):
        rule = rules.get(f'{entity}@{previous}')
        if rule is not None:
            return rule
    return None


def stage_settle(out: Path, round_no: int):
    home = out / 'production'
    tasks = [t for t in load(out / 'fixtures' / 'tasks.json') if t['round'] == round_no]
    decisions = load(out / 'stages' / f'r{round_no}-recall-decision.json')
    answers = load(out / 'stages' / f'r{round_no}-answers.json')
    rules = load(out / 'fixtures' / 'rules.json')
    results = []
    for task in tasks:
        entity = task['entity']
        answer = answers.get(entity, {'unknown': True, 'basis': 'missing'})
        verdict = _score(task, answer)
        basis = answer.get('basis') or ('experience' if not decisions.get(entity, {}).get('need_source') else 'source')
        stale_error = False
        if verdict == 'wrong' and basis == 'experience':
            stale = _stale_rule(rules, entity, round_no)
            if stale is not None and _score(task, _answer_from_rule(stale, task)) == 'correct':
                stale_error = True
        results.append({'round': round_no, 'entity': entity, 'family': task['family'],
                        'version': task['version'], 'stable': task['stable'],
                        'need_source': bool(decisions.get(entity, {}).get('need_source')),
                        'basis': basis, 'verdict': verdict, 'stale_version_error': stale_error,
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
    # 记录写回之后各记忆的真实状态，供 P4 判定
    statuses = service_call(home, lambda svc: _statuses(svc, results))
    for row in results:
        row['status_after'] = statuses.get(row['entity'])
    dump(out / 'stages' / f'r{round_no}-result.json', {'round': round_no, 'results': results})
    counts = {k: sum(1 for r in results if r['verdict'] == k) for k in ('correct', 'wrong', 'unknown')}
    reads = sum(1 for r in results if r['need_source'])
    print(f"[settle r{round_no}] {counts} reads={reads} stale_errors={sum(r['stale_version_error'] for r in results)}")


def _statuses(svc, results):
    memory = svc.memory(MEMBER)
    try:
        out = {}
        for row in results:
            memory_id = f"{row['entity']}-v{row['version']}"
            found = memory.get(memory_id)
            out[row['entity']] = None if found is None else found['status']
        return out
    finally:
        memory.close()


def _answer_from_rule(rule, task):
    truth = solve(rule, task)
    if task['family'] == 'calibration':
        return {'value': truth}
    if task['family'] == 'schema':
        return {'flow': truth['flow'], 'level': truth['level']}
    parameter = next(iter(truth['kwargs']))
    return {'endpoint': truth['endpoint'], 'parameter': parameter, 'value': truth['kwargs'][parameter]}


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
        for group in ('stable', 'drift'):
            subset = [r for r in block if r['stable'] == (group == 'stable')]
            by_round[f'r{round_no}-{group}'] = {
                'n': len(subset),
                'correct': sum(r['verdict'] == 'correct' for r in subset),
                'wrong': sum(r['verdict'] == 'wrong' for r in subset),
                'unknown': sum(r['verdict'] == 'unknown' for r in subset),
                'reads': sum(1 for r in subset if r['need_source']),
                'experience_basis': sum(1 for r in subset if r['basis'] == 'experience'),
                'stale_version_errors': sum(1 for r in subset if r['stale_version_error']),
            }
    stable_r1 = by_round['r1-stable']
    stable_r3 = by_round['r3-stable']
    drift = [by_round[f'r{round_no}-drift'] for round_no in (2, 3)]
    p1 = stable_r3['correct'] >= stable_r1['correct']
    p2 = (all(block['stale_version_errors'] == 0 for block in drift)
          and all(block['reads'] == block['n'] for block in drift))
    p3 = stable_r3['reads'] <= stable_r3['n'] * 0.5 and stable_r3['correct'] >= stable_r1['correct']
    p4 = (all(row.get('status_after') == 'warm' for row in rows if row['stable'] and row['round'] == 3)
          and all(row.get('status_after') != 'warm' for row in rows if not row['stable'] and row['round'] in (2, 3)))
    # P5 用比预注册更严的口径实现（只会把 pass 变 fail，不会放过假绿）：
    #   1) 每一轮每个实体都真的发过一次 butian_memory_recall；
    #   2) 依据经验作答的行，其 from_memory 必须出现在该轮该实体的召回结果里。
    recall_items = {}
    for round_no in range(1, ROUNDS + 1):
        path = out / 'stages' / f'r{round_no}-recall-request.json'
        if path.is_file():
            for item in load(path)['items']:
                recall_items[(round_no, item['entity'])] = {m['memory_id'] for m in item['memories']}
    recall_calls = set()
    trace_path = out / 'mcp-trace.jsonl'
    if trace_path.is_file():
        for line in trace_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get('tool') == 'butian_memory_recall':
                query = str(entry.get('arguments', {}).get('query', ''))
                recall_calls.add(query)
    p5_reasons = []
    for row in rows:
        key = (row['round'], row['entity'])
        if key not in recall_items:
            p5_reasons.append(f"no recall record for {key}")
        if row['entity'] not in recall_calls:
            p5_reasons.append(f"no recall tool call for {row['entity']}")
        if row['basis'] == 'experience':
            claimed = (row['answer'] or {}).get('from_memory')
            if not claimed:
                p5_reasons.append(f"experience answer without from_memory for {key}")
            elif claimed not in recall_items.get(key, set()):
                p5_reasons.append(f"claimed memory {claimed} absent from recall payload for {key}")
    p5 = not p5_reasons
    summary = {
        'execution_kind': 'real_host_multiround_loop',
        'protocol': PROTOCOL,
        'host_kind': 'stub' if STUB else 'real_model_current_conversation',
        'llm_inference_calls': None if STUB else 'unmetered: host is the conversational model, calls not instrumented',
        'model_weights_changed': False,
        'rounds': ROUNDS,
        'entities_per_family': PER_FAMILY,
        'stable_per_family': STABLE_PER_FAMILY,
        'total_answers': len(rows),
        'by_round': by_round,
        'criteria': {
            'P1_accuracy_not_worse': p1,
            'P2_version_gate_effective': p2,
            'P3_reuse_saves_reads': p3,
            'P4_experience_formed': p4,
            'P5_behaviour_auditable': p5,
        },
        'all_criteria_met': all((p1, p2, p3, p4, p5)),
        'p5_violations': p5_reasons,
        'recall_limit': RECALL_LIMIT,
        'channel_notes': [
            'butian_memory_recall 只做子串模糊匹配（查询按二字组命中，再按状态/权重/更新时间排序），'
            '没有按实体或标签的结构化过滤；limit=10 时 12 条记忆只回 10 条且可能不含提问实体本身。'
            '宿主因此用 limit=%d 取回全部记忆后自行按内容里的 entity/version 过滤。' % RECALL_LIMIT,
        ],
        'limitations': [
            '宿主同时是实验设计者，非盲；只能靠客观判分与事前冻结判据部分补偿',
            '单宿主、单次运行，不能跨模型外推，不报告 p 值',
            '任务仍是契约化合成任务，真值来自 solve()，不是真实业务数据',
            '源文档是合成的单文件归档，不是真实版本化文档库',
            '不测量 token 成本与延迟，不声称模型参数变化',
        ],
        'rows': rows,
    }
    dump(out / 'summary.json', summary)
    print(json.dumps({'by_round': by_round, 'criteria': summary['criteria'],
                      'all_criteria_met': summary['all_criteria_met']}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- stub（仅管道自检）

def stub_decisions(out: Path, round_no: int):
    tasks = [t for t in load(out / 'fixtures' / 'tasks.json') if t['round'] == round_no]
    rules = load(out / 'fixtures' / 'rules.json')
    decisions = {}
    for task in tasks:
        rule = rules[f"{task['entity']}@{task['round']}"]
        decisions[task['entity']] = {'use_experience': False, 'need_source': True, '_rule': rule}
    dump(out / 'stages' / f'r{round_no}-recall-decision.json',
         {k: {'use_experience': v['use_experience'], 'need_source': v['need_source']} for k, v in decisions.items()})
    answers = {task['entity']: {**_answer_from_rule(rules[f"{task['entity']}@{task['round']}"], task),
                                'basis': 'source'} for task in tasks}
    dump(out / 'stages' / f'r{round_no}-answers.json', answers)


def main(argv=None):
    global STUB
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True)
    parser.add_argument('--stage', required=True,
                        choices=['bootstrap', 'recall', 'source', 'settle', 'report', 'stub'])
    parser.add_argument('--round', type=int, default=0)
    parser.add_argument('--stub', action='store_true', help='只用于管道自检，结果标注为 stub')
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
