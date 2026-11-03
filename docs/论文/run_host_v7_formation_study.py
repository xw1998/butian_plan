"""Host-loop v7: 形成侧有效性——库清空，宿主边干边攒，再看攒出来的经验能不能复用。

协议见 `实验协议-真实宿主多轮-v7.md`（跑之前已冻结，含一条实施前的修订 r1）。

  arm form    空库起步：recall + source_read + practice + revise，边读源边写回
  arm noref   只有 source_read，每题翻手册（不用经验库的基线）

判据 F1..F5：不比不用差 / 二三轮读源省一半 / 基线不瘸 / 复用率过半 / 零污染。
语料与 v6 完全同源（同种子、同手册、同漂移日程），只把「预置 120 条 warm」换成「从空库起步」。

密钥只从环境变量 DEEPSEEK_API_KEY 读取，不写入任何产物。

  --stage bootstrap   建语料、建两个臂的源快照、登记 (实体,版本) 的证据件
  --stage run         两臂三轮全跑（已结算的轮次自动跳过）
  --stage report      聚合 F1..F5，写 summary.json
"""
from pathlib import Path
import argparse
import json
import os
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_host_v5_multiround_study as v5
import run_host_v6_effectiveness_study as v6
from run_host_multiround_study import (McpSession, dump, load, service_call, sha256_text,
                                       _score, _answer_from_rule)

PROTOCOL = 'host-loop-v7'
SEED = v6.SEED
ROUNDS = v6.ROUNDS
MODEL = 'deepseek-flash'   # 刻意不用 pro：本档自变量是形成链，不是模型；且用户要求省钱
ARMS = ['form', 'noref']
MEMBER = 'host'
TOKEN = 'host-loop-token'
KIND = 'lesson'
RECALL_LIMIT = 20
SOURCE_LIMIT = 50000  # butian_knowledge_source_read 的上限

# ---------------------------------------------------------------- 抗服务端拥塞

API_ATTEMPTS = 8            # 503 洪水下的总尝试次数（v5 原件只有 3 次、共 ~18 s）
API_BACKOFF_CAP = 45        # 单次退避上限（秒）


