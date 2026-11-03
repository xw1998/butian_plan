"""Host-loop v5: 把 v4 的版本元数据消融交给外部 API 模型当宿主重跑。

协议见 `实验协议-真实宿主多轮-v5.md`（跑之前已冻结）。任务、规则、召回呈现层、
消费规则与 v4 逐条相同，只换宿主：

  deepseek-v4-pro   真正的跨模型臂（与会话宿主不同族）
  deepseek-flash    同族独立进程臂（会话宿主日志为 deepseek-v4.1-flash-1017）

每个宿主 12 实体 × 4 轮 = 48 次作答，各自一个 BUTIAN_HOME，由 v4 的 bootstrap
以同一确定性流程预置。判分两条口径分开：

  script_verdict  用**模型选中的规则**在本地算答案再判分（与 v4 口径一致，主口径）
  model_verdict   直接判分模型自己给出的答案（副口径，暴露算术混淆）

密钥只从环境变量 DEEPSEEK_API_KEY 读取，不写入任何产物；落盘消息不含 Authorization。

  --stage run     两个宿主全流程跑完（bootstrap/recall/模型决策/读源/settle）
  --stage report  聚合 Y1..Y4，写 summary.json
"""
from pathlib import Path
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_host_v4_multiround_study as v4
from run_host_multiround_study import (McpSession, dump, load, _score, _answer_from_rule,
                                       question_text)

PROTOCOL = 'host-loop-v5'
API = 'https://api.deepseek.com/chat/completions'
MODELS = ['deepseek-v4-pro', 'deepseek-flash']
ROUNDS = v4.ROUNDS
FAMILIES = v4.FAMILIES
GROUPS = v4.GROUPS
ARMS = v4.ARMS

SYSTEM = """你是一个工程助手，通过工具使用一个经验库。你会收到一道题和一次经验召回的结果。

三族任务的换算约定（公式本身不随版本变化，参数随实体与版本变化）：
- calibration: 工程值 = (raw - offset) / scale
- schema:      flow = payload[flow_field] / divisor, level = payload[level_field] / divisor
- tool:        端点 = endpoint, 参数名 = parameter, 参数值 = raw * factor

你必须严格按以下消费规则决定依据，不得自行放宽或收紧：
1. 召回给出的经验，若状态为 warm 或 hot，且其版本与本轮任务的文档版本一致 -> 据此作答；
2. 若召回项不含任何版本与状态信息 -> 按内容直接作答；
3. 若版本不一致，或状态为 candidate 或 disputed -> 声明需要读源（decision="source"），不要先作答；
4. 三者都不成立 -> decision="unknown"。

只输出一个 JSON 对象，不要 markdown 代码块，不要额外文字。字段：
{
  "decision": "experience" | "source" | "unknown",
  "memory_index": 整数或 null,   // 你采用的召回项在列表中的下标，从 0 开始
  "rule": {...} | null,          // 你决定采用的那条 RULE 对象，原样抄出，不要改写字段
  "answer": {...} | null,        // 你自己算出的答案；decision="source" 时填 null
  "reason": "一句话说明你依据上面哪一条规则"
}

answer 的格式：calibration 族 {"value": 数}；schema 族 {"flow": 数, "level": 数}；
tool 族 {"endpoint": 字符串, "parameter": 字符串, "value": 数}。
判分是精确匹配。没有可用依据时必须 decision="unknown"，不得猜。"""

SOURCE_TURN = """这是源文档的全文快照。请从中找出该实体在**本轮文档版本**下的那条 RULE，
原样抄进 rule 字段，并给出你自己算出的 answer。只输出同样格式的 JSON 对象，
decision 固定为 "source"。源文档内容：

"""


def api_call(messages, model, key):
    body = json.dumps({'model': model, 'messages': messages, 'temperature': 0,
                       'max_tokens': 2000, 'stream': False}).encode('utf-8')
    last = None
    for attempt in range(3):
        req = urllib.request.Request(API, data=body, headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        try:
            with urllib.request.urlopen(req, timeout=180) as handle:
                return json.load(handle)
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', 'replace')[:300]
            last = f'HTTP {error.code}: {detail}'
            if error.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(last)
        except Exception as error:  # 网络抖动
            last = f'{type(error).__name__}: {error}'
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f'api failed after 3 attempts: {last}')


