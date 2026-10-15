"""Longitudinal mechanism benchmark. No LLM inference or invented token counts.
Run: python docs/论文/run_multiround_study.py --output <new-directory>
All variants share a resolver, task stream, indexed archive and delayed verifier.
"""
from pathlib import Path
import argparse, collections, hashlib, json, platform, random, subprocess, sys, time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from butian.runtime.service import ButianService

METHODS = ['B0-scan', 'B0-index', 'B1-static', 'B2-full', 'A-no-version', 'A-no-write', 'A-no-persist']
SEEDS = [1103, 2207, 3301, 4409, 5519, 6619, 7723, 8837, 9941, 10151]
FAMILIES = ['calibration', 'schema', 'tool']

def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

def make_rule(family, entity, version, rng):
    base = dict(family=family, entity=entity, version=version)
    if family == 'calibration':
        base.update(scale=rng.choice([125, 250, 500, 1000, 2000]), offset=rng.randrange(0, 30))
    elif family == 'schema':
        base.update(flow_field=f'f{version}', level_field=f'h{version}', divisor=rng.choice([10, 100, 1000]))
    else:
        base.update(endpoint=f'/api/v{version}/{entity}', parameter=f'limit_v{version}', factor=rng.choice([10, 100, 1000]))
    return base

def solve(rule, task):
    if rule is None:
        return None
    f = rule['family']
    if f == 'calibration':
        return round((task['raw'] - rule['offset']) / rule['scale'], 10)
    if f == 'schema':
        return {'flow': task['payload'][rule['flow_field']] / rule['divisor'], 'level': task['payload'][rule['level_field']] / rule['divisor']}
    return {'endpoint': rule['endpoint'], 'kwargs': {rule['parameter']: task['raw'] * rule['factor']}}

def parse_context(text, entity, version, check_version=True):
    rules = []
    for line in text.splitlines():
        if 'RULE ' not in line:
            continue
        try:
            obj = json.loads(line.split('RULE ', 1)[1])
        except (ValueError, TypeError):
            continue
        if obj.get('entity') == entity and (not check_version or obj.get('version') == version):
            rules.append(obj)
    unique = {json.dumps(x, sort_keys=True): x for x in rules}
    return next(iter(unique.values())) if len(unique) == 1 else None

class Runner:
    def __init__(self, home, name, budget=2048):
        self.home, self.name, self.budget = home, name, budget
        self.svc = None
        self.open()
    def open(self):
        self.svc = ButianService(self.home)
        if not self.svc.store.db.execute("SELECT 1 FROM members WHERE member_id='host'").fetchone():
            self.svc.add_member('host', 'Same deterministic host resolver')
    def close(self):
        self.svc.close()
    def restart(self, alternate=None):
        self.close()
        if alternate is not None:
            self.home = alternate
        self.open()
    def remember(self, rule, eid, round_id):
        event = self.svc.store.append_event('host', 'evidence_recorded', {'rule': rule, 'source_document': eid, 'synthetic': True}, f'{eid}-{round_id}')
        mem = self.svc.memory('host')
        try:
            mem.put(rule['entity'], 'lesson', 'RULE ' + json.dumps(rule, sort_keys=True), status='warm', confidence=1.0, source_event_ids=[event], change_reason=f'verified_source_round_{round_id}')
        finally:
            mem.close()
    def context(self, entity):
        return self.svc.compose_context('host', f'resolve {entity}', budgets={'L3': self.budget})

def aggregate(rows):
    groups = {}
    for method in sorted({r['method'] for r in rows}):
        rr = [r for r in rows if r['method'] == method]
        n = len(rr)
        groups[method] = dict(tasks=n, correct=sum(r['correct'] for r in rr), wrong=sum(r['wrong'] for r in rr), unknown=sum(r['unknown'] for r in rr), coverage=sum(not r['unknown'] for r in rr)/n,
            accuracy=sum(r['correct'] for r in rr)/n, conditional_accuracy=sum(r['correct'] for r in rr)/max(1, sum(not r['unknown'] for r in rr)),
            source_reads=sum(r['source_reads'] for r in rr), queries=sum(r['queries'] for r in rr), writes=sum(r['writes'] for r in rr), source_chars=sum(r['source_chars'] for r in rr), context_chars=sum(r['context_chars'] for r in rr), memory_hits=sum(r['memory_hit'] for r in rr),
            mean_ms=1000*sum(r['elapsed_s'] for r in rr)/n, operations=sum(r['source_reads']+r['queries']+r['writes'] for r in rr))
    return groups