def install_resilient_api():
    """把 v5 的 api_call 换成「退避更久」的版本：2026-11-03 实测官方 API 间歇 503。

    只改重试策略，不改请求体、不改判分、不改任何读数口径。
    """
    import urllib.error
    import urllib.request

    def api_call(messages, model, key, attempts=API_ATTEMPTS):
        body = json.dumps({'model': model, 'messages': messages, 'temperature': 0,
                           'max_tokens': 2000, 'stream': False}).encode('utf-8')
        last = None
        for attempt in range(attempts):
            req = urllib.request.Request(v5.API, data=body, headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
            try:
                with urllib.request.urlopen(req, timeout=180) as handle:
                    return json.load(handle)
            except urllib.error.HTTPError as error:
                detail = error.read().decode('utf-8', 'replace')[:300]
                last = f'HTTP {error.code}: {detail}'
                if error.code in (429, 500, 502, 503, 504):
                    delay = min(API_BACKOFF_CAP, 2 * (2 ** attempt))
                    print(f'[retry] {error.code} {attempt + 1}/{attempts} sleep {delay}s',
                          flush=True)
                    time.sleep(delay)
                    continue
                raise RuntimeError(last)
            except Exception as error:  # 网络抖动 / 超时
                last = f'{type(error).__name__}: {error}'
                delay = min(API_BACKOFF_CAP, 2 * (2 ** attempt))
                print(f'[retry] {last} {attempt + 1}/{attempts} sleep {delay}s', flush=True)
                time.sleep(delay)
        raise RuntimeError(f'api failed after {attempts} attempts: {last}')

    v5.api_call = api_call


install_resilient_api()


def _ckpt_load(path: Path):
    """读逐题断点：被强杀时最后一行可能只写了一半，解析不了的直接丢。"""
    rows, calls = [], []
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get('kind') == 'row':
                rows.append(item['payload'])
            elif item.get('kind') == 'call':
                calls.append(item['payload'])
    return rows, calls


def _ckpt_append(handle, kind: str, payload):
    handle.write(json.dumps({'kind': kind, 'payload': payload}, ensure_ascii=False) + '\n')
    handle.flush()
    os.fsync(handle.fileno())


# ---------------------------------------------------------------- 语料（复用 v6）

def build_corpus():
    """v6 的 120 实体 × 1..4 版本手册 + 24 实体 × 3 轮任务，逐位相同。"""
    return v6.build_corpus()

def rule_line(rule) -> str:
    return 'RULE ' + json.dumps(rule, sort_keys=True, ensure_ascii=False)

# ---------------------------------------------------------------- 提示词

FORMULAS = v6.FORMULAS

FORMAT = """只输出一个 JSON 对象，不要 markdown 代码块，不要额外文字。字段：
{
  "decision": "experience" | "source" | "unknown",
  "rule": {...} | null,          // 你采用的那条 RULE 对象，原样抄出，不要改写字段
  "answer": {...} | null,        // 你自己算出的答案
  "memorize": "RULE {...}" | null,   // 仅当你读了源文档：把该实体本轮版本那条 RULE 原样抄成一行
  "reason": "一句话说明依据"
}

answer 的格式：calibration 族 {"value": 数}；schema 族 {"flow": 数, "level": 数}；
tool 族 {"endpoint": 字符串, "parameter": 字符串, "value": 数}。
判分是精确匹配。只有在拿到源文档之后仍然找不到依据时，才 decision="unknown"，不得凭记忆猜。"""

FORM_SYSTEM = """你是一个工程助手，靠自己的经验库和源文档答题。经验库从空开始，你要边干边攒。

""" + FORMULAS + """

读源机制：你看不到源文档，但可以申请。decision="source" 就是「申请读源」——装置收到后会把
手册全文（120 个实体、每个版本一行 RULE）一次发给你，你在第二轮给出最终作答，decision 仍写
"source"。所以「本轮没有附手册」不代表读不到，不要因为这个就答 unknown。

消费规则（按顺序判，命中即止，严格照做）：
1. 召回里有**本实体**（memory_id 与题面实体名完全相同）的条目，其内容版本与本轮文档版本一致，
   状态为 warm / hot / candidate -> decision="experience"，rule 抄那一条，直接作答。
   candidate 是你自己上一轮从源文档抄下来的，证据就是源行本身，允许直接用；
   needs_revalidation 只是提示，不是禁令。
2. 召回里没有本实体的条目 -> decision="source"。别的实体、别的族的条目都不算依据，不许套用。
3. 有本实体条目但版本不一致，或状态为 disputed -> decision="source"。
4. 拿到手册之后：找出该实体**本轮版本**那一行 RULE，原样抄进 rule 和 memorize（两者必须完全一致，
   一个字都不要改），并给出你自己算的 answer；decision 仍写 "source"。
   memorize 是你下一轮不用再翻手册的唯一依据。
5. 只有拿到手册之后、源里确实找不到该实体本轮版本那一行时，才 decision="unknown"。

""" + FORMAT

NOREF_SYSTEM = v6.NOREF_SYSTEM

SOURCE_TURN = v5.SOURCE_TURN

REMINDER = ('\n\n注意：本轮没有附源文档。若你需要查手册（比如召回里没有本实体可用的条目），'
            '直接输出 decision="source"，装置会立刻把手册全文发给你；'
            '不要因为看不到手册就答 unknown。')

# ---------------------------------------------------------------- 阶段：语料与证据

def build_fixtures(out: Path):
    plan, tasks, rules = build_corpus()
    archive = v6.build_archive(rules)
    fixtures = out / 'fixtures'
    fixtures.mkdir(parents=True, exist_ok=True)
    archive_path = fixtures / 'contract-manual-120.txt'
    archive_path.write_text(archive, encoding='utf-8')
    dump(fixtures / 'tasks.json', tasks)
    dump(fixtures / 'plan.json', plan)
    dump(fixtures / 'rules.json', {f'{k[0]}@{k[1]}': v for k, v in rules.items()})
    return plan, tasks, rules, archive, archive_path

def stage_arm(out: Path, arm: str, tasks, rules, archive, archive_path: Path):
    """建一个臂的库：成员 + 源快照 + (实体,版本) 证据件。记忆库保持空。"""
    home = out / 'models' / arm
    (home / 'fixtures').mkdir(parents=True, exist_ok=True)
    dump(home / 'fixtures' / 'tasks.json', tasks)
    production = home / 'production'
    digest = sha256_text(archive)

    def setup(svc):
        row = svc.store.db.execute("SELECT 1 FROM members WHERE member_id=?", (MEMBER,)).fetchone()
        if not row:
            svc.add_member(MEMBER, f'Real host driving Butian over MCP (v7 formation, {arm})')
        svc.store.set_member_token(MEMBER, TOKEN)
        return svc.learning.add_source('manual', str(archive_path),
                                       '契约手册（120 实体，v1-v4）', digest,
                                       owner_member_id=MEMBER)

    source_id = service_call(production, setup)
    verified = service_call(production, lambda svc: svc.learning.verify_source_content(
        source_id, archive, owner_member_id=MEMBER))

    # 证据件：每个 (实体, 版本) 一份，正文就是手册里那一行 RULE。
    # 这样 evidence_ref 的哈希身份仍然由源行决定（文件名带 hash 前 8 位，映射落盘可核），
    # 同时满足补天的「证据必须登记且可校验」这条硬约束（见协议修订 r1）。
    evidence_dir = home / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    mapping = {}

    def register(svc):
        for key in sorted(rules):
            entity, version = key
            line = rule_line(rules[key])
            body = line + '\n'
            name = f'manual-{entity}-v{version}-{sha256_text(body)[:8]}.txt'
            path = evidence_dir / name
            path.write_text(body, encoding='utf-8')
            evidence_id = svc.record_evidence(path, owner_member_id=MEMBER,
                                             actor_member_id=MEMBER)
            mapping[f'{entity}@{version}'] = {'evidence_ref': evidence_id,
                                              'path': str(path),
                                              'sha256': sha256_text(body)}

    service_call(production, register)
    dump(home / 'fixtures' / 'evidence-map.json', mapping)
    dump(home / 'state.json', {'protocol': PROTOCOL, 'arm': arm, 'source_id': source_id,
                               'source_sha256': digest, 'archive': str(archive_path),
                               'archive_chars': len(archive),
                               'archive_bytes': len(archive.encode('utf-8')),
                               'verified': verified, 'created_at': time.time()})
    print(f'[bootstrap {arm}] source_id={source_id} verified={verified} '
          f'evidence={len(mapping)} archive={len(archive.encode())}B')

def stage_bootstrap(out: Path):
    plan, tasks, rules, archive, archive_path = build_fixtures(out)
    for arm in ARMS:
        stage_arm(out, arm, tasks, rules, archive, archive_path)
    dump(out / 'fixtures' / 'corpus.json',
         {'protocol': PROTOCOL, 'seed': SEED, 'entities': len(plan),
          'tasks': len(tasks), 'rounds': ROUNDS, 'archive_chars': len(archive)})
    print(f'[bootstrap] entities={len(plan)} tasks={len(tasks)} '
          f'archive={len(archive.encode())}B')

# ---------------------------------------------------------------- 工具封装

def _read_source(session, state):
    """读源并核对完整性。截断的手册会让基线看起来更差，必须硬失败。"""
    payload = session.call('butian_knowledge_source_read', source_id=state['source_id'],
                           member_id=MEMBER, limit=SOURCE_LIMIT)
    text = payload.get('content', '')
    expected = state.get('archive_chars')
    if expected is not None and len(text) != expected:
        raise SystemExit(f'源文档被截断：返回 {len(text)} 字符，期望 {expected}')
    return text

def _recall(session, entity, family, round_no):
    payload = session.call('butian_memory_recall',
                           query=f'{entity} {family}',
                           decision_id=f'v7-r{round_no}-{entity}',
                           member_id=MEMBER, limit=RECALL_LIMIT)
    items = payload.get('memories') or []
    return [{key: item.get(key) for key in
             ('memory_id', 'status', 'content', 'needs_revalidation', 'recall_confidence')}
            for item in items]

def _write_back(session, task, memorize, evidence_map, form_state):
    """把模型抄回来的 RULE 原样入库。装置不纠正内容——能测污染的前提。"""
    entity, version = task['entity'], task['version']
    content = (memorize or '').strip() if isinstance(memorize, str) else ''
    record = form_state.get(entity)
    if not content:
        return {'action': 'skipped', 'error': 'missing_memorize'}
    proof = evidence_map.get(f'{entity}@{version}')
    if not proof:
        return {'action': 'skipped', 'error': 'evidence_missing'}
    if record is None:
        payload = session.call('butian_memory_practice', memory_id=entity, kind=KIND,
                               content=content, outcome='success',
                               independent_key=f'src-{entity}-v{version}',
                               evidence_ref=proof['evidence_ref'])
        form_state[entity] = {'content': content, 'version': version,
                              'status': payload.get('status'), 'writes': 1}
        return {'action': 'practice', 'status': payload.get('status'),
                'successes': payload.get('successes')}
    if record['content'] == content:
        return {'action': 'unchanged'}
    payload = session.call('butian_memory_revise', memory_id=entity, content=content,
                           reason=f'源文档 v{version} 与库中内容不一致，按本轮源行改写',
                           evidence_refs=[proof['evidence_ref']])
    form_state[entity] = {'content': content, 'version': version,
                          'status': payload.get('status'),
                          'writes': int(record.get('writes', 0)) + 1}
    return {'action': 'revise', 'status': payload.get('status'),
            'previous_status': payload.get('previous_status')}

# ---------------------------------------------------------------- 作答

def _truth_rows(tasks, round_no):
    return [t for t in tasks if t['round'] == round_no]

def _verdict(task, rule, answer):
    if isinstance(rule, dict):
        try:
            script_ans = _answer_from_rule(rule, task)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            script_ans = {'unknown': True}
    else:
        script_ans = {'unknown': True}
    return _score(task, script_ans), _score(task, answer)

def run_form(home: Path, key: str, round_no: int):
    state = load(home / 'state.json')
    tasks = load(home / 'fixtures' / 'tasks.json')
    evidence_map = load(home / 'fixtures' / 'evidence-map.json')
    form_state = load(home / 'form-state.json') if (home / 'form-state.json').is_file() else {}
    calls_dir = home / 'calls' / f'r{round_no}-form'
    calls_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = calls_dir / f'_checkpoint-r{round_no}.jsonl'
    rows, calls = _ckpt_load(ckpt_path)
    done = {r['entity'] for r in rows}
    reads = sum(int(r.get('reads_this') or 0) for r in rows)
    if done:
        print(f'[form r{round_no}] resume: {len(done)} 题已结算，继续', flush=True)
    ckpt = ckpt_path.open('a', encoding='utf-8')
    session = McpSession(home / 'production', TOKEN, home / 'mcp-trace.jsonl')
    try:
        for task in _truth_rows(tasks, round_no):
            entity = task['entity']
            if entity in done:
                continue
            memories = _recall(session, entity, family=task['family'], round_no=round_no)
            own = next((m for m in memories if m.get('memory_id') == entity), None)
            memory_before = {'status': (own or {}).get('status'),
                             'version': (form_state.get(entity) or {}).get('version'),
                             'content': (own or {}).get('content')}
            shown = [{'memory_id': m.get('memory_id'), 'status': m.get('status'),
                      'content': (m.get('content') or '')[:400],
                      'needs_revalidation': m.get('needs_revalidation')}
                     for m in memories]
            user = (v6.question_text(task) + '\n\n'
                    + f'本次经验召回返回（共 {len(shown)} 条，列表顺序即下标顺序）：\n'
                    + json.dumps(shown, ensure_ascii=False, indent=2) + REMINDER)
            messages = [{'role': 'system', 'content': FORM_SYSTEM},
                        {'role': 'user', 'content': user}]
            tag = f'r{round_no}-form-{entity}'
            written_calls = len(calls)
            reads_before = reads
            obj, record = v5.ask(messages, MODEL, key, calls, tag)
            if (obj or {}).get('decision') == 'source':
                src_text = _read_source(session, state)
                reads += 1
                follow = messages + [
                    {'role': 'assistant', 'content': record.get('retry_reply') or record['reply']},
                    {'role': 'user', 'content': SOURCE_TURN + src_text}]
                obj2, record2 = v5.ask(follow, MODEL, key, calls, tag + '-source')
                if obj2 is not None:
                    obj = obj2
            decision = (obj or {}).get('decision')
            rule = (obj or {}).get('rule')
            answer = (obj or {}).get('answer')
            script_verdict, model_verdict = _verdict(task, rule, answer)
            if decision == 'source':
                try:
                    write = _write_back(session, task, (obj or {}).get('memorize'),
                                        evidence_map, form_state)
                except RuntimeError as error:
                    write = {'action': 'error', 'error': str(error)[:200]}
            else:
                write = {'action': 'not_applicable'}
            rows.append({'round': round_no, 'entity': entity, 'family': task['family'],
                         'k': task['k'], 'arm': 'form', 'version': task['version'],
                         'stale': task['stale'], 'decision': decision,
                         'memory_before': memory_before,
                         'stale_reuse': bool(decision == 'experience' and memory_before['version']
                                             not in (None, task['version'])),
                         'reads_this': reads - reads_before,
                         'write': write,
                         'script_verdict': script_verdict,
                         'model_verdict': model_verdict,
                         'answer': answer, 'truth': task['truth']})
            dump(home / 'form-state.json', form_state)
            _ckpt_append(ckpt, 'row', rows[-1])
            for record in calls[written_calls:]:
                _ckpt_append(ckpt, 'call', record)
            written_calls = len(calls)
    finally:
        ckpt.close()
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
            'served_models': sorted({r.get('served_model') for r in calls if r.get('served_model')})}