def parse_json(text):
    text = (text or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
        if text.rstrip().endswith('```'):
            text = text.rstrip()[:-3]
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('no JSON object in reply')
    return json.loads(text[start:end + 1])


def ask(messages, model, key, calls, tag):
    """一次调用 + 解析失败重试一次；全过程落盘（不含密钥）。"""
    raw = api_call(messages, model, key)
    text = raw['choices'][0]['message']['content']
    record = {'tag': tag, 'requested_model': model, 'served_model': raw.get('model'),
              'messages': messages, 'reply': text, 'usage': raw.get('usage'),
              'finish_reason': raw['choices'][0].get('finish_reason')}
    try:
        obj = parse_json(text)
        record['parse'] = 'ok'
    except ValueError as error:
        record['parse'] = f'retry: {error}'
        retry = messages + [{'role': 'assistant', 'content': text},
                            {'role': 'user', 'content': '上次回复不是合法 JSON。只输出一个 JSON 对象，不要任何其他文字。'}]
        raw = api_call(retry, model, key)
        text = raw['choices'][0]['message']['content']
        record['retry_messages'] = retry
        record['retry_reply'] = text
        record['retry_usage'] = raw.get('usage')
        record['served_model'] = raw.get('model')
        try:
            obj = parse_json(text)
            record['parse'] = 'ok_after_retry'
        except ValueError as error2:
            record['parse'] = f'failed: {error2}'
            obj = None
    calls.append(record)
    return obj, record


def script_answer(rule, task):
    """主口径：用模型选中的规则在本地算答案。规则被改坏就记 unknown，不猜。"""
    if not isinstance(rule, dict):
        return {'unknown': True}
    try:
        return _answer_from_rule(rule, task)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {'unknown': True}


def run_model(out: Path, model: str, key: str):
    home = out / 'models' / model
    home.mkdir(parents=True, exist_ok=True)
    if not (home / 'state.json').is_file():
        v4.stage_bootstrap(home)
    tasks_all = load(home / 'fixtures' / 'tasks.json')
    usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}
    served, parse_errors, protocol_errors = set(), 0, []

    for round_no in range(1, ROUNDS + 1):
        result_path = home / 'stages' / f'r{round_no}-result.json'
        if result_path.is_file():
            print(f'[{model} r{round_no}] already settled, skip')
            continue
        v4.stage_recall(home, round_no)
        req = load(home / 'stages' / f'r{round_no}-recall-request.json')
        tasks = {t['entity']: t for t in tasks_all if t['round'] == round_no}
        decisions, answers, model_answers, calls = {}, {}, {}, []
        session = McpSession(home / 'production', 'host-loop-token', home / 'mcp-trace.jsonl')
        state = load(home / 'state.json')
        try:
            for item in req['items']:
                entity = item['entity']
                task = tasks[entity]
                user = (item['question'] + '\n\n本次经验召回返回（列表顺序即下标顺序）：\n'
                        + json.dumps(item['memories'], ensure_ascii=False, indent=2))
                messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': user}]
                obj, rec = ask(messages, model, key, calls, f'r{round_no}-{entity}-recall')
                served.add(rec.get('served_model'))
                for u in (rec.get('usage'), rec.get('retry_usage')):
                    if u:
                        usage['prompt_tokens'] += u.get('prompt_tokens', 0)
                        usage['completion_tokens'] += u.get('completion_tokens', 0)
                        usage['calls'] += 1
                if rec['parse'].startswith('failed'):
                    parse_errors += 1
                decision = (obj or {}).get('decision')
                rule = (obj or {}).get('rule')
                model_answer = (obj or {}).get('answer')
                if decision == 'source':
                    payload = session.call('butian_knowledge_source_read',
                                           source_id=state['source_id'], member_id='host', limit=50000)
                    messages = messages + [
                        {'role': 'assistant', 'content': rec.get('retry_reply') or rec['reply']},
                        {'role': 'user', 'content': SOURCE_TURN + payload.get('content', '')}]
                    obj2, rec2 = ask(messages, model, key, calls, f'r{round_no}-{entity}-source')
                    served.add(rec2.get('served_model'))
                    for u in (rec2.get('usage'), rec2.get('retry_usage')):
                        if u:
                            usage['prompt_tokens'] += u.get('prompt_tokens', 0)
                            usage['completion_tokens'] += u.get('completion_tokens', 0)
                            usage['calls'] += 1
                    if rec2['parse'].startswith('failed'):
                        parse_errors += 1
                    rule = (obj2 or {}).get('rule')
                    model_answer = (obj2 or {}).get('answer')
                    if obj2 is None:
                        decision = 'unknown'
                if decision not in ('experience', 'source', 'unknown'):
                    protocol_errors.append({'entity': entity, 'round': round_no,
                                            'why': f'bad decision {decision!r}'})
                    decision = 'unknown'
                need_source = decision == 'source'
                decisions[entity] = {'use_experience': decision == 'experience',
                                     'need_source': need_source,
                                     'why': (obj or {}).get('reason'),
                                     'memory_index': (obj or {}).get('memory_index')}
                if decision == 'unknown':
                    answers[entity] = {'unknown': True, 'basis': 'unknown'}
                    model_answers[entity] = {'unknown': True}
                else:
                    basis = 'source' if need_source else 'experience'
                    ans = {**script_answer(rule, task), 'basis': basis}
                    idx = (obj or {}).get('memory_index')
                    if basis == 'experience' and item['arm'] == 'gated' and isinstance(idx, int) \
                            and 0 <= idx < len(item['memories']):
                        ans['from_memory'] = item['memories'][idx].get('memory_id')
                    answers[entity] = ans
                    model_answers[entity] = model_answer if isinstance(model_answer, dict) else {'unknown': True}
                print(f'  [{model} r{round_no}] {entity:16s} {item["group"]:6s} {item["arm"]:5s} '
                      f'-> {decision}')
        finally:
            session.close()
        dump(home / 'stages' / f'r{round_no}-recall-decision.json', decisions)
        dump(home / 'stages' / f'r{round_no}-answers.json', answers)
        dump(home / 'stages' / f'r{round_no}-model-answers.json', model_answers)
        # source-request 由装置补写，供审计核对读源次数与 v4 口径一致
        dump(home / 'stages' / f'r{round_no}-source-request.json',
             {'round': round_no, 'reads': sum(1 for d in decisions.values() if d['need_source']),
              'items': []})
        (home / 'calls').mkdir(parents=True, exist_ok=True)
        dump(home / 'calls' / f'r{round_no}.json', calls)
        v4.stage_settle(home, round_no)
    dump(home / 'api-usage.json', {'requested_model': model, 'served_models': sorted(x for x in served if x),
                                   'usage': usage, 'parse_errors': parse_errors,
                                   'protocol_errors': protocol_errors})
    print(f'[{model}] done usage={usage} served={sorted(x for x in served if x)} '
          f'parse_errors={parse_errors} protocol_errors={len(protocol_errors)}')