def bootstrap(rows):
    # Seeds, rather than correlated turns, are the resampling units.
    rng = random.Random(20261015)
    result = {}
    for baseline in ['B0-scan', 'B0-index', 'B1-static', 'A-no-version', 'A-no-write', 'A-no-persist']:
        paired = []
        for seed in SEEDS:
            bb = [r for r in rows if r['seed'] == seed and r['method'] == baseline]
            ff = [r for r in rows if r['seed'] == seed and r['method'] == 'B2-full']
            paired.append({'seed': seed, 'accuracy_delta': sum(x['correct'] for x in ff)/len(ff)-sum(x['correct'] for x in bb)/len(bb),
                           'operation_reduction': 1-sum(x['source_reads']+x['queries']+x['writes'] for x in ff)/sum(x['source_reads']+x['queries']+x['writes'] for x in bb)})
        out = {}
        for metric in ['accuracy_delta', 'operation_reduction']:
            vals = [x[metric] for x in paired]
            draws = sorted(sum(rng.choice(vals) for _ in vals)/len(vals) for _ in range(10000))
            out[metric] = {'mean': sum(vals)/len(vals), 'cluster_bootstrap_95_percentile': [draws[249], draws[9749]], 'seed_values': vals}
        result[baseline] = out
    return result

def main_study(out):
    rows, fixtures, traces, audits = [], [], [], []
    work = out/'production'
    for seed in SEEDS:
        rng = random.Random(seed)
        entities = [(f'{family}{i:03}', family) for family in FAMILIES for i in range(4)]
        archive = []
        active = {}
        # Archive contains historical records plus unrelated distractors. It is identical for all conditions.
        for entity, family in entities:
            rule = make_rule(family, entity, 1, rng)
            active[entity] = rule
            archive.append(rule)
        for i in range(12):
            archive.append(make_rule('calibration', f'distractor{i:03}', 1, rng))
        runners = {m: Runner(work/str(seed)/m, m) for m in METHODS if m not in ['B0-scan', 'B0-index']}
        try:
            for rnd in range(1, 13):
                # Staggered updates; only some families/entities drift, allowing unaffected controls.
                changed = set()
                if rnd in (5, 9):
                    for pos, (entity, family) in enumerate(entities):
                        if rng.random() < 0.5:
                            previous = active[entity]
                            rule = make_rule(family, entity, previous['version']+1, rng)
                            while {k:v for k,v in rule.items() if k != 'version'} == {k:v for k,v in previous.items() if k != 'version'}:
                                rule = make_rule(family, entity, previous['version']+1, rng)
                            active[entity] = rule
                            archive.append(rule)
                            changed.add(entity)
                if rnd == 8:
                    for m, runner in runners.items():
                        runner.restart(work/str(seed)/'A-no-persist-empty-r8' if m == 'A-no-persist' else None)
                tasks = []
                for entity, family in entities:
                    rule = active[entity]
                    raw = rng.randrange(100, 10000)
                    payload = {f'{k}{v}': rng.randrange(1, 10000) for k in ['f', 'h'] for v in [1, 2, 3]}
                    # Shared source outage in r6 and r7. No condition gets an oracle repair.
                    available = not (rnd in (6, 7) and rng.random() < 0.25)
                    task = dict(seed=seed, round=rnd, entity=entity, family=family, version=rule['version'], raw=raw, payload=payload, source_available=available, drift_now=entity in changed)
                    tasks.append(task)
                    fixtures.append(dict(task, reference=solve(rule, task)))
                rng.shuffle(tasks)
                for position, task in enumerate(tasks):
                    entity = task['entity']
                    rule = active[entity]
                    reference = solve(rule, task)
                    order = METHODS[(position+rnd+seed)%len(METHODS):] + METHODS[:(position+rnd+seed)%len(METHODS)]
                    for method in order:
                        start = time.perf_counter()
                        chosen = None
                        source_reads = source_chars = queries = writes = context_chars = 0
                        hit = False
                        recalled = []
                        if method in runners:
                            ctx, report = runners[method].context(entity)
                            queries = 1
                            context_chars = len(ctx)
                            recalled = report.recalled_memory_ids
                            chosen = parse_context(ctx, entity, task['version'], method != 'A-no-version')
                            hit = chosen is not None
                            traces.append(dict(seed=seed, round=rnd, entity=entity, method=method, context=ctx, recalled_ids=recalled))
                        fetched = False
                        if chosen is None and task['source_available']:
                            if method == 'B0-scan':
                                # Count all examined documents, not just the selected source.
                                candidates = []
                                for doc in archive:
                                    source_reads += 1
                                    source_chars += len(json.dumps(doc, sort_keys=True))
                                    if doc['entity'] == entity and doc['version'] == task['version']:
                                        candidates.append(doc)
                                chosen = candidates[0] if len(candidates) == 1 else None
                            else:
                                # Strong baseline: perfect index with one exact document lookup.
                                candidates = [d for d in archive if d['entity'] == entity and d['version'] == task['version']]
                                source_reads = 1
                                chosen = candidates[0] if len(candidates) == 1 else None
                                source_chars = len(json.dumps(chosen, sort_keys=True)) if chosen else 0
                            fetched = chosen is not None
                        answer = solve(chosen, task)
                        correct = answer is not None and answer == reference
                        unknown = answer is None
                        # Feedback follows the initial answer. It cannot retroactively improve first-pass accuracy.
                        wrong = not unknown and not correct
                        feedback_corrected = False
                        if wrong and task['source_available']:
                            source_reads += 1
                            source_chars += len(json.dumps(rule, sort_keys=True))
                            fetched = True
                            chosen = rule
                            feedback_corrected = True
                        if fetched and method in runners and method != 'A-no-write' and not (method == 'B1-static' and rnd > 1):
                            runners[method].remember(chosen, f'{seed}-{entity}-v{chosen["version"]}', rnd)
                            writes = 1 # one logical commit: internally event+memory version (reported separately)
                        elapsed = time.perf_counter()-start
                        rows.append(dict(task, method=method, condition_position=order.index(method), answer=answer, reference=reference, correct=correct, wrong=wrong, unknown=unknown,
                                         memory_hit=hit, feedback_corrected=feedback_corrected, source_reads=source_reads, queries=queries, writes=writes, source_chars=source_chars, context_chars=context_chars, elapsed_s=elapsed))
            for method, runner in runners.items():
                mem = runner.svc.memory('host')
                try:
                    for entity, family in entities:
                        if mem.get(entity):
                            audit = mem.audit_version_chain(entity)
                            audits.append(dict(seed=seed, method=method, **audit))
                finally:
                    mem.close()
        finally:
            for runner in runners.values():
                runner.close()
    dump(out/'task-stream.json', fixtures)
    dump(out/'rows.json', rows)
    dump(out/'context-traces.json', traces)
    dump(out/'version-audits.json', audits)
    summary = aggregate(rows)
    dump(out/'summary.json', {'execution_kind':'production_mechanism_benchmark', 'llm_inference_calls':0, 'seeds':SEEDS, 'rounds':12, 'families':FAMILIES, 'methods':METHODS, 'summary':summary, 'paired_seed_bootstrap':bootstrap(rows),
                             'logical_write_definition':'one verified-source commit = append_event + MemoryStore.put; SQL page writes not counted',
                             'scope':'Shared deterministic host resolver. Explicit version admission belongs to host policy; compose_context itself does not enforce version correctness.',
                             'limitations':['Synthetic contracts, not real user population', 'No measured LLM token, dollar or model-weight improvement', 'Five independent task streams; turns are correlated', 'Logical operation totals do not imply equal wall-clock costs', 'Verifier reads truth after answer for scoring only; repair requires available source', 'No disputed-memory ingestion in this study']})
    print(json.dumps(summary, indent=2), flush=True)
    return rows