def run_noref(home: Path, key: str, round_no: int):
    state = load(home / 'state.json')
    tasks = load(home / 'fixtures' / 'tasks.json')
    calls_dir = home / 'calls' / f'r{round_no}-noref'
    calls_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = calls_dir / f'_checkpoint-r{round_no}.jsonl'
    rows, calls = _ckpt_load(ckpt_path)
    done = {r['entity'] for r in rows}
    reads = sum(int(r.get('reads_this') or 0) for r in rows)
    if done:
        print(f'[noref r{round_no}] resume: {len(done)} 题已结算，继续', flush=True)
    ckpt = ckpt_path.open('a', encoding='utf-8')
    session = McpSession(home / 'production', TOKEN, home / 'mcp-trace.jsonl')
    try:
        for task in _truth_rows(tasks, round_no):
            if task['entity'] in done:
                continue
            src_text = _read_source(session, state)
            reads += 1
            user = (v6.question_text(task) + '\n\n以下是源文档全文快照：\n\n' + src_text)
            messages = [{'role': 'system', 'content': NOREF_SYSTEM},
                        {'role': 'user', 'content': user}]
            tag = f'r{round_no}-noref-{task["entity"]}'
            written_calls = len(calls)
            obj, record = v5.ask(messages, MODEL, key, calls, tag)
            rule = (obj or {}).get('rule')
            answer = (obj or {}).get('answer')
            script_verdict, model_verdict = _verdict(task, rule, answer)
            rows.append({'round': round_no, 'entity': task['entity'], 'family': task['family'],
                         'k': task['k'], 'arm': 'noref', 'version': task['version'],
                         'stale': task['stale'], 'decision': (obj or {}).get('decision'),
                         'memory_before': None, 'stale_reuse': False,
                         'reads_this': 1, 'write': None,
                         'script_verdict': script_verdict,
                         'model_verdict': model_verdict,
                         'answer': answer, 'truth': task['truth']})
            _ckpt_append(ckpt, 'row', rows[-1])
            for record in calls[written_calls:]:
                _ckpt_append(ckpt, 'call', record)
            written_calls = len(calls)
    finally:
        ckpt.close()
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
            'served_models': sorted({r.get('served_model') for r in calls if r.get('served_model')})}