# ---------------------------------------------------------------- 汇总

def _cell(rows):
    return {'n': len(rows),
            'correct': sum(r['verdict'] == 'correct' for r in rows),
            'wrong': sum(r['verdict'] == 'wrong' for r in rows),
            'unknown': sum(r['verdict'] == 'unknown' for r in rows),
            'reads': sum(1 for r in rows if r['need_source']),
            'stale_match': sum(1 for r in rows if r['stale_match'])}


def model_summary(out: Path, model: str):
    home = out / 'models' / model
    rows, model_rows = [], []
    tasks = {(t['round'], t['entity']): t for t in load(home / 'fixtures' / 'tasks.json')}
    for round_no in range(1, ROUNDS + 1):
        block = load(home / 'stages' / f'r{round_no}-result.json')['results']
        rows.extend(block)
        ma = load(home / 'stages' / f'r{round_no}-model-answers.json')
        for row in block:
            model_rows.append(dict(row, verdict=_score(tasks[(row['round'], row['entity'])],
                                                       ma.get(row['entity'], {'unknown': True}))))

    def pick(group=None, arm=None, family=None, rmin=1, rmax=ROUNDS, src=None):
        return [r for r in (src if src is not None else rows)
                if (group is None or r['group'] == group)
                and (arm is None or r['arm'] == arm)
                and (family is None or r['family'] == family)
                and rmin <= r['round'] <= rmax]

    late = lambda arm: pick('drift', arm, rmin=2)
    g, n = late('gated'), late('naive')
    g_acc = sum(r['verdict'] == 'correct' for r in g) / len(g) if g else 0.0
    n_acc = sum(r['verdict'] == 'correct' for r in n) / len(n) if n else 0.0
    gap = (g_acc - n_acc) * 100
    y1 = len(g) == 9 and len(n) == 9 and gap >= 50.0
    n_wrong = [r for r in n if r['verdict'] == 'wrong']
    stale_ratio = (sum(r['stale_match'] for r in n_wrong) / len(n_wrong)) if n_wrong else None
    y2 = bool(n_wrong) and stale_ratio >= 2 / 3
    gs, ns = pick('stable', 'gated'), pick('stable', 'naive')
    y3 = (len(gs) == 12 and len(ns) == 12
          and sum(r['verdict'] == 'correct' for r in gs) >= 11
          and sum(r['verdict'] == 'correct' for r in ns) >= 11)
    y4 = all(r['memory_version'] == 1 and r['memory_status'] == 'warm' for r in rows)

    if y1 and y2 and y3 and y4:
        verdict = 'v5_replicated'
    elif not y3:
        verdict = 'v5_control_failed'
    elif y1:
        verdict = 'v5_direction_only'
    else:
        verdict = 'v5_not_replicated'

    by_family = {}
    for family in FAMILIES:
        for arm in ARMS:
            sub = pick('drift', arm, family=family, rmin=2)
            by_family[f'drift-late-{family}-{arm}'] = _cell(sub)
    cal_g = pick('drift', 'gated', family='calibration', rmin=2)
    cal_n = pick('drift', 'naive', family='calibration', rmin=2)
    cal_gap = ((sum(r['verdict'] == 'correct' for r in cal_g) / len(cal_g)
                - sum(r['verdict'] == 'correct' for r in cal_n) / len(cal_n)) * 100
               if cal_g and cal_n else None)

    usage = load(home / 'api-usage.json')
    return {
        'requested_model': model,
        'served_models': usage['served_models'],
        'verdict': verdict,
        'criteria': {'Y1_direction_replicated': y1, 'Y2_failure_is_stale_reuse': y2,
                     'Y3_stable_control_holds': y3, 'Y4_store_unchanged': y4},
        'drift_late': {'gated': _cell(g), 'naive': _cell(n), 'gap_pp': round(gap, 2)},
        'stale_ratio_among_wrong': None if stale_ratio is None else round(stale_ratio, 4),
        'stable': {'gated': _cell(gs), 'naive': _cell(ns)},
        'by_cell': {f'{grp}-{arm}': _cell(pick(grp, arm)) for grp in GROUPS for arm in ARMS},
        'by_family_drift_late': by_family,
        'clean_subgroup_calibration_gap_pp': None if cal_gap is None else round(cal_gap, 2),
        'model_arithmetic': {
            'correct': sum(r['verdict'] == 'correct' for r in model_rows),
            'n': len(model_rows),
            'by_cell': {f'{grp}-{arm}': _cell(pick(grp, arm, src=model_rows))
                        for grp in GROUPS for arm in ARMS},
        },
        'api': usage,
        'rows': rows,
    }