def sensitivity(out):
    result = []
    for size in [8, 32, 96]:
        for budget in [128, 512, 2048]:
            for seed in SEEDS[:3]:
                run = Runner(out/'production'/'sensitivity'/f'{size}-{budget}-{seed}', 'B2-full', budget)
                try:
                    rng = random.Random(seed)
                    for i in range(size):
                        rule = make_rule('calibration', f'capacity{i:03}', 1, rng)
                        run.remember(rule, f'capacity-{i}', 0)
                    for i in rng.sample(range(size), min(8, size)):
                        entity = f'capacity{i:03}'
                        start = time.perf_counter()
                        ctx, report = run.context(entity)
                        rule = parse_context(ctx, entity, 1)
                        result.append(dict(size=size, budget=budget, seed=seed, entity=entity, hit=rule is not None, context_chars=len(ctx), elapsed_s=time.perf_counter()-start))
                finally:
                    run.close()
    dump(out/'sensitivity.json', result)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    dump(out/'protocol.json', dict(protocol_version=1, seeds=SEEDS, methods=METHODS, rounds=12, entities_per_family=4, sensitivity_sizes=[8,32,96], sensitivity_budgets=[128,512,2048], bootstrap_draws=10000, drift_probability=0.5, outage_probability=0.25, pilot_preceded_this_protocol=True, randomization='cyclic condition order by seed, round and task position', written_before_execution=True))
    rows = main_study(out)
    sensitivity(out)
    dump(out/'environment.json', dict(python=platform.python_version(), platform=platform.platform(), base_commit=subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(), script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    dump(out/'manifest.json', {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('*.json')})
    print('COMPLETE', out, flush=True)