def run_arm(out: Path, arm: str, key: str, round_no: int):
    home = out / 'models' / arm
    if not (home / 'state.json').is_file():
        plan, tasks, rules, archive, archive_path = build_fixtures(out)
        stage_arm(out, arm, tasks, rules, archive, archive_path)
    stages = home / 'stages'
    stages.mkdir(parents=True, exist_ok=True)
    path = stages / f'r{round_no}-{arm}.json'
    if path.is_file():
        print(f'[{arm} r{round_no}] settled, skip')
        return load(path)
    result = run_form(home, key, round_no) if arm == 'form' else run_noref(home, key, round_no)
    dump(path, result)
    print(f'[{arm} r{round_no}] rows={len(result["rows"])} reads={result["reads"]} '
          f'prompt={result["usage"]["prompt_tokens"]} '
          f'correct={sum(1 for r in result["rows"] if r["script_verdict"] == "correct")}')
    return result

# ---------------------------------------------------------------- 汇总

def _acc(rows):
    n = len(rows)
    correct = sum(1 for r in rows if r['script_verdict'] == 'correct')
    wrong = sum(1 for r in rows if r['script_verdict'] == 'wrong')
    unknown = sum(1 for r in rows if r['script_verdict'] == 'unknown')
    return {'n': n, 'correct': correct, 'wrong': wrong, 'unknown': unknown,
            'acc': round(correct / n, 4) if n else 0.0}