def stage_report(out: Path):
    per_model = {m: model_summary(out, m) for m in MODELS}
    pro = per_model['deepseek-v4-pro']['verdict']
    flash = per_model['deepseek-flash']['verdict']
    if pro == flash == 'v5_replicated':
        verdict = 'v5_cross_model_replicated'
    elif pro == 'v5_replicated':
        verdict = 'v5_cross_model_replicated_on_pro_only'
    elif flash == 'v5_replicated':
        verdict = 'v5_same_family_only'
    else:
        verdict = 'v5_not_replicated'
    summary = {
        'execution_kind': 'external_model_host_multiround_v5',
        'protocol': PROTOCOL,
        'verdict': verdict,
        'host_kind': 'external_api_models',
        'task_seed': v4.SEED,
        'design': 'identical tasks/store/presentation/consumption rule as host-loop-v4; only the host changes',
        'total_answers': sum(len(per_model[m]['rows']) for m in MODELS),
        'models': per_model,
        'limitations': [
            '设计者非盲仍在；宿主非盲在本档被消除（外部模型不知道分组与假设）',
            'naive 臂「无元数据即信任内容」是写进 prompt 的建模假设，不是对真实记忆模块的实测',
            '主口径算术由脚本执行，副口径单独报告模型算术，两者不得混引',
            'naive 呈现层只抹掉显式 version 与 memory_id/status，tool/schema 族内容仍含偶然版本线索；'
            'calibration 子群是唯一干净消融，已预注册单独报告',
            '每宿主单次运行、temperature=0，无重复采样，不报 p 值',
            '不写回，故不回答「能不能学会新版本」',
            '两个外部模型同属一家厂商，不能外推为任意模型',
            '实际模型名以 API 响应 model 字段为准，已逐次落盘',
        ],
    }
    dump(out / 'summary.json', summary)
    print(json.dumps({'verdict': verdict,
                      'per_model': {m: {'verdict': per_model[m]['verdict'],
                                        'criteria': per_model[m]['criteria'],
                                        'drift_late': per_model[m]['drift_late'],
                                        'stable': per_model[m]['stable'],
                                        'clean_calibration_gap_pp': per_model[m]['clean_subgroup_calibration_gap_pp'],
                                        'served_models': per_model[m]['served_models']}
                                    for m in MODELS}}, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True)
    parser.add_argument('--stage', required=True, choices=['run', 'report'])
    parser.add_argument('--models', nargs='*', default=MODELS)
    args = parser.parse_args(argv)
    out = Path(args.out)
    if not out.is_absolute():
        out = (HERE / out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.stage == 'run':
        key = os.environ.get('DEEPSEEK_API_KEY')
        if not key:
            raise SystemExit('DEEPSEEK_API_KEY 未设置')
        for model in args.models:
            run_model(out, model, key)
    else:
        stage_report(out)


if __name__ == '__main__':
    main()