def _arm_summary(home: Path, arm: str) -> dict:
    rows, by_round, usage = [], {}, {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}
    for round_no in range(1, ROUNDS + 1):
        data = load(home / 'stages' / f'r{round_no}-{arm}.json')
        rows.extend(data['rows'])
        by_round[f'R{round_no}'] = {**_acc(data['rows']), 'reads': data['reads']}
        for key_name in usage:
            usage[key_name] += data['usage'][key_name]
    writes = [r['write'] or {} for r in rows if r['arm'] == 'form']
    return {**_acc(rows), 'by_round': by_round, 'usage': usage,
            'reads': sum(v['reads'] for v in by_round.values()),
            'writes': {'practice': sum(1 for w in writes if w.get('action') == 'practice'),
                       'revise': sum(1 for w in writes if w.get('action') == 'revise'),
                       'unchanged': sum(1 for w in writes if w.get('action') == 'unchanged'),
                       'skipped': sum(1 for w in writes if w.get('action') == 'skipped'),
                       'error': sum(1 for w in writes if w.get('action') == 'error'),
                       'errors': sorted({w.get('error') for w in writes if w.get('error')})[:6]},
            'rows': rows}

def stage_report(out: Path):
    arms = {}
    for arm in ARMS:
        home = out / 'models' / arm
        if not all((home / 'stages' / f'r{r}-{arm}.json').is_file() for r in range(1, ROUNDS + 1)):
            continue
        arms[arm] = _arm_summary(home, arm)

    summary = {'protocol': PROTOCOL, 'execution_kind': 'external_model_formation_v7',
               'task_seed': SEED, 'model': MODEL}
    if len(arms) != len(ARMS):
        summary['verdict'] = 'incomplete'
        dump(out / 'summary.json', summary)
        print('[report] incomplete: 还有臂/轮次没跑完')
        return summary

    form, noref = arms['form'], arms['noref']
    f_rows = form['rows']
    r23 = [r for r in f_rows if r['round'] >= 2]
    reads_form_23 = sum(1 for r in r23 if r['reads_this'] > 0)
    reads_noref_23 = sum(noref['by_round'][f'R{r}']['reads'] for r in (2, 3))
    reuse = [r for r in r23 if r['reads_this'] == 0 and r['decision'] == 'experience'
             and r['script_verdict'] == 'correct']
    contamination = [r for r in f_rows if r['decision'] == 'experience'
                     and r['script_verdict'] == 'wrong']
    stale_reuse = [r for r in f_rows if r['stale_reuse']]

    f1 = form['acc'] >= noref['acc'] - 0.05
    f2 = reads_form_23 <= 0.5 * reads_noref_23 if reads_noref_23 else False
    f3 = noref['acc'] >= 0.95
    f4 = (len(reuse) / len(r23)) >= 0.50 if r23 else False
    f5 = len(contamination) == 0

    if not f3:
        verdict = 'v7_baseline_handicapped'
    elif not f1:
        verdict = 'v7_formation_regressed'
    elif f1 and f3 and f5 and f2 and f4:
        verdict = 'v7_formation_effective'
    elif f1 and f3 and f5:
        verdict = 'v7_formation_reuse_weak'
    elif f1 and f3 and f2 and f4 and not f5:
        verdict = 'v7_formation_effective_with_contamination'
    else:
        verdict = 'v7_formation_reuse_weak'

    summary.update({
        'arms': {arm: {k: v for k, v in data.items() if k != 'rows'} for arm, data in arms.items()},
        'criteria': {'F1_accuracy_not_worse': f1, 'F2_reads_r23_at_most_half': f2,
                     'F3_baseline_above_95': f3, 'F4_reuse_rate_above_half': f4,
                     'F5_zero_contamination': f5},
        'reads_r23': {'form': reads_form_23, 'noref': reads_noref_23,
                      'form_threshold': round(0.5 * reads_noref_23, 1),
                      'saving_pct': round((1 - reads_form_23 / reads_noref_23) * 100, 1)
                      if reads_noref_23 else None},
        'reuse': {'n': len(r23), 'reused': len(reuse),
                  'rate': round(len(reuse) / len(r23), 4) if r23 else 0.0},
        'contamination': {'n': len(contamination),
                          'rows': [{'round': r['round'], 'entity': r['entity'],
                                    'answer': r['answer'], 'truth': r['truth']}
                                   for r in contamination]},
        'stale_reuse': {'n': len(stale_reuse),
                        'rows': [{'round': r['round'], 'entity': r['entity'],
                                  'memory_version': r['memory_before']['version'],
                                  'task_version': r['version']} for r in stale_reuse]},
        'verdict': verdict,
    })
    dump(out / 'summary.json', summary)
    print(f'[report] verdict={verdict} F={summary["criteria"]}')
    print(f'  form acc={form["acc"]} reads={form["reads"]} | noref acc={noref["acc"]} '
          f'reads={noref["reads"]}')
    print(f'  R2+R3 reads form={reads_form_23} noref={reads_noref_23} '
          f'reuse={len(reuse)}/{len(r23)} contamination={len(contamination)}')
    return summary

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--stage', required=True,
                        choices=['bootstrap', 'run', 'report', 'auto'])
    parser.add_argument('--arm', default=None, choices=ARMS)
    parser.add_argument('--max-tries', type=int, default=40,
                        help='auto 阶段单次 run 失败后的重试上限')
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
    arms = [args.arm] if args.arm else ARMS
    tries = args.max_tries if args.stage == 'auto' else 1
    for attempt in range(tries):
        try:
            for round_no in range(1, ROUNDS + 1):
                for arm in arms:
                    run_arm(out, arm, key, round_no)
            break
        except Exception as error:  # 服务端拥塞 / MCP 抖动：断点续跑，退避重来
            if args.stage != 'auto' or attempt == tries - 1:
                raise
            delay = min(300, 30 * (attempt + 1))
            print(f'[auto] 第 {attempt + 1} 次中断：{type(error).__name__}: {error}'
                  f'；{delay}s 后从断点续跑', flush=True)
            time.sleep(delay)
    stage_report(out)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